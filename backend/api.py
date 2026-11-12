import ipaddress
import json
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Literal, Optional, Union

import chess
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.datastructures import MutableHeaders
from starlette.routing import Match

from backend.analyzer import GameAnalyzer  # kept exported on module for contract tests
from backend.db import closing_db, get_db, get_setting, init_db, set_setting
from backend.deviations import (
    find_game_deviations,
    get_deviation_drill,
    mark_deviation_resolved,
    reset_resolved_deviations,
)
from backend.evaluator import (
    SYNC_POOL_WAIT_TIMEOUT,
    AsyncEngineEvaluator,
    EngineBusy,
    get_sync_engine_pool,
)
from backend.game_analysis import (
    DEFAULT_ANALYSIS_DEPTH,
    MAX_ANALYSIS_DEPTH,
    MIN_ANALYSIS_DEPTH,
    analyze_full_game_sync,
    preview_sequence_for_repertoire,
    save_sequence_to_repertoire,
    stream_game_analysis,
)
from backend.lichess_auth import (
    complete_oauth_pkce,
    inspect_token_capabilities,
    start_oauth_pkce,
)
from backend.lichess_client import DEFAULT_PERF_TYPES, LichessClient
from backend.live_play import LivePlayError, LivePlayManager
from backend.openings import OpeningBook
from backend.repertoire import RepertoireManager, extract_player_info, normalize_fen

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_JSON_DEPTH = 32
SQLITE_INT64_MAX = 2 ** 63 - 1
SQLITE_INT64_MIN = -(2 ** 63)

logger = logging.getLogger("chess.api")

STANDARD_CASTLING_CHARS = frozenset("KQkq-")
MS_EPOCH_THRESHOLD = 100_000_000_000


def is_valid_fen(fen: str) -> bool:
    """Strictly validates a standard-chess FEN string and board legality."""
    if not fen or not isinstance(fen, str):
        return False
    parts = fen.strip().split()
    if len(parts) < 4 or parts[0].count("/") != 7 or len(parts) > 6:
        return False
    if any(ch not in STANDARD_CASTLING_CHARS for ch in parts[2]):
        return False
    if len(parts) >= 5 and (not parts[4].isdigit() or len(parts[4]) > 6):
        return False
    if len(parts) >= 6 and (not parts[5].isdigit() or len(parts[5]) > 6):
        return False
    try:
        board = chess.Board(fen)
        return board.is_valid()
    except Exception:
        return False


def normalize_played_at(value: Any) -> int:
    """Normalise a Lichess timestamp to epoch MILLISECONDS."""
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    return v if v >= MS_EPOCH_THRESHOLD else v * 1000


def normalize_game_result(value: Any) -> str:
    if value is None:
        return "draw"
    token = str(value).strip().lower()
    if token in ("white", "black"):
        return token
    return "draw"


def render_game_outcome(stored_result: Any, user_color: str) -> tuple:
    token = normalize_game_result(stored_result)
    if token == "draw":
        return "draw", "½-½"
    if token == user_color:
        return "win", ("1-0" if user_color == "white" else "0-1")
    return "loss", ("0-1" if user_color == "white" else "1-0")


def _setting_int(key: str, default: int) -> int:
    try:
        return int(str(get_setting(key, default)).strip())
    except (TypeError, ValueError):
        logger.warning("Setting %r is not an integer; falling back to %r", key, default)
        return default


def _setting_float(key: str, default: float) -> float:
    try:
        value = float(str(get_setting(key, default)).strip())
    except (TypeError, ValueError):
        logger.warning("Setting %r is not a float; falling back to %r", key, default)
        return default
    if math.isnan(value) or math.isinf(value):
        logger.warning("Setting %r is not finite; falling back to %r", key, default)
        return default
    return value


def migrate_played_at_units() -> int:
    with closing_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE games SET played_at = played_at * 1000 "
            "WHERE played_at IS NOT NULL AND played_at > 0 AND played_at < ?",
            (MS_EPOCH_THRESHOLD,),
        )
        changed = cursor.rowcount or 0
        conn.commit()
        return changed


app = FastAPI(title="Chess Opening Improvement Platform")

# --------------------------------------------------------------------------- #
# Validation & Error Rendering
# --------------------------------------------------------------------------- #

MAX_ERROR_ITEMS = 20
MAX_ERROR_DEPTH = 6
MAX_ERROR_STRING = 200
MAX_ERROR_INT = 10 ** 18

FALLBACK_VALIDATION_BODY = '{"detail": [{"type": "value_error", "msg": "Request could not be validated."}]}'


def _utf8_safe(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    if len(text) > MAX_ERROR_STRING:
        text = text[:MAX_ERROR_STRING] + "... (truncated)"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "replace").decode("utf-8", "replace")
    return text


def _json_safe(value, _depth: int = 0):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if -MAX_ERROR_INT < value < MAX_ERROR_INT:
            return value
        return f"<int with {value.bit_length()} bits>"
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, str):
        return _utf8_safe(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return _utf8_safe(raw[:MAX_ERROR_STRING].decode("utf-8", "replace")) + (
            f"... ({len(raw)} bytes)" if len(raw) > MAX_ERROR_STRING else ""
        )
    if _depth >= MAX_ERROR_DEPTH:
        return "..."
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ERROR_ITEMS:
                out["..."] = f"{len(value) - MAX_ERROR_ITEMS} more key(s)"
                break
            out[_utf8_safe(key) if isinstance(key, str) else _utf8_safe(repr(key))] = _json_safe(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [_json_safe(item, _depth + 1) for item in items[:MAX_ERROR_ITEMS]]
        if len(items) > MAX_ERROR_ITEMS:
            out.append(f"... {len(items) - MAX_ERROR_ITEMS} more item(s)")
        return out
    return _utf8_safe(repr(value))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    try:
        errors = exc.errors()
    except Exception:
        logger.exception("Could not extract validation errors for %s %s", request.method, request.url.path)
        errors = [{"type": "value_error", "msg": "Request could not be validated."}]
    try:
        payload = json.dumps({"detail": _json_safe(errors)}, allow_nan=False)
    except Exception:
        logger.exception("Could not render a 422 body for %s %s", request.method, request.url.path)
        payload = FALLBACK_VALIDATION_BODY
    return Response(content=payload.encode("utf-8", "replace"), status_code=422, media_type="application/json")


@app.exception_handler(ValueError)
async def value_error_handler(request, exc: ValueError):
    detail = str(exc)
    if "Out of range float values" in detail:
        return JSONResponse(status_code=422, content={"detail": "Invalid float value: NaN and Infinity are not allowed."})
    logger.exception("Unhandled ValueError while serving %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.exception_handler(RecursionError)
async def recursion_error_handler(request, exc: RecursionError):
    logger.warning("RecursionError while serving %s %s", request.method, request.url.path)
    return JSONResponse(status_code=400, content={"detail": "Request body is nested too deeply."})


@app.exception_handler(OverflowError)
async def overflow_error_handler(request, exc: OverflowError):
    logger.warning("OverflowError while serving %s %s", request.method, request.url.path)
    return JSONResponse(status_code=400, content={"detail": "Numeric value out of range."})


# --------------------------------------------------------------------------- #
# Local-Origin Security Boundary
# --------------------------------------------------------------------------- #

ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
ENGINE_BUSY_RETRY_AFTER = max(1, int(round(SYNC_POOL_WAIT_TIMEOUT)))

SECURITY_RESPONSE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'none'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; media-src 'self' blob:; "
        "connect-src 'self'; frame-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception):
    logger.exception("Unhandled %s while serving %s %s", type(exc).__name__, request.method, request.url.path)
    response = JSONResponse(status_code=500, content={"detail": "Internal server error."})
    for name, value in SECURITY_RESPONSE_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def _split_host_port(value: str):
    value = (value or "").strip().lower()
    if not value:
        return "", None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        host = value[1:end]
        rest = value[end + 1:]
        return host, (rest[1:] if rest.startswith(":") else None)
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        return host, (port or None)
    return value, None


def is_loopback_hostname(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if not host:
        return False
    if host in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _env_list(name: str) -> frozenset:
    raw = os.environ.get(name, "") or ""
    return frozenset(item.strip().rstrip("/").lower() for item in raw.split(",") if item.strip())


def is_allowed_local_origin(origin: str, host_header: str = "") -> bool:
    origin = (origin or "").strip().rstrip("/").lower()
    if not origin or origin == "null":
        return False
    if origin in _env_list("CHESS_ALLOWED_ORIGINS"):
        return True
    if not origin.startswith("http://"):
        return False
    host, port = _split_host_port(origin[len("http://"):])
    if port is not None and not port.isdigit():
        return False
    if is_loopback_hostname(host):
        return True
    return bool(host_header) and origin == "http://" + (host_header or "").strip().rstrip("/").lower()


def is_allowed_request_host(host_header: str) -> bool:
    host, _port = _split_host_port(host_header)
    if not host:
        return True
    if is_loopback_hostname(host):
        return True
    return host in _env_list("CHESS_ALLOWED_HOSTS")


def json_max_nesting_depth(data: bytes) -> int:
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for byte in data:
        char = chr(byte)
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > deepest:
                deepest = depth
        elif char in "]}":
            depth -= 1
    return deepest


def body_is_json(content_type: str, body: bytes) -> bool:
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        return True
    return body.lstrip()[:1] in (b"{", b"[")


def _text_has_unpaired_surrogate_escape(text: str) -> bool:
    index, end, pending_high = 0, len(text), False
    while index < end:
        if text[index] != "\\":
            index += 1
            continue
        if index + 1 >= end or text[index + 1] != "u":
            index += 2
            continue
        digits = text[index + 2:index + 6]
        if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
            index += 2
            continue
        code = int(digits, 16)
        index += 6
        if 0xD800 <= code <= 0xDBFF:
            if pending_high:
                return True
            pending_high = True
        elif 0xDC00 <= code <= 0xDFFF:
            if not pending_high:
                return True
            pending_high = False
        elif pending_high:
            return True
    return pending_high


_SURROGATE_BYTE_RANGE_RE = re.compile(b"[\xed][\xa0-\xbf][\x80-\xbf]")


def body_has_lone_surrogate(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return bool(_SURROGATE_BYTE_RANGE_RE.search(data))
    if "\\u" not in text and "\\U" not in text:
        return False
    return _text_has_unpaired_surrogate_escape(text)


class LocalBoundaryMiddleware:
    """Pure-ASGI guard: origin/CSRF boundary, body caps, security headers."""

    def __init__(self, app, max_body_bytes: int = MAX_BODY_BYTES, max_json_depth: int = MAX_JSON_DEPTH):
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_json_depth = max_json_depth

    @staticmethod
    async def _respond(send, status: int, detail: str, extra_headers=None):
        body = json.dumps({"detail": detail}).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        for name, value in (extra_headers or {}).items():
            headers.append((name.encode("latin-1"), value.encode("latin-1")))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = (scope.get("method") or "GET").upper()
        raw_headers = scope.get("headers") or []
        headers = {}
        for key, value in raw_headers:
            try:
                name = key.decode("latin-1").lower()
            except Exception:
                continue
            headers[name] = value.decode("latin-1")

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in SECURITY_RESPONSE_HEADERS.items():
                    if name not in response_headers:
                        response_headers[name] = value
            await send(message)

        host_header = headers.get("host", "")
        origin = headers.get("origin", "")
        fetch_site = (headers.get("sec-fetch-site") or "").strip().lower()
        browser_driven = bool(origin) or bool(fetch_site) or bool(headers.get("sec-fetch-mode"))

        if origin and not is_allowed_local_origin(origin, host_header):
            logger.warning("Refused cross-origin %s %s from Origin %r", method, scope.get("path"), origin)
            await self._respond(
                send_with_security_headers,
                403,
                "Cross-origin requests are not allowed by this local app.",
                {"Vary": "Origin"},
            )
            return

        is_oauth_callback_nav = (
            method == "GET"
            and scope.get("path") == "/api/auth/lichess/callback"
            and (headers.get("sec-fetch-mode") or "").strip().lower() in ("navigate", "")
            and not origin
        )
        if fetch_site and fetch_site not in ALLOWED_FETCH_SITES and not is_oauth_callback_nav:
            logger.warning("Refused %s %s: Sec-Fetch-Site %r", method, scope.get("path"), fetch_site)
            await self._respond(
                send_with_security_headers,
                403,
                "Cross-site requests are not allowed by this local app.",
            )
            return

        if browser_driven and not is_allowed_request_host(host_header):
            logger.warning("Refused %s %s: Host %r is not a loopback name", method, scope.get("path"), host_header)
            await self._respond(send_with_security_headers, 403, "This server only answers loopback requests.")
            return

        if method == "OPTIONS" and "access-control-request-method" in headers:
            if origin and is_allowed_local_origin(origin, host_header):
                requested = (headers.get("access-control-request-headers") or "").lower()
                allowed = sorted({h.strip() for h in requested.split(",") if h.strip()} | {"content-type", "accept"})
                await self._respond(
                    send_with_security_headers,
                    204,
                    "",
                    {
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS",
                        "Access-Control-Allow-Headers": ", ".join(allowed),
                        "Access-Control-Max-Age": "600",
                        "Vary": "Origin",
                    },
                )
                return
            await self._respond(
                send_with_security_headers,
                403,
                "Cross-origin requests are not allowed by this local app.",
                {"Vary": "Origin"},
            )
            return

        has_body = method in BODY_METHODS or "transfer-encoding" in headers or "content-length" in headers
        if has_body:
            declared = headers.get("content-length")
            if declared:
                try:
                    if int(declared) > self.max_body_bytes:
                        await self._respond(send_with_security_headers, 413, "Request body too large.")
                        return
                except ValueError:
                    await self._respond(send_with_security_headers, 400, "Invalid Content-Length header.")
                    return

            body = b""
            more = True
            while more:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                body += message.get("body") or b""
                more = bool(message.get("more_body"))
                if len(body) > self.max_body_bytes:
                    await self._respond(send_with_security_headers, 413, "Request body too large.")
                    return

            if body and body_is_json(headers.get("content-type"), body):
                depth = json_max_nesting_depth(body)
                if depth > self.max_json_depth:
                    await self._respond(
                        send_with_security_headers,
                        400,
                        f"Request body is nested too deeply (max {self.max_json_depth}).",
                    )
                    return
                if body_has_lone_surrogate(body):
                    await self._respond(
                        send_with_security_headers,
                        400,
                        "Request body contains an unpaired surrogate; it is not valid UTF-8 text.",
                    )
                    return

            replayed = [{"type": "http.request", "body": body, "more_body": False}]

            async def receive_replay():
                if replayed:
                    return replayed.pop(0)
                return {"type": "http.disconnect"}

            receive = receive_replay

        await self.app(scope, receive, send_with_security_headers)


app.add_middleware(LocalBoundaryMiddleware)


@app.middleware("http")
async def add_no_cache_header(request, call_next):
    response = await call_next(request)
    if request.url.path.endswith((".js", ".css", ".html")) or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.on_event("startup")
def startup_event():
    init_db()
    try:
        rewritten = migrate_played_at_units()
        if rewritten:
            logger.info("Normalised played_at unit for %d game row(s) to milliseconds", rewritten)
    except Exception:
        logger.exception("played_at unit migration failed")

    try:
        OpeningBook.get_instance()
    except Exception:
        logger.exception("OpeningBook warm-up failed")


@app.on_event("shutdown")
async def shutdown_event():
    LivePlayManager.get_instance().shutdown()
    evaluator = AsyncEngineEvaluator.get_instance()
    await evaluator.close()
    try:
        get_sync_engine_pool().close_all()
    except Exception:
        logger.exception("Closing the sync engine pool failed")


def _enforce_fair_play() -> None:
    """Reject repertoire/explorer/engine queries while a live Lichess game is active."""
    if LivePlayManager.get_instance().is_live_game_active():
        raise HTTPException(
            status_code=409,
            detail="Fair-Play Lock: Repertoire and engine tools are disabled during an active live game.",
        )


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #

MAX_LICHESS_TOKEN_LEN = 512
MAX_USERNAME_LEN = 64
MAX_STUDY_NAME_LEN = 120
MAX_DESCRIPTION_LEN = 2_000
MAX_COMMENT_LEN = 4_000
MAX_ARROWS_LEN = 4_000
MAX_FEN_LEN = 120
MAX_UCI_LEN = 8
MAX_LINE_PLIES = 200
MAX_TREE_LINES = 500
MAX_OPENING_NAME_LEN = 200
MAX_OPENING_INFO_MOVES = 200
MAX_PGN_TEXT_LEN = 1_500_000


class SettingsPayload(BaseModel):
    lichess_token: Optional[str] = Field(None, max_length=MAX_LICHESS_TOKEN_LEN)
    lichess_username: Optional[str] = Field(None, max_length=MAX_USERNAME_LEN)
    max_opening_ply: Optional[int] = Field(None, ge=1, le=100)
    blunder_threshold_cp: Optional[int] = Field(None, ge=1, le=2000)
    gap_threshold: Optional[float] = Field(None, ge=0.1, le=100.0, allow_inf_nan=False)
    sync_perf_types: Optional[str] = Field(None, max_length=120)
    analysis_depth: Optional[int] = Field(None, ge=MIN_ANALYSIS_DEPTH, le=MAX_ANALYSIS_DEPTH)


class PlaySeekPayload(BaseModel):
    minutes: Union[int, float] = Field(10, ge=0.5, le=180)
    increment: int = Field(0, ge=0, le=180)
    rated: bool = True
    color: Literal["white", "black", "random"] = "random"
    rating_range: Optional[str] = Field(None, max_length=32)

    @field_validator("rating_range")
    @classmethod
    def validate_rating_range(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        parts = v.split("-")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError("rating_range must be formatted as 'min-max' (e.g. '1945-2045')")
        min_r, max_r = int(parts[0]), int(parts[1])
        if min_r < 400 or max_r > 3500 or min_r > max_r:
            raise ValueError("rating_range must be between 400 and 3500 with min <= max")
        return f"{min_r}-{max_r}"


class PlayChallengePayload(BaseModel):
    mode: Literal["ai", "user"] = "ai"
    ai_level: int = Field(5, ge=1, le=8)
    username: Optional[str] = Field("", max_length=MAX_USERNAME_LEN)
    clock_limit_sec: int = Field(600, ge=30, le=10800)
    increment_sec: int = Field(0, ge=0, le=180)
    rated: bool = False
    color: Literal["white", "black", "random"] = "random"


class PlayChallengeRespondPayload(BaseModel):
    challenge_id: str = Field(..., min_length=1, max_length=64)
    accept: bool = True


class PlayMovePayload(BaseModel):
    game_id: str = Field(..., min_length=1, max_length=64)
    move_uci: str = Field(..., min_length=4, max_length=MAX_UCI_LEN)
    offer_draw: bool = False


class PlayActionPayload(BaseModel):
    game_id: str = Field(..., min_length=1, max_length=64)
    action: Literal["abort", "resign", "draw_offer", "draw_accept", "draw_decline", "claim_victory", "dismiss"]


class SequencePreviewPayload(BaseModel):
    repertoire: Literal["white", "black"]
    moves: List[Dict[str, Any]] = Field(..., max_length=MAX_LINE_PLIES)


class SequenceSavePayload(BaseModel):
    repertoire: Literal["white", "black"]
    moves: List[Dict[str, Any]] = Field(..., max_length=MAX_LINE_PLIES)
    study_name: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)
    replace_conflicts: bool = False
    prioritize_for_training: bool = True
    note: Optional[str] = Field(None, max_length=MAX_COMMENT_LEN)


class LineImportPayload(BaseModel):
    repertoire: Literal["white", "black"]
    moves: List[Dict[str, Any]] = Field(..., max_length=MAX_LINE_PLIES)
    study_name: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)


class CheckLinePayload(BaseModel):
    repertoire: Literal["white", "black"] = "white"
    moves: List[Dict[str, Any]] = Field(default_factory=list, max_length=MAX_LINE_PLIES)


class CheckTreePayload(BaseModel):
    repertoire: Literal["white", "black"] = "white"
    lines: List[Dict[str, Any]] = Field(default_factory=list, max_length=MAX_TREE_LINES)


class RepertoireMovePayload(BaseModel):
    repertoire: Literal["white", "black"]
    parent_fen: str = Field(..., max_length=MAX_FEN_LEN)
    move_uci: str = Field(..., max_length=MAX_UCI_LEN)
    is_user_move: Optional[bool] = True
    popularity_pct: Optional[float] = Field(0.0, ge=0.0, le=100.0, allow_inf_nan=False)
    win_rate: Optional[float] = Field(0.0, ge=0.0, le=100.0, allow_inf_nan=False)
    eval_score: Optional[int] = Field(0, ge=-100000, le=100000)
    is_important: Optional[bool] = None
    comment: Optional[str] = Field(None, max_length=MAX_COMMENT_LEN)
    arrows: Optional[str] = Field(None, max_length=MAX_ARROWS_LEN)
    study_name: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)
    replace_existing: Optional[bool] = False


class UpdateMoveMetaPayload(BaseModel):
    repertoire: Literal["white", "black"]
    parent_fen: str = Field(..., max_length=MAX_FEN_LEN)
    move_uci: str = Field(..., max_length=MAX_UCI_LEN)
    is_important: Optional[bool] = None
    comment: Optional[str] = Field(None, max_length=MAX_COMMENT_LEN)
    arrows: Optional[str] = Field(None, max_length=MAX_ARROWS_LEN)
    study_name: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)


class DeleteMovePayload(BaseModel):
    repertoire: Literal["white", "black"]
    parent_fen: str = Field(..., max_length=MAX_FEN_LEN)
    move_uci: str = Field(..., max_length=MAX_UCI_LEN)


class TrainerMovePayload(BaseModel):
    repertoire: Literal["white", "black"]
    current_fen: str = Field(..., max_length=MAX_FEN_LEN)
    attempted_uci: str = Field(..., max_length=MAX_UCI_LEN)
    study_name: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)
    opening_filter: Optional[str] = Field(None, max_length=MAX_OPENING_NAME_LEN)
    is_retry: Optional[bool] = False
    response_ms: Optional[int] = Field(None, ge=0, le=3_600_000)
    game_id: Optional[str] = Field(None, max_length=64)
    is_deviation: Optional[bool] = False
    played_uci: Optional[str] = Field(None, max_length=MAX_UCI_LEN)


class ResolveDeviationPayload(BaseModel):
    game_id: Optional[str] = Field(None, max_length=64)
    fen_before: Optional[str] = Field(None, max_length=MAX_FEN_LEN)
    played_uci: Optional[str] = Field(None, max_length=MAX_UCI_LEN)
    prepared_uci: Optional[str] = Field(None, max_length=MAX_UCI_LEN)
    user_color: Optional[str] = Field("white", pattern="^(white|black)$")


class StudyCreatePayload(BaseModel):
    name: str = Field(..., max_length=MAX_STUDY_NAME_LEN)
    repertoire: Literal["white", "black"]
    description: Optional[str] = Field("", max_length=MAX_DESCRIPTION_LEN)


class GroupSubtreePayload(BaseModel):
    repertoire: Literal["white", "black"]
    start_fen: str = Field(..., max_length=MAX_FEN_LEN)
    study_name: str = Field(..., min_length=1, max_length=MAX_STUDY_NAME_LEN)
    include_path_from_root: Optional[bool] = True
    path_uci: Optional[List[str]] = None


class PgnPreviewPayload(BaseModel):
    pgn_text: str = Field(..., max_length=MAX_PGN_TEXT_LEN)


class PgnImportPayload(BaseModel):
    pgn_text: str = Field(..., max_length=MAX_PGN_TEXT_LEN)
    repertoire: Literal["white", "black", "auto"] = "auto"
    study_name: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)
    split_chapters_as_studies: bool = False
    replace_conflicts: bool = True
    clean_repertoire: bool = False


class ImportGameOpeningRequest(BaseModel):
    game_id: str = Field(..., max_length=32)
    repertoire: Literal["white", "black"]
    max_ply: Optional[int] = Field(None, ge=1, le=100)
    replace_existing: Optional[bool] = False


# --------------------------------------------------------------------------- #
# Account & Settings Endpoints
# --------------------------------------------------------------------------- #


@app.get("/api/account")
def get_account():
    client = LichessClient()
    profile = client.get_account_profile()
    username = get_setting("lichess_username") or (profile.get("username") if profile else None)
    return {
        "connected": profile is not None,
        "username": username,
        "perfs": profile.get("perfs", {}) if profile else {},
        "token_present": bool(client.token),
    }


@app.get("/api/settings")
def get_settings():
    return {
        "lichess_username": get_setting("lichess_username", ""),
        "has_token": bool(get_setting("lichess_token") or LichessClient().token),
        "max_opening_ply": _setting_int("max_opening_ply", 24),
        "blunder_threshold_cp": _setting_int("blunder_threshold_cp", 150),
        "gap_threshold": _setting_float("gap_threshold", 5.0),
        "sync_perf_types": get_setting("sync_perf_types", DEFAULT_PERF_TYPES),
        "analysis_depth": _setting_int("analysis_depth", DEFAULT_ANALYSIS_DEPTH),
    }


@app.post("/api/settings")
def update_settings(payload: SettingsPayload):
    if payload.lichess_token is not None:
        set_setting("lichess_token", payload.lichess_token.strip())
    if payload.lichess_username is not None:
        set_setting("lichess_username", payload.lichess_username.strip())
    if payload.max_opening_ply is not None:
        set_setting("max_opening_ply", str(payload.max_opening_ply))
    if payload.blunder_threshold_cp is not None:
        set_setting("blunder_threshold_cp", str(payload.blunder_threshold_cp))
    if payload.gap_threshold is not None:
        set_setting("gap_threshold", str(payload.gap_threshold))
    if payload.sync_perf_types is not None:
        set_setting("sync_perf_types", payload.sync_perf_types.strip())
    if payload.analysis_depth is not None:
        set_setting("analysis_depth", str(payload.analysis_depth))
    return {"status": "ok", "message": "Settings saved."}


# --------------------------------------------------------------------------- #
# Games & Sync Endpoints
# --------------------------------------------------------------------------- #


@app.get("/api/games/recent")
def get_recent_games(
    color: Optional[str] = Query(None, pattern="^(white|black)$"),
    speed: Optional[str] = Query(None, max_length=32),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None, max_length=120),
):
    with closing_db() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM games"
        conditions = []
        params: List[Any] = []

        if color:
            conditions.append("user_color = ?")
            params.append(color)
        if speed and speed.strip():
            conditions.append("LOWER(speed) = ?")
            params.append(speed.strip().lower())
        if search and search.strip():
            q = f"%{search.strip()}%"
            conditions.append("(white_player LIKE ? OR black_player LIKE ? OR opening_name LIKE ? OR eco LIKE ?)")
            params.extend([q, q, q, q])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        cursor.execute(f"SELECT COUNT(*) as cnt FROM ({query})", params)
        total_count = cursor.fetchone()["cnt"]

        query += (
            f" ORDER BY CASE WHEN played_at > 0 AND played_at < {MS_EPOCH_THRESHOLD} "
            "THEN played_at * 1000 ELSE played_at END DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()

    now = time.time()
    opening_book = OpeningBook.get_instance()
    games = []

    for r in rows:
        row = dict(r)
        played_at_ms = normalize_played_at(row.get("played_at"))
        played_at_sec = played_at_ms / 1000.0

        diff = now - played_at_sec
        if diff < 60:
            formatted_date = "Just now"
        elif diff < 3600:
            formatted_date = f"{int(diff // 60)}m ago"
        elif diff < 86400:
            formatted_date = f"{int(diff // 3600)}h ago"
        elif diff < 172800:
            formatted_date = "Yesterday"
        else:
            formatted_date = time.strftime("%b %d, %Y", time.localtime(played_at_sec)) if played_at_sec > 0 else "Unknown"

        user_color = row.get("user_color") or "white"
        result, score_text = render_game_outcome(row.get("result"), user_color)

        if user_color == "white":
            opponent_name = row.get("black_player") or "Opponent"
            opponent_rating = row.get("black_rating") or 0
            user_rating = row.get("white_rating") or 0
        else:
            opponent_name = row.get("white_player") or "Opponent"
            opponent_rating = row.get("white_rating") or 0
            user_rating = row.get("black_rating") or 0

        moves_san = row.get("moves_san") or ""
        tokens = [m for m in moves_san.strip().split() if m and not m.endswith(".") and not m[0].isdigit()]

        opening_name = row.get("opening_name") or ""
        eco = row.get("eco") or ""
        opening_ply = 0

        if tokens:
            ident = opening_book.identify_game_opening(tokens)
            opening_ply = ident.get("ply", 0)
            if not opening_name or opening_name == "Standard Chess Opening":
                opening_name = ident.get("name") or opening_name
            if not eco or eco == "A00":
                eco = ident.get("eco") or eco

        games.append({
            "id": row.get("id"),
            "speed": row.get("speed") or "rapid",
            "rated": bool(row.get("rated")),
            "played_at": played_at_ms,
            "played_at_formatted": formatted_date,
            "user_color": user_color,
            "user_rating": user_rating,
            "opponent_name": opponent_name,
            "opponent_rating": opponent_rating,
            "result": result,
            "score_text": score_text,
            "opening_name": opening_name or "Standard Opening",
            "eco": eco or "",
            "moves_san": moves_san,
            "total_plies": len(tokens),
            "opening_ply": opening_ply or min(len(tokens), 12),
            "analysis_depth": int(row.get("analysis_depth") or 0),
            "white_accuracy": row.get("white_accuracy"),
            "black_accuracy": row.get("black_accuracy"),
            "game_url": f"https://lichess.org/{row.get('id')}",
        })

    return {"games": games, "total": total_count, "limit": limit, "offset": offset}


@app.post("/api/games/sync_recent")
def sync_recent_games_endpoint(
    max_games: int = Query(20, ge=1, le=50),
    speeds: Optional[str] = Query(None, max_length=120),
):
    rm = RepertoireManager.get_instance()
    if speeds:
        return rm.sync_recent_games(max_games=max_games, perf_type=speeds)
    return rm.sync_recent_games(max_games=max_games)


@app.get("/api/games/sync_recent_stream")
async def stream_sync_recent_games_endpoint(
    request: Request,
    max_games: int = Query(30, ge=1, le=100),
    speeds: Optional[str] = Query(None, max_length=120),
):
    rm = RepertoireManager.get_instance()
    gen = rm.stream_sync_recent_games(max_games=max_games, request=request, perf_type=speeds)
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/repertoire/import_game_opening")
def import_game_opening_endpoint(payload: ImportGameOpeningRequest):
    rm = RepertoireManager.get_instance()
    try:
        return rm.import_game_opening(
            payload.game_id,
            payload.repertoire,
            payload.max_ply,
            replace_existing=bool(payload.replace_existing),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/repertoire/sync_games")
def sync_repertoire_games(
    max_games: int = Query(30, ge=1, le=100),
    speeds: Optional[str] = Query(None, max_length=120),
):
    rm = RepertoireManager.get_instance()
    return rm.analyze_user_games(max_games=max_games, max_ply=30, perf_type=speeds)


@app.get("/api/repertoire/sync_games_stream")
async def stream_sync_repertoire_games(
    request: Request,
    max_games: int = Query(30, ge=1, le=100),
    speeds: Optional[str] = Query(None, max_length=120),
):
    rm = RepertoireManager.get_instance()
    gen = rm.stream_analyze_user_games(max_games=max_games, max_ply=30, request=request, perf_type=speeds)
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Repertoire Builder Endpoints
# --------------------------------------------------------------------------- #


@app.get("/api/repertoire/explore")
def explore_position_2000(
    fen: str = Query(...),
    ratings: str = Query("2000,2200,2500"),
    color: Literal["white", "black"] = Query("white"),
):
    _enforce_fair_play()
    if not is_valid_fen(fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    return RepertoireManager.get_instance().get_candidate_moves_2000(fen, ratings=ratings, color=color)


@app.get("/api/repertoire/tree")
def get_repertoire_tree(
    color: Literal["white", "black"] = Query("white"),
    threshold: float = Query(5.0, ge=0.0, le=100.0),
    min_games: int = Query(30, ge=0),
    include_patched: bool = Query(False),
):
    _enforce_fair_play()
    return RepertoireManager.get_instance().get_repertoire_tree(
        color, gap_threshold=threshold, min_games=min_games, include_patched=include_patched
    )


@app.post("/api/repertoire/move")
def add_repertoire_move(payload: RepertoireMovePayload):
    if not is_valid_fen(payload.parent_fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    res = RepertoireManager.get_instance().add_repertoire_move(
        payload.repertoire,
        payload.parent_fen,
        payload.move_uci,
        is_user_move=payload.is_user_move,
        popularity_pct=payload.popularity_pct,
        win_rate=payload.win_rate,
        eval_score=payload.eval_score,
        is_important=payload.is_important,
        comment=payload.comment,
        arrows=payload.arrows,
        study_name=payload.study_name,
        replace_existing=bool(payload.replace_existing),
    )
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message", "Error adding move"))
    return res


@app.post("/api/repertoire/move_meta")
def update_move_meta(payload: UpdateMoveMetaPayload):
    if not is_valid_fen(payload.parent_fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    res = RepertoireManager.get_instance().update_move_metadata(
        repertoire=payload.repertoire,
        parent_fen=payload.parent_fen,
        move_uci=payload.move_uci,
        is_important=payload.is_important,
        comment=payload.comment,
        arrows=payload.arrows,
        study_name=payload.study_name,
    )
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message", "Error updating move metadata"))
    return res


@app.delete("/api/repertoire/move")
def delete_repertoire_move(payload: DeleteMovePayload):
    if not is_valid_fen(payload.parent_fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    RepertoireManager.get_instance().delete_repertoire_branch(payload.repertoire, payload.parent_fen, payload.move_uci)
    return {"status": "ok", "message": "Branch deleted."}


@app.get("/api/repertoire/studies")
def get_repertoire_studies(repertoire: Optional[str] = Query(None)):
    return RepertoireManager.get_instance().get_studies(repertoire)


@app.post("/api/repertoire/studies")
def create_repertoire_study(payload: StudyCreatePayload):
    res = RepertoireManager.get_instance().create_study(payload.name, payload.repertoire, payload.description or "")
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message", "Error creating study"))
    return res


@app.delete("/api/repertoire/studies/{study_id}")
def delete_repertoire_study(
    study_id: int = Path(..., ge=SQLITE_INT64_MIN, le=SQLITE_INT64_MAX),
    delete_moves: bool = Query(False),
):
    res = RepertoireManager.get_instance().delete_study(study_id, delete_moves=delete_moves)
    if res.get("status") == "error":
        raise HTTPException(status_code=404, detail=res.get("message", "Study not found"))
    return res


@app.post("/api/repertoire/studies/group_subtree")
def group_repertoire_subtree(payload: GroupSubtreePayload):
    if not is_valid_fen(payload.start_fen):
        raise HTTPException(status_code=400, detail="Invalid start FEN position")
    res = RepertoireManager.get_instance().group_subtree_into_study(
        repertoire=payload.repertoire,
        start_fen=payload.start_fen,
        study_name=payload.study_name,
        include_path_from_root=bool(payload.include_path_from_root),
        path_uci=payload.path_uci,
    )
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message", "Error grouping subtree"))
    return res


@app.post("/api/repertoire/import_line")
def repertoire_import_line(payload: LineImportPayload):
    try:
        return RepertoireManager.get_instance().import_line_to_repertoire(
            repertoire=payload.repertoire,
            moves=payload.moves,
            study_name=payload.study_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/repertoire/check_line")
def repertoire_check_line(payload: CheckLinePayload):
    return RepertoireManager.get_instance().check_line_overlap(repertoire=payload.repertoire, moves=payload.moves)


@app.post("/api/repertoire/check_tree")
def repertoire_check_tree(payload: CheckTreePayload):
    total_plies = sum(len(line.get("moves") or []) for line in payload.lines if isinstance(line, dict))
    if total_plies > 10_000:
        raise HTTPException(status_code=400, detail="Too many moves in one tree check.")
    return RepertoireManager.get_instance().check_lines_overlap(repertoire=payload.repertoire, lines=payload.lines)


def _count_repertoire_moves() -> int:
    with closing_db() as conn:
        return conn.execute("SELECT COUNT(*) as cnt FROM repertoire_moves").fetchone()["cnt"]


@app.post("/api/repertoire/seed")
def seed_default_repertoires(force: bool = Query(False)):
    rm = RepertoireManager.get_instance()
    before = _count_repertoire_moves()
    try:
        result = rm.seed_default_repertoires(force=force)
    except Exception:
        logger.exception("Seeding default repertoires failed")
        raise HTTPException(status_code=500, detail="Seeding failed unexpectedly. The repertoire may be incomplete.")

    after = _count_repertoire_moves()
    seeded = after - before
    payload: Dict[str, Any] = dict(result) if isinstance(result, dict) else {}
    status = str(payload.get("status") or "").strip().lower()

    if status == "skipped":
        return JSONResponse(
            status_code=409,
            content={
                **payload,
                "status": "skipped",
                "message": payload.get("message") or "Repertoire already contains moves.",
                "seeded_moves": 0,
                "total_moves": after,
                "force_required": True,
            },
        )
    if status in ("ok", "success", "seeded"):
        return {**payload, "status": "ok", "seeded_moves": seeded, "total_moves": after}
    if seeded > 0:
        logger.warning("seed_default_repertoires returned %r; reporting verified post-condition (%d moves added)", type(result).__name__, seeded)
        return {"status": "ok", "message": "Default repertoires seeded.", "seeded_moves": seeded, "total_moves": after}
    logger.error("seed_default_repertoires returned %r and wrote no moves", type(result).__name__)
    raise HTTPException(status_code=500, detail="Seeding did not complete. The repertoire was not changed.")


@app.post("/api/repertoire/clear")
def clear_repertoire(repertoire: Optional[str] = Query(None)):
    clean_rep = repertoire.lower().strip() if repertoire else None
    if clean_rep and clean_rep not in ("white", "black"):
        raise HTTPException(status_code=400, detail="Invalid repertoire color. Expected 'white', 'black', or omit for all.")
    deleted_count = RepertoireManager.get_instance().clear_repertoire_database(clean_rep)
    scope = f"{clean_rep.capitalize()} repertoire" if clean_rep else "Entire repertoire database"
    return {"status": "ok", "message": f"{scope} reset.", "deleted_count": deleted_count}


@app.post("/api/repertoire/preview_pgn")
def repertoire_preview_pgn(payload: PgnPreviewPayload):
    try:
        return RepertoireManager.get_instance().preview_pgn_repertoire(payload.pgn_text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("PGN preview parsing failed")
        raise HTTPException(status_code=500, detail="Could not parse PGN preview.")


@app.post("/api/repertoire/import_pgn")
def repertoire_import_pgn(payload: PgnImportPayload):
    try:
        return RepertoireManager.get_instance().import_pgn_repertoire(
            pgn_text=payload.pgn_text,
            repertoire=payload.repertoire,
            study_name=payload.study_name,
            split_chapters_as_studies=payload.split_chapters_as_studies,
            replace_conflicts=payload.replace_conflicts,
            clean_repertoire=payload.clean_repertoire,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("PGN repertoire import failed")
        raise HTTPException(status_code=500, detail="Could not import PGN repertoire.")


@app.get("/api/repertoire/game_gaps")
def get_repertoire_game_gaps(
    color: Literal["white", "black"] = Query("white"),
    min_games: int = Query(30, ge=0),
    include_patched: bool = Query(False),
):
    return RepertoireManager.get_instance().get_game_gaps(color, min_games=min_games, include_patched=include_patched)


@app.get("/api/repertoire/eval_stream")
async def stream_position_eval(
    request: Request,
    fen: str = Query(...),
    depth: int = Query(16, ge=1, le=30),
    multipv: int = Query(1, ge=1, le=5),
):
    _enforce_fair_play()
    if not is_valid_fen(fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    evaluator = AsyncEngineEvaluator.get_instance()
    return StreamingResponse(
        evaluator.stream_eval(fen, max_depth=depth, request=request, multipv=multipv),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/api/repertoire/opening_info")
def get_opening_info(fen: str = Query(...), moves: str = Query("")):
    if not is_valid_fen(fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    line = [t for t in re.split(r"[\s,]+", moves or "") if t][:MAX_OPENING_INFO_MOVES]
    return OpeningBook.get_instance().get_opening_context(fen, line)


# --------------------------------------------------------------------------- #
# Repertoire Trainer & Deviations Endpoints
# --------------------------------------------------------------------------- #


@app.get("/api/repertoire/train/next")
def get_next_repertoire_drill(
    color: Literal["white", "black", "mixed", "both", "all"] = Query("mixed"),
    opening: Optional[str] = Query(None),
    mode: Literal[
        "adaptive",
        "due_only",
        "frontier_only",
        "weak_only",
        "mastered_audit",
        "full",
        "lapses_only",
        "deviations",
    ] = Query("adaptive"),
    lapses: Optional[str] = Query(None),
    recent_ids: Optional[str] = Query(None),
    index: Optional[int] = Query(None, ge=0, le=10000),
    game_id: Optional[str] = Query(None, max_length=64),
):
    _enforce_fair_play()
    if mode == "deviations":
        recent_list = [x.strip() for x in (recent_ids or "").split(",") if x.strip()]
        dev_color = None if color in ("mixed", "both", "all") else color
        drill = get_deviation_drill(color=dev_color, game_id=game_id, recent_ids=recent_list, index=index)
        if not drill:
            return {"all_deviations_completed": True, "message": "No real game deviations found for this repertoire."}
        return drill

    parsed_lapses = [int(x.strip()) for x in (lapses or "").split(",") if x.strip().isdigit()]
    parsed_recent = [int(x.strip()) for x in (recent_ids or "").split(",") if x.strip().isdigit()]

    drill = RepertoireManager.get_instance().get_next_trainer_drill(
        color,
        opening_filter=opening,
        session_mode=mode,
        recent_session_lapses=parsed_lapses,
        recent_drilled_ids=parsed_recent,
    )
    if not drill:
        if opening:
            raise HTTPException(status_code=404, detail=f"No prepared moves found for {color} in '{opening}'.")
        raise HTTPException(status_code=404, detail="No repertoire moves configured yet for this color.")
    return drill


@app.post("/api/repertoire/train/attempt")
def evaluate_repertoire_attempt(payload: TrainerMovePayload):
    _enforce_fair_play()
    if not is_valid_fen(payload.current_fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    filter_val = payload.study_name or payload.opening_filter
    result = RepertoireManager.get_instance().evaluate_trainer_move(
        payload.repertoire,
        payload.current_fen,
        payload.attempted_uci,
        study_filter=filter_val,
        is_retry=bool(payload.is_retry),
        response_ms=payload.response_ms,
    )
    if (payload.is_deviation or payload.game_id) and result.get("correct") and not payload.is_retry:
        mark_deviation_resolved(
            game_id=payload.game_id,
            fen_before=payload.current_fen,
            played_uci=payload.played_uci or "",
            prepared_uci=result.get("expected_uci") or payload.attempted_uci,
            user_color=payload.repertoire,
        )
        result["deviation_resolved"] = True
    return result


@app.get("/api/repertoire/train/openings")
def get_trainer_openings(color: Optional[str] = Query(None)):
    return RepertoireManager.get_instance().get_repertoire_openings(color)


@app.get("/api/repertoire/train/progress")
def get_trainer_progress_endpoint(
    color: Literal["white", "black", "mixed", "both", "all"] = Query("mixed"),
    study: Optional[str] = Query(None),
):
    return RepertoireManager.get_instance().get_trainer_progress(color, study_name=study)


@app.post("/api/repertoire/train/recent_opening")
def set_recent_learned_opening(payload: Dict[str, Any]):
    name = (payload.get("name") or payload.get("opening_name") or "").strip()
    eco = (payload.get("eco") or "").strip()
    if len(name) > MAX_OPENING_NAME_LEN:
        raise HTTPException(status_code=400, detail=f"Opening name is too long (max {MAX_OPENING_NAME_LEN} characters).")
    if len(eco) > 8:
        raise HTTPException(status_code=400, detail="ECO code is too long (max 8 characters).")
    if name:
        set_setting("recent_learned_opening", json.dumps({"name": name, "eco": eco}))
        return {"status": "success", "recent_opening": {"name": name, "eco": eco}}
    return {"status": "error", "message": "Missing opening name"}


@app.get("/api/repertoire/deviations")
def get_repertoire_deviations_endpoint(
    color: Optional[str] = Query(None, pattern="^(white|black)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None, max_length=120),
):
    return find_game_deviations(color=color, limit=limit, offset=offset, search=search)


@app.get("/api/repertoire/train/deviation_drill")
def get_trainer_deviation_drill_endpoint(
    color: Literal["white", "black", "mixed", "both", "all"] = Query("white"),
    game_id: Optional[str] = Query(None, max_length=64),
    index: Optional[int] = Query(None, ge=0, le=10000),
):
    dev_color = None if color in ("mixed", "both", "all") else color
    drill = get_deviation_drill(color=dev_color, game_id=game_id, index=index)
    if not drill:
        raise HTTPException(status_code=404, detail="No deviation found for this color/game.")
    return drill


@app.post("/api/repertoire/deviations/resolve")
def resolve_repertoire_deviation_endpoint(payload: ResolveDeviationPayload):
    success = mark_deviation_resolved(
        game_id=payload.game_id,
        fen_before=payload.fen_before or "",
        played_uci=payload.played_uci or "",
        prepared_uci=payload.prepared_uci or "",
        user_color=payload.user_color or "white",
    )
    res = find_game_deviations(color=payload.user_color)
    return {
        "status": "success" if success else "ignored",
        "resolved": success,
        "remaining_deviations": res["total_deviations"],
        "compliance_pct": res["compliance_pct"],
    }


@app.post("/api/repertoire/deviations/reset")
def reset_repertoire_deviations_endpoint():
    reset_resolved_deviations()
    return {"status": "success", "message": "All resolved deviations reset."}


# --------------------------------------------------------------------------- #
# Lichess OAuth2 PKCE & Live Board API Endpoints
# --------------------------------------------------------------------------- #


@app.get("/api/auth/lichess/status")
def get_lichess_auth_status():
    return inspect_token_capabilities()


@app.post("/api/auth/lichess/start")
def start_lichess_oauth(request: Request):
    host = request.headers.get("host") or "127.0.0.1:38281"
    redirect_uri = f"http://{host}/api/auth/lichess/callback"
    return start_oauth_pkce(redirect_uri=redirect_uri)


@app.get("/api/auth/lichess/callback")
def lichess_oauth_callback(code: str = Query(""), state: str = Query(""), error: str = Query("")):
    if error:
        ok, msg, username = False, f"Lichess authorization canceled ({error}).", None
    else:
        ok, msg, username = complete_oauth_pkce(code=code, state=state)

    safe_title = "Connected to Lichess" if ok else "Lichess Connection Failed"
    accent = "#34d399" if ok else "#ef4444"
    safe_user = re.sub(r"[^A-Za-z0-9_\-]", "", username or "")
    safe_msg = (
        f"Signed in as {safe_user}. Return to Repertoire Studio."
        if ok and safe_user
        else re.sub(r"[<>\"'&]", "", msg)[:180]
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{safe_title}</title>
<style>
body {{ background:#0b0d11; color:#eef2f7; font-family:system-ui,sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
.card {{ background:#151922; border:1px solid rgba(255,255,255,0.12); border-radius:10px; padding:2rem 2.4rem; text-align:center; max-width:420px; }}
h2 {{ color:{accent}; margin:0 0 0.6rem; font-size:1.2rem; }}
p {{ color:#98a3b3; margin:0 0 1.2rem; font-size:0.92rem; }}
a {{ display:inline-block; background:#d4a853; color:#0b0d11; font-weight:700; text-decoration:none; padding:0.5rem 1.1rem; border-radius:6px; }}
</style></head>
<body><div class="card"><h2>{safe_title}</h2><p>{safe_msg}</p><a href="/">Open Studio</a></div></body>
</html>"""
    return HTMLResponse(content=html, status_code=200 if ok else 400)


@app.get("/api/play/state")
def get_live_play_state():
    return LivePlayManager.get_instance().snapshot()


@app.get("/api/play/stream")
async def stream_live_play(request: Request):
    mgr = LivePlayManager.get_instance()
    return StreamingResponse(
        mgr.stream_events(request=request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/play/seek")
def start_live_seek(payload: PlaySeekPayload):
    try:
        return LivePlayManager.get_instance().start_seek(
            minutes=payload.minutes,
            increment=payload.increment,
            rated=payload.rated,
            color=payload.color,
            rating_range=payload.rating_range,
        )
    except LivePlayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@app.post("/api/play/seek/cancel")
def cancel_live_seek():
    return LivePlayManager.get_instance().cancel_seek()


@app.post("/api/play/challenge")
def create_live_challenge(payload: PlayChallengePayload):
    try:
        return LivePlayManager.get_instance().create_challenge(
            mode=payload.mode,
            ai_level=payload.ai_level,
            username=payload.username or "",
            clock_limit_sec=payload.clock_limit_sec,
            increment_sec=payload.increment_sec,
            rated=payload.rated,
            color=payload.color,
        )
    except LivePlayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@app.post("/api/play/challenge/respond")
def respond_live_challenge(payload: PlayChallengeRespondPayload):
    try:
        return LivePlayManager.get_instance().respond_challenge(
            challenge_id=payload.challenge_id,
            accept=payload.accept,
        )
    except LivePlayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@app.post("/api/play/move")
def submit_live_move(payload: PlayMovePayload):
    try:
        return LivePlayManager.get_instance().submit_move(
            game_id=payload.game_id,
            move_uci=payload.move_uci,
            offer_draw=payload.offer_draw,
        )
    except LivePlayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@app.post("/api/play/action")
def submit_live_action(payload: PlayActionPayload):
    try:
        return LivePlayManager.get_instance().game_action(
            game_id=payload.game_id,
            action=payload.action,
        )
    except LivePlayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


# --------------------------------------------------------------------------- #
# Full-Game Stockfish 19 Analysis & Guided Repertoire Sequence Endpoints
# --------------------------------------------------------------------------- #


@app.get("/api/analysis/{game_id}")
def get_game_analysis(
    game_id: str = Path(..., min_length=1, max_length=64),
    depth: Optional[int] = Query(None, ge=MIN_ANALYSIS_DEPTH, le=MAX_ANALYSIS_DEPTH),
    force: bool = Query(False),
    cached_only: bool = Query(False),
):
    _enforce_fair_play()
    if cached_only:
        with closing_db() as conn:
            row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Game not found.")
        g = dict(row)
        if g.get("analysis_json"):
            try:
                parsed = json.loads(g["analysis_json"])
                if isinstance(parsed, dict):
                    parsed["cached"] = True
                    return {"status": "ready", "analysis": parsed}
            except Exception:
                pass
        return {
            "status": "pending",
            "game": {
                "id": g["id"],
                "white_player": g.get("white_player") or "White",
                "black_player": g.get("black_player") or "Black",
                "white_rating": g.get("white_rating") or 0,
                "black_rating": g.get("black_rating") or 0,
                "user_color": g.get("user_color") or "white",
                "speed": g.get("speed") or "rapid",
                "rated": bool(g.get("rated")),
                "result": g.get("result") or "draw",
                "opening_name": g.get("opening_name") or "Standard Opening",
                "eco": g.get("eco") or "",
                "moves_san": g.get("moves_san") or "",
            },
        }

    try:
        report = analyze_full_game_sync(game_id=game_id, depth=depth, force=force)
        return {"status": "ready", "analysis": report}
    except EngineBusy:
        return JSONResponse(
            status_code=503,
            content={"detail": "Stockfish engine pool is busy. Retry shortly."},
            headers={"Retry-After": str(ENGINE_BUSY_RETRY_AFTER)},
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/analysis/{game_id}/stream")
async def stream_full_game_analysis(
    request: Request,
    game_id: str = Path(..., min_length=1, max_length=64),
    depth: Optional[int] = Query(None, ge=MIN_ANALYSIS_DEPTH, le=MAX_ANALYSIS_DEPTH),
    force: bool = Query(False),
):
    _enforce_fair_play()
    return StreamingResponse(
        stream_game_analysis(game_id=game_id, depth=depth, force=force, request=request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/analysis/preview_sequence")
def preview_analysis_sequence(payload: SequencePreviewPayload):
    _enforce_fair_play()
    try:
        return preview_sequence_for_repertoire(repertoire=payload.repertoire, moves=payload.moves)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/analysis/save_sequence")
def save_analysis_sequence(payload: SequenceSavePayload):
    _enforce_fair_play()
    try:
        return save_sequence_to_repertoire(
            repertoire=payload.repertoire,
            moves=payload.moves,
            study_name=payload.study_name,
            replace_conflicts=payload.replace_conflicts,
            prioritize_for_training=payload.prioritize_for_training,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------- #
# Catch-All /api/* 404/405 & Static Frontend Mount
# --------------------------------------------------------------------------- #


@app.api_route(
    "/api/{unmatched_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def api_unmatched_route(request: Request, unmatched_path: str = ""):
    probe_scope = {
        "type": "http",
        "path": request.scope.get("path", ""),
        "root_path": request.scope.get("root_path", ""),
        "method": request.method,
    }
    for route in app.router.routes:
        try:
            match, _ = route.matches(probe_scope)
        except Exception:
            continue
        if match == Match.PARTIAL:
            return JSONResponse(status_code=405, content={"detail": "Method Not Allowed"})
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
