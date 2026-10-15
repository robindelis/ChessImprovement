import os
import json
import logging
import math
import re
import time
import ipaddress
import threading
import chess
from fastapi import FastAPI, Query, Path, HTTPException, Body, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders
from starlette.routing import Match
from typing import Annotated, Optional, List, Literal, Dict, Any

from backend.db import get_db, init_db, get_setting, set_setting, list_recent_scouted, toggle_favorite_scouted
from backend.lichess_client import LichessClient, DEFAULT_PERF_TYPES
from backend.analyzer import GameAnalyzer
from backend.openings import OpeningBook
from backend.repertoire import RepertoireManager, extract_player_info, normalize_fen
from backend.evaluator import (
    AsyncEngineEvaluator,
    get_sync_engine_pool,
    EngineBusy,
    SYNC_POOL_WAIT_TIMEOUT,
)
from backend.scout import ContentScout
from backend.scout_discovery import discover, enrich_videos
from backend.deviations import (
    find_game_deviations,
    get_deviation_drill,
    mark_deviation_resolved,
    reset_resolved_deviations,
    get_resolved_deviations,
)

# ---------------------------------------------------------------------------
# Request/response size caps (audit ABUSE-09 / ABUSE-08)
# ---------------------------------------------------------------------------
#: Largest request body accepted, in bytes. Every real request in this app is a
#: small JSON document (a move, a scouted line, a settings form); the biggest
#: realistic payload is a pasted PGN in `POST /api/scout/analyze`.
MAX_BODY_BYTES = 2 * 1024 * 1024

#: Deepest JSON nesting accepted. `python-chess` / pydantic recurse without a
#: depth limit, so a ~2 KB body of `[[[[...]]]]` used to raise `RecursionError`
#: (an uncaught 500 on every POST-JSON route). Real payloads nest 4 deep.
MAX_JSON_DEPTH = 32

#: `SQLite` stores INTEGERs as signed 64-bit; anything larger is an `OverflowError`.
SQLITE_INT64_MAX = 2 ** 63 - 1
SQLITE_INT64_MIN = -(2 ** 63)

logger = logging.getLogger("chess.api")

#: Castling field of a **standard** chess FEN. Shredder-FEN (chess960) uses
#: letters such as `AHah` and is deliberately rejected -- see `is_valid_fen`.
STANDARD_CASTLING_CHARS = frozenset("KQkq-")


def is_valid_fen(fen: str) -> bool:
    """Strictly validates a **standard-chess** FEN string and board legality.

    Audit BUILDER-12: this gate used to delegate entirely to
    `chess.Board(fen).is_valid()`, and `python-chess` accepts Shredder-FEN
    castling notation (`AHah` = "White's king-side rook is on the H file") as a
    synonym. A chess960 position was therefore accepted, moves were validated
    against it, and the stored row came back with a *standard* castling field --
    a different game smuggled into a standard repertoire, which the boot
    reachability migration then counts as an orphan forever.

    The repertoire is a standard tree (`chess.min.js` only ever emits `KQkq`,
    and `normalize_fen`'s 4-token key assumes it), so the castling field is now
    checked explicitly.
    """
    if not fen or not isinstance(fen, str):
        return False
    parts = fen.strip().split()
    if len(parts) < 4 or parts[0].count('/') != 7:
        return False
    if len(parts) > 6:
        return False
    # Castling availability must be standard notation, and the halfmove/fullmove
    # counters (when present) must be non-negative integers.
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

# ---------------------------------------------------------------------------
# Shared normalisation helpers (audit CODE-02-05 / CODE-02-06)
# ---------------------------------------------------------------------------

# Epoch values at or above this magnitude are milliseconds. 1e11 seconds is the
# year 5138, 1e11 milliseconds is 1973 -- so the threshold is unambiguous.
MS_EPOCH_THRESHOLD = 100_000_000_000


def normalize_played_at(value: Any) -> int:
    """Normalise a Lichess timestamp to epoch MILLISECONDS.

    `games.played_at` used to be written in milliseconds by
    `RepertoireManager.sync_recent_games` and in seconds by `POST /api/sync`,
    so `ORDER BY played_at DESC` was globally wrong (audit CODE-02-06).
    Milliseconds is the canonical unit because it is what the Lichess API emits
    in `createdAt`; any smaller magnitude is treated as seconds and scaled up.
    """
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    return v if v >= MS_EPOCH_THRESHOLD else v * 1000


def normalize_game_result(value: Any) -> str:
    """Normalise a Lichess `winner` field (or a stored `games.result`) to a
    self-describing three-way token: 'white' | 'black' | 'draw'.

    Audit CODE-02-05: Lichess exports `"winner": null` for a draw, so
    `g.get("winner", "draw")` returned `None`. That was persisted as SQL NULL
    and the reader then rendered every draw as a loss (`0-1`). A NULL, empty or
    unrecognised value is therefore normalised to 'draw'.
    """
    if value is None:
        return "draw"
    token = str(value).strip().lower()
    if token in ("white", "black"):
        return token
    return "draw"


def render_game_outcome(stored_result: Any, user_color: str) -> tuple:
    """(result, score_text) for a stored outcome token and the user's colour.

    `result` is always one of 'win' | 'loss' | 'draw' so the frontend contract
    (`.game-outcome-pill.win/.loss/.draw` in style.css) holds.
    """
    token = normalize_game_result(stored_result)
    if token == "draw":
        return "draw", "½-½"
    if token == user_color:
        return "win", ("1-0" if user_color == "white" else "0-1")
    return "loss", ("0-1" if user_color == "white" else "1-0")


def _setting_int(key: str, default: int) -> int:
    """Read an integer setting, falling back to `default` on a corrupt row.

    Audit CODE-02-13: `GET /api/settings` used to let a bad `settings` value
    raise a bare `ValueError`, which killed the whole Settings page.
    """
    try:
        return int(str(get_setting(key, default)).strip())
    except (TypeError, ValueError):
        logger.warning("Setting %r is not an integer; falling back to %r", key, default)
        return default


def _setting_float(key: str, default: float) -> float:
    """Read a float setting, falling back to `default` on a corrupt row."""
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
    """One-way, idempotent data migration: scale second-valued `played_at` rows
    up to milliseconds so every stored row shares the column's canonical unit
    (audit CODE-02-06). Returns the number of rows rewritten.

    Safe to call on every request: after the first run the WHERE clause matches
    zero rows. Never touches a millisecond-valued or zero/negative row.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE games SET played_at = played_at * 1000 "
            "WHERE played_at IS NOT NULL AND played_at > 0 AND played_at < ?",
            (MS_EPOCH_THRESHOLD,),
        )
        changed = cursor.rowcount or 0
        conn.commit()
        return changed
    finally:
        conn.close()


app = FastAPI(title="Chess Opening Improvement Platform")

# ---------------------------------------------------------------------------
# Validation-error rendering (audit ABUSE-16)
# ---------------------------------------------------------------------------
#: Caps applied to the reflected validation error. A 2 MiB request body must
#: not be amplified into a 2 MiB error response, and the error's `input` may be
#: an arbitrarily deep object (`{'input': {...deeply nested...}}`), so the
#: renderer below is itself depth-bounded.
MAX_ERROR_ITEMS = 20
MAX_ERROR_DEPTH = 6
MAX_ERROR_STRING = 200
#: Python ints wider than this are rendered as text: `json.dumps` on a
#: 10**100000 int is quadratically expensive and a 2 MiB body can hold one.
MAX_ERROR_INT = 10 ** 18


def _utf8_safe(text: str) -> str:
    """A `str` that `.encode("utf-8")` can never fail on, length-capped.

    A lone surrogate is a perfectly legal Python `str` and is exactly what a
    body of `\xed\xa0\x80` decodes to, so it has to be neutralised here rather
    than hoped away: the 422 body is serialised with `ensure_ascii=False`, and
    a single surrogate in it makes the *encoder* raise.
    """
    if not isinstance(text, str):
        text = str(text)
    if len(text) > MAX_ERROR_STRING:
        text = text[:MAX_ERROR_STRING] + "... (truncated)"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # C-level, O(n): unencodable characters become '?'.
        text = text.encode("utf-8", "replace").decode("utf-8", "replace")
    return text


def _json_safe(value, _depth: int = 0):
    """Coerce *anything* pydantic can place in `exc.errors()` into a value that
    `json.dumps(..., ensure_ascii=False).encode("utf-8")` is guaranteed to
    accept. Total by construction: no input can make this raise.

    Audit ABUSE-16: the previous `sanitize()` handled only non-finite floats,
    dicts and lists, so the 422 handler itself raised -- and a handler meant to
    answer 422s turned a 5-byte request into an unhandled 500 on 16 of the 20
    POST routes, over a real socket:

      * `TypeError: Object of type bytes is not JSON serializable`
        pydantic reports `'input': b'<raw body>'` whenever the body could not
        be decoded as JSON at all, which is precisely what FastAPI does for
        every non-JSON content type (`text/plain`, absent, `octet-stream`).
      * `UnicodeEncodeError`
        `'input'` may also be a `str` holding a lone surrogate, e.g. a body of
        `{"name": "\\xed\\xa0\\x80"}`, which `json.loads` produces happily.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if -MAX_ERROR_INT < value < MAX_ERROR_INT:
            return value
        # `str(huge_int)` is itself guarded in CPython 3.11+ ("Exceeds the limit
        # (4300 digits) for integer string conversion"), so a 2 MiB body of
        # digits would turn this branch into a 500. Render it from the bits.
        return f"<int with {value.bit_length()} bits>"
    if isinstance(value, float):
        # NaN/Infinity are not valid JSON and pydantic reports them verbatim.
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, str):
        return _utf8_safe(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return _utf8_safe(raw[:MAX_ERROR_STRING].decode("utf-8", "replace")) + \
            (f"... ({len(raw)} bytes)" if len(raw) > MAX_ERROR_STRING else "")
    if _depth >= MAX_ERROR_DEPTH:
        return "..."
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ERROR_ITEMS:
                out["..."] = f"{len(value) - MAX_ERROR_ITEMS} more key(s)"
                break
            out[_utf8_safe(key) if isinstance(key, str) else _utf8_safe(repr(key))] = \
                _json_safe(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [_json_safe(item, _depth + 1) for item in items[:MAX_ERROR_ITEMS]]
        if len(items) > MAX_ERROR_ITEMS:
            out.append(f"... {len(items) - MAX_ERROR_ITEMS} more item(s)")
        return out
    # Sets have no order and Exception/Decimal/UUID/... have no JSON form.
    return _utf8_safe(repr(value))


#: The body used when even rendering the error list fails. Kept as a literal so
#: it is provably encodable.
FALLBACK_VALIDATION_BODY = \
    '{"detail": [{"type": "value_error", "msg": "Request could not be validated."}]}'


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """422 for anything pydantic could not bind to a route signature.

    Audit ABUSE-16: this handler used to be able to raise. See `_json_safe`.
    It is also now *total* about the status it returns -- a validation failure
    is always a 4xx, never a 5xx, whatever the body was.
    """
    try:
        errors = exc.errors()
    except Exception:  # pragma: no cover - pydantic error extraction failed
        logger.exception("Could not extract validation errors for %s %s",
                         request.method, request.url.path)
        errors = [{"type": "value_error", "msg": "Request could not be validated."}]
    try:
        payload = json.dumps({"detail": _json_safe(errors)}, allow_nan=False)
    except Exception:  # pragma: no cover - _json_safe is total, belt and braces
        logger.exception("Could not render a 422 body for %s %s",
                         request.method, request.url.path)
        payload = FALLBACK_VALIDATION_BODY
    # `Response`, not `JSONResponse`: the body is already rendered, and the
    # final `errors="replace"` is the last line of defence against an encoder
    # surprise escaping as a 500.
    return Response(content=payload.encode("utf-8", "replace"),
                    status_code=422, media_type="application/json")

@app.exception_handler(ValueError)
async def value_error_handler(request, exc: ValueError):
    """Map a stray `ValueError` to a response without reflecting internal detail.

    Audit CODE-02-13: this handler used to answer `400 {"detail": str(exc)}` for
    EVERY ValueError raised anywhere in the app, so an internal failure such as
    a bad SQL column was reported to the browser as a client mistake with the
    raw schema text.

    Genuine user-input errors are still 4xx and still informative because every
    route converts them itself: FEN/UCI/colour validation raises an explicit
    `HTTPException(400/422)`, and the scout routes map `ContentScout`'s
    `ValueError`s (bad URL, unknown source, invalid colour) to 400 via
    `_scout_call`. Whatever still reaches this global handler is an internal
    bug, so it is logged server-side and answered with a generic 500.
    """
    detail = str(exc)
    if "Out of range float values" in detail:
        # Pydantic's own message, produced while validating the request body:
        # this really is a client error, so it stays a 422.
        return JSONResponse(status_code=422, content={"detail": "Invalid float value: NaN and Infinity are not allowed."})
    logger.exception("Unhandled ValueError while serving %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})

@app.exception_handler(RecursionError)
async def recursion_error_handler(request, exc: RecursionError):
    """Audit ABUSE-08: ~2 KB of `[[[[...]]]]` JSON used to raise an uncaught
    `RecursionError`, i.e. a bare 500 on *every* POST-JSON route. The depth cap
    in `LocalBoundaryMiddleware` rejects such a body up front with a 400; this
    handler is the backstop for anything that still recurses too far (a deeply
    nested *string* structure, a pathological `data:` payload, ...).
    """
    logger.warning("RecursionError while serving %s %s (body too deeply nested?)",
                   request.method, request.url.path)
    return JSONResponse(status_code=400, content={"detail": "Request body is nested too deeply."})

@app.exception_handler(OverflowError)
async def overflow_error_handler(request, exc: OverflowError):
    """Audit ABUSE-07 / SYNC-07: a Python `int` parameter larger than SQLite's
    64-bit `INTEGER` raised `OverflowError` at `cursor.execute`, which no handler
    covered -- a 500 for what is plainly a bad request value. Every `LIMIT`/id
    in this module is now range-checked, so reaching this is a bug rather than
    a client error, but the client still gets a 4xx and never a traceback.
    """
    logger.warning("OverflowError while serving %s %s", request.method, request.url.path)
    return JSONResponse(status_code=400, content={"detail": "Numeric value out of range."})

@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception):
    """Last-resort handler (audit ABUSE-08): no route may answer a bare
    `500 Internal Server Error` produced by an exception this app did not
    classify. The traceback goes to the log; the client gets a generic body.

    This handler lives in Starlette's `ServerErrorMiddleware`, i.e. *outside*
    `LocalBoundaryMiddleware`, so the security headers are attached explicitly
    here rather than by the middleware.
    """
    logger.exception("Unhandled %s while serving %s %s",
                     type(exc).__name__, request.method, request.url.path)
    response = JSONResponse(status_code=500, content={"detail": "Internal server error."})
    for name, value in SECURITY_RESPONSE_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


# ---------------------------------------------------------------------------
# Local-origin boundary (audit ABUSE-03) -- replaces the open CORS middleware
# ---------------------------------------------------------------------------
#
# Threat model
# ------------
# This is a SINGLE-USER desktop app bound to 127.0.0.1 whose API has **no**
# authentication at all (there is not a single `Depends`/`Security` in the
# codebase). The adversary is therefore not "the internet" but "any other web
# page open in the user's browser", plus DNS rebinding. What the fix must stop
# is a hostile *web origin* reaching a state-changing route. What it must NOT
# break is the Electron shell (`loadURL("http://127.0.0.1:<port>")`) or a plain
# browser tab on the same local origin.
#
# Design
# ------
# 1. **No CORS reflection, ever.** `CORSMiddleware(allow_origins=["*"],
#    allow_credentials=True)` was the whole bug: Starlette cannot answer `*` when
#    credentials are allowed, so it *echoed the caller's Origin* and marked the
#    response `Access-Control-Allow-Credentials: true`, handing every origin on
#    the internet full read access to the user's game library. `CORSMiddleware`
#    is removed. `Access-Control-Allow-Origin` is now emitted only for an
#    allow-listed local origin, and **never** together with
#    `Access-Control-Allow-Credentials: true` (this API is cookie-less by
#    design, so credentials are not needed for anything).
# 2. **Preflight refusal (CSRF, layer c).** A cross-origin `fetch` that sets
#    `Content-Type: application/json` (or any non-simple header) is preflighted
#    first. We answer `403` for a non-allow-listed origin, so the browser never
#    sends the real request at all.
# 3. **`Origin` / `Sec-Fetch-Site` enforcement (CSRF, layer c).** Every request
#    that carries an `Origin` which is not a local origin is refused with 403 --
#    for *every* method, not only mutating ones, because two GET routes
#    (`/api/games/sync_recent_stream`, `/api/repertoire/sync_games_stream`)
#    really do write to the database. `Sec-Fetch-Site` (sent by every current
#    browser on every request) is checked as an independent signal: anything
#    other than `same-origin`/`none` is refused. This is what actually stops
#    `<form>`/`no-cors` submissions, which cannot set custom headers.
# 4. **`Host` validation (layer d).** A rebound DNS name (`evil.test` resolving
#    to 127.0.0.1) makes the browser send `Host: evil.test`; requiring the
#    `Host` of a *browser-driven* request to be a loopback name/IP blunts that,
#    and also fails loudly if someone exposes the app on a LAN interface.
#
#    Two scoping decisions, both deliberate:
#    * It is scoped to **browser-driven** requests (those carrying `Origin` or
#      `Sec-Fetch-*`). DNS rebinding needs a browser to be useful, and a direct
#      non-browser client is not a CSRF vector at all — so `curl`, a local
#      script and Starlette's own `TestClient` (whose `Host` is the
#      unresolvable name `testserver`) keep working. `testserver` cannot be
#      resolved by a browser, so allowing it via this scoping carries no
#      rebinding risk in production.
#    * It is evaluated **after** the `Origin` and `Sec-Fetch-Site` checks.
#      `Origin` is the primary defence, so a hostile origin must be the named
#      cause in the log; checking `Host` first would make every browser probe
#      look like a rebinding probe and hide which defence actually fired.
#
#    Escape hatches: `CHESS_ALLOWED_HOSTS` / `CHESS_ALLOWED_ORIGINS`.
# 5. **Body caps (ABUSE-09/ABUSE-08)** are enforced in the same place so a
#    rejected request never reaches a route.
# 6. **Security response headers (ABUSE-13)** on every response, including the
#    403s produced above. The directive set is byte-identical to FIXER-E's
#    `APP_CSP` in `electron/main.js`, which *deletes* any server CSP for the
#    main frame and injects its own -- so the two agree and neither weakens the
#    other; the server header is what protects the plain-browser path.
#
# Rejected alternatives
# ---------------------
# * **Login / API key / `HTTPBearer`.** Explicitly out of scope: a product
#   decision (the user would have to type a secret into their own desktop app),
#   and it changes every frontend call site. Not an audit fix.
# * **Required custom header on mutating requests** (the classic
#   double-submit-style defence). Effective, but it is only enforceable if the
#   client sets it, and `frontend/**` + `electron/**` are owned by other
#   fixers in this wave; shipping it here would break the UI. Handed off
#   instead. It is also strictly weaker than (3): a cross-origin `fetch` cannot
#   set a custom header *without* a preflight we already refuse.
# * **A per-install random shared secret in a header.** Same blocker, and it
#   reintroduces the "auth system" the brief rules out. The `Origin` check is
#   the whole of CSRF protection needed when the client is same-origin.
# * **Keep `allow_origins=["*"]` but add the `Origin` check.** The middleware
#   would still *reflect* the header it is supposed to reject; deleting it is
#   the fix.
#
# Residual risk (documented, not fixable in this file)
# -----------------------------------------------------
# * A **non-browser** client (curl, a local script) can still call anything:
#   there is no auth, by design. If an attacker can run code on the machine
#   the CSRF boundary is moot.
# * A hypothetical pre-2016 browser that sends neither `Origin` nor
#   `Sec-Fetch-Site` on a cross-site POST is indistinguishable from `curl`.

#: `Sec-Fetch-Site` values that may reach a route. `same-origin` is the app
#: itself; `none` is a bookmark/address-bar navigation.
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})

#: Methods that may carry a request body (used to decide what to buffer).
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Host names that always mean "this machine".
LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

#: `Retry-After` advertised with a 503 when the shared Stockfish pool is
#: saturated. Audit ABUSE-22: this was a hard-coded `"5"` while the pool's own
#: bounded wait was `evaluator.SYNC_POOL_WAIT_TIMEOUT = 300.0`, i.e. the client
#: was told the slot would be free 60x sooner than it could be -- and a caller
#: that honoured the header and retried then measured 176 s of tail latency.
#: It is now *derived* from the wait, so the two cannot drift again, and the
#: wait itself was cut to 20 s (the value the async/SSE pool in the same module
#: already uses; 300 s is not a wait, it is a hang).
ENGINE_BUSY_RETRY_AFTER = max(1, int(round(SYNC_POOL_WAIT_TIMEOUT)))

#: Response headers applied to **every** response (audit ABUSE-13). `setdefault`
#: is used, so a route/middleware that sets one deliberately is never clobbered.
#: The CSP mirrors FIXER-E's `APP_CSP` in `electron/main.js` exactly.
SECURITY_RESPONSE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'none'; "
        "script-src 'self' https://www.youtube.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https://i.ytimg.com; "
        "font-src 'self' data:; media-src 'self' blob:; "
        "connect-src 'self'; frame-src https://www.youtube.com"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}


def _split_host_port(value: str):
    """`(hostname, port)` from a `Host` header or an origin authority.

    Handles `[::1]:38281`, `[::1]`, `127.0.0.1:38281`, `example.com:80` and a
    bare hostname. The port is `None` when absent.
    """
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
    """True when `host` can only name this machine (audit ABUSE-03 layer d)."""
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
    """Is `origin` an origin this local desktop app legitimately serves from?

    Allow-listed:
      * any `http` origin on a **loopback** host (`127.0.0.1`, `localhost`,
        `[::1]`) on any port -- the app answers on whichever port uvicorn/Electron
        picked, and the user may type `localhost` where Electron uses
        `127.0.0.1`;
      * the origin that matches this request's own `Host` header (self-origin);
      * anything listed in `CHESS_ALLOWED_ORIGINS` (operator escape hatch).

    Explicitly **not** allow-listed: `null` (a sandboxed iframe / `data:` /
    `file:` document -- the Electron shell loads over `http`, never `file://`),
    any `https` origin, every other host, and any authority whose port is not
    numeric (a malformed authority must not be pattern-matched into the
    loopback rule).
    """
    origin = (origin or "").strip().rstrip("/").lower()
    if not origin or origin == "null":
        return False
    if origin in _env_list("CHESS_ALLOWED_ORIGINS"):
        return True
    if not origin.startswith("http://"):
        return False
    host, port = _split_host_port(origin[len("http://"):])
    # A non-numeric port means this is not a well-formed authority at all
    # (e.g. `http://127.0.0.1:38281.evil.com`). No browser emits such an Origin,
    # but accepting it would be a parser differential against whoever parses
    # the header next, so it is refused rather than pattern-matched.
    if port is not None and not port.isdigit():
        return False
    if is_loopback_hostname(host):
        return True
    # Self-origin: the document was served by this very server.
    return bool(host_header) and origin == "http://" + (host_header or "").strip().rstrip("/").lower()


def is_allowed_request_host(host_header: str) -> bool:
    """Audit ABUSE-03 layer d: refuse a request addressed to a non-loopback name.

    `start.py` binds 127.0.0.1, so every legitimate request carries a loopback
    `Host`. A DNS-rebound name does not.
    """
    host, _port = _split_host_port(host_header)
    if not host:
        # HTTP/1.0 client with no Host header: nothing to validate against.
        return True
    if is_loopback_hostname(host):
        return True
    return host in _env_list("CHESS_ALLOWED_HOSTS")


def json_max_nesting_depth(data: bytes) -> int:
    """Maximum bracket nesting depth of a JSON document.

    String-aware (a `[` inside a JSON string does not open a container and an
    escaped quote does not close one), so a PGN such as `[%clk 0:05:00]` in a
    study name is scored correctly. Used to reject a hostile body *before*
    pydantic recurses into it (audit ABUSE-08).
    """
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
    """Should `body` be analysed as a JSON document?

    Audit ABUSE-16: the depth guard used to be gated on `Content-Type:
    application/json` alone, and nothing in FastAPI enforces that header. A
    `text/plain` (or absent, or `application/octet-stream`) request carrying a
    4000-deep JSON bomb therefore skipped the guard completely and reached
    pydantic, which returned 500 instead of 400. The header is caller-
    controlled, so the *body* gets a vote: a first non-whitespace byte of `{`
    or `[` is a JSON container whatever the header claims.

    This cannot reject a legitimate non-JSON body: form and multipart bodies
    percent-encode or quote their payloads, and real payloads nest 4 deep.
    """
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        return True
    return body.lstrip()[:1] in (b"{", b"[")


def _text_has_unpaired_surrogate_escape(text: str) -> bool:
    """Does `text` contain a JSON `\\uD800`-style escape that is not part of a
    properly *paired* surrogate pair?

    The walk is left-to-right and consumes `\\` as a unit, so a literal escaped
    backslash never starts an escape. A paired `\\ud800\\udc00` is a legitimate
    astral code point and is accepted, exactly as JSON and `json.loads` agree.
    """
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


def body_has_lone_surrogate(data: bytes) -> bool:
    """True when `data` cannot survive a UTF-8 round-trip as text.

    Audit ABUSE-16, third instance of the same root cause. A lone surrogate is
    a legal Python `str` and `json.loads` produces one from a five-byte body,
    because CPython decodes request bytes with `errors="surrogatepass"`:

      * raw WTF-8 -- `b'{"name": "\\xed\\xa0\\x80"}'` decodes to
        `{'name': '\\ud800'}`, and so does the ASCII escape `\\ud800` in the
        JSON source. Every JSON encoder in the request path (Starlette's
        `JSONResponse` uses `ensure_ascii=False`) then raises
        `UnicodeEncodeError`.

    Where that surfaces depends on which layer happens to reflect the value
    back, which is why it produced 500s in *two* unrelated places: inside
    FIXER-L's `RequestValidationError` handler, and inside the body of
    `POST /api/repertoire/train/recent_opening` (which additionally **persists**
    the surrogate into the `settings` table). Rejecting the body once, here,
    closes the class rather than the instances.

    Legitimate input to this app -- FENs, UCI, SAN, PGN, study names, Lichess
    ids -- contains no unpaired surrogate, and a *paired* one (an astral code
    point such as an emoji in a study name) is still accepted.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Only the surrogate range decodes under `surrogatepass`; anything else
        # undecodable here is already a 400 further down the stack.
        return bool(_SURROGATE_BYTE_RANGE_RE.search(data))
    if "\\u" not in text and "\\U" not in text:
        return False
    return _text_has_unpaired_surrogate_escape(text)


#: A UTF-8 encoded surrogate is `ED A0 80` .. `ED BF BF`.
_SURROGATE_BYTE_RANGE_RE = re.compile(b"[\xed][\xa0-\xbf][\x80-\xbf]")


class LocalBoundaryMiddleware:
    """Pure-ASGI guard: origin/CSRF boundary, body caps, security headers.

    Implemented at the ASGI level (not `BaseHTTPMiddleware`) for two reasons:
    the body cap must abort *while the body is being read* rather than after it
    has been fully buffered and JSON-parsed, and the security headers must be
    attached to responses produced by layers this app does not own (the
    `StaticFiles` mount, the 404/405 fall-through, `ServerErrorMiddleware`).
    """

    def __init__(self, app, max_body_bytes: int = MAX_BODY_BYTES,
                 max_json_depth: int = MAX_JSON_DEPTH):
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_json_depth = max_json_depth

    # -- helpers ---------------------------------------------------------- #
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

    # -- main ------------------------------------------------------------- #
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
            except Exception:  # pragma: no cover - malformed ASGI header
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
        # A request that carries *any* browser cross-origin marker is
        # browser-driven; one that carries none is a direct (non-browser) client
        # such as `curl`, `TestClient` or a local script. The Host check below
        # is a browser defence (DNS rebinding needs a browser to be useful), so
        # it is scoped to the browser-driven case -- which also keeps the
        # documented non-browser client surface (no auth, by design) working.
        browser_driven = bool(origin) or bool(fetch_site) or bool(headers.get("sec-fetch-mode"))

        # (a)/(b) Origin boundary -- checked FIRST, and for EVERY method, not
        # only the mutating ones (two GET routes stream a full Stockfish
        # analysis and write blunders).
        #
        # Ordering is deliberate: `Origin` is the primary defence and the reason
        # a refusal must be attributable to, so a hostile origin is named in the
        # log even when the `Host` is also unusual. Running the Host check first
        # would make every browser probe look like a DNS-rebinding probe and
        # hide which defence actually fired.
        if origin and not is_allowed_local_origin(origin, host_header):
            logger.warning("Refused cross-origin %s %s from Origin %r (Host %r)",
                           method, scope.get("path"), origin, host_header)
            await self._respond(
                send_with_security_headers, 403,
                "Cross-origin requests are not allowed by this local app.",
                # Never reflect the hostile origin, and never allow credentials.
                {"Vary": "Origin"},
            )
            return

        # (c) Fetch Metadata: an independent, browser-emitted cross-site signal.
        # `cross-site` covers a hostile page; `same-site` covers e.g. a rebound
        # `evil.test:38281` (same site, different port) or a page on `localhost`
        # calling `127.0.0.1`.
        if fetch_site and fetch_site not in ALLOWED_FETCH_SITES:
            logger.warning("Refused %s %s: Sec-Fetch-Site %r",
                           method, scope.get("path"), fetch_site)
            await self._respond(send_with_security_headers, 403,
                                "Cross-site requests are not allowed by this local app.")
            return

        # (d) Host validation -- blunts DNS rebinding and accidental LAN
        # exposure. Scoped to browser-driven requests, and evaluated only after
        # the Origin and Sec-Fetch-Site checks so the log names the real cause.
        if browser_driven and not is_allowed_request_host(host_header):
            logger.warning("Refused %s %s: Host %r is not a loopback name",
                           method, scope.get("path"), host_header)
            await self._respond(send_with_security_headers, 403,
                                "This server only answers loopback requests.")
            return

        # (c) CORS preflight. Refusing it is what stops the browser from ever
        # sending the real, state-changing request.
        if method == "OPTIONS" and "access-control-request-method" in headers:
            if origin and is_allowed_local_origin(origin, host_header):
                requested = (headers.get("access-control-request-headers") or "").lower()
                allowed = sorted({
                    h.strip() for h in requested.split(",") if h.strip()
                } | {"content-type", "accept"})
                await self._respond(
                    send_with_security_headers, 204, "",
                    {
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS",
                        "Access-Control-Allow-Headers": ", ".join(allowed),
                        "Access-Control-Max-Age": "600",
                        "Vary": "Origin",
                    },
                )
                return
            # No ACAO and no credentials: the browser blocks the follow-up.
            await self._respond(send_with_security_headers, 403,
                                "Cross-origin requests are not allowed by this local app.",
                                {"Vary": "Origin"})
            return

        # Same-origin safe methods need no body handling and must not be
        # buffered (SSE responses are long-lived GETs).
        #
        # Audit ABUSE-21: `BODY_METHODS` does not contain "GET" and a GET does
        # not normally send `Transfer-Encoding`, so a GET that *did* declare a
        # body skipped BOTH the 2 MiB cap and the JSON depth guard. The cap is
        # a property of the request, not of the verb, and ATK-5 confirmed the
        # gap is not request-smuggling-exploitable -- but a cap that applies to
        # every verb except one is not a cap. A GET that declares no
        # `Content-Length` and no `Transfer-Encoding` (every normal GET,
        # including the two long-lived SSE routes) is still not buffered.
        has_body = (method in BODY_METHODS
                    or "transfer-encoding" in headers
                    or "content-length" in headers)
        if has_body:
            declared = headers.get("content-length")
            if declared:
                try:
                    if int(declared) > self.max_body_bytes:
                        await self._respond(send_with_security_headers, 413,
                                            "Request body too large.")
                        return
                except ValueError:
                    await self._respond(send_with_security_headers, 400,
                                        "Invalid Content-Length header.")
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
                    logger.warning("Refused %s %s: body exceeded %d bytes",
                                   method, scope.get("path"), self.max_body_bytes)
                    await self._respond(send_with_security_headers, 413,
                                        "Request body too large.")
                    return

            # Gated on the body as well as the header -- see `body_is_json`.
            if body and body_is_json(headers.get("content-type"), body):
                depth = json_max_nesting_depth(body)
                if depth > self.max_json_depth:
                    logger.warning("Refused %s %s: JSON nested %d deep",
                                   method, scope.get("path"), depth)
                    await self._respond(
                        send_with_security_headers, 400,
                        f"Request body is nested too deeply (max {self.max_json_depth}).")
                    return
                # A lone surrogate is legal in a Python `str` and `json.loads`
                # manufactures one from a five-byte body, so every JSON encoder
                # downstream of it raises. Nothing this app accepts contains
                # one, so it is refused here rather than in each of the several
                # layers that could reflect it (audit ABUSE-16).
                if body_has_lone_surrogate(body):
                    logger.warning("Refused %s %s: request body holds an unpaired "
                                   "UTF-16 surrogate", method, scope.get("path"))
                    await self._respond(
                        send_with_security_headers, 400,
                        "Request body contains an unpaired surrogate; "
                        "it is not valid UTF-8 text.")
                    return

            replayed = [{"type": "http.request", "body": body, "more_body": False}]

            async def receive_replay():
                if replayed:
                    return replayed.pop(0)
                return {"type": "http.disconnect"}

            receive = receive_replay

        await self.app(scope, receive, send_with_security_headers)


# Added last -> outermost, so the boundary is evaluated before any route (or the
# `StaticFiles` mount) can do work, and the security headers are applied last.
app.add_middleware(LocalBoundaryMiddleware)

@app.middleware("http")
async def add_no_cache_header(request, call_next):
    response = await call_next(request)
    if request.url.path.endswith((".js", ".css", ".html")) or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Startup and Shutdown events
@app.on_event("startup")
def startup_event():
    init_db()
    # Normalise historical `games.played_at` rows to the canonical unit
    # (milliseconds) so `ORDER BY played_at DESC` is correct after boot.
    #
    # This is the ONLY caller of the migration (audit ABUSE-18). It used to also
    # run at the top of `GET /api/games/recent`, which made a read route issue
    # an `UPDATE` -- and therefore need SQLite's write lock -- on every request.
    # A boot migration belongs here; the read path is already correct without
    # it (the `ORDER BY` CASE + `normalize_played_at()`), so nothing is lost.
    try:
        rewritten = migrate_played_at_units()
        if rewritten:
            logger.info("Normalised played_at unit for %d game row(s) to milliseconds", rewritten)
    except Exception:
        logger.exception("played_at unit migration failed; continuing with read-time normalisation")

    try:
        OpeningBook.get_instance()
    except Exception:
        logger.exception("OpeningBook warm-up failed")

@app.on_event("shutdown")
async def shutdown_event():
    evaluator = AsyncEngineEvaluator.get_instance()
    await evaluator.close()
    # Hand-off FIXER-C section 6.1: the shared Stockfish pool that
    # `GameAnalyzer` / `RepertoireManager` check engines out of is process-wide,
    # so it is closed explicitly here instead of relying on the
    # `threading._register_atexit` net.
    try:
        get_sync_engine_pool().close_all()
    except Exception:
        logger.exception("Closing the sync engine pool failed")

# --- Pydantic Models ---
#
# Audit ABUSE-09 / SYNC-14: every string and every list used to be an
# unconstrained `str` / `List[Dict[str, Any]]`, so a 4 MB study name or a
# 200 000-character token was accepted and *persisted* into SQLite, and
# `check_tree` accepted 2 000 lines in one request (an O(lines x repertoire
# rows) fan-out). The caps below are far above anything the UI can produce:
# a Lichess token is 64 characters, a Lichess username 30, a study name is a
# label, and a single scouted opening line is well under 200 plies.
MAX_LICHESS_TOKEN_LEN = 512
MAX_USERNAME_LEN = 64
MAX_API_KEY_LEN = 512
MAX_STUDY_NAME_LEN = 120
MAX_DESCRIPTION_LEN = 2_000
MAX_COMMENT_LEN = 4_000
MAX_ARROWS_LEN = 4_000
MAX_FEN_LEN = 120
MAX_UCI_LEN = 8
MAX_SCOUT_SOURCE_LEN = 8_192
MAX_SCOUT_LINES = 200
MAX_SCOUT_TREE_LINES = 500
MAX_SOURCE_ID_LEN = 128
MAX_OPENING_NAME_LEN = 200
#: Move tokens accepted by `GET /api/repertoire/opening_info?moves=…`.  Mirrors
#: `openings.MAX_CONTEXT_MOVES`; the route clamps as well, so the bound holds even
#: if the two ever drift apart.
MAX_OPENING_INFO_MOVES = 200


class SettingsPayload(BaseModel):
    lichess_token: Optional[str] = Field(None, max_length=MAX_LICHESS_TOKEN_LEN)
    lichess_username: Optional[str] = Field(None, max_length=MAX_USERNAME_LEN)
    gemini_api_key: Optional[str] = Field(None, max_length=MAX_API_KEY_LEN)
    max_opening_ply: Optional[int] = Field(None, ge=1, le=100)
    blunder_threshold_cp: Optional[int] = Field(None, ge=1, le=2000)
    gap_threshold: Optional[float] = Field(None, ge=0.1, le=100.0, allow_inf_nan=False)
    sync_perf_types: Optional[str] = Field(None, max_length=120)

class ScoutAnalyzePayload(BaseModel):
    source: str = Field(..., max_length=MAX_SCOUT_SOURCE_LEN)
    force_refresh: Optional[bool] = False

class ScoutImportPayload(BaseModel):
    repertoire: Literal['white', 'black']
    moves: List[Dict[str, Any]] = Field(..., max_length=MAX_SCOUT_LINES)
    study_name: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)

class ScoutCheckLinePayload(BaseModel):
    repertoire: Literal['white', 'black'] = 'white'
    moves: List[Dict[str, Any]] = Field(default_factory=list, max_length=MAX_SCOUT_LINES)

class ScoutCheckTreePayload(BaseModel):
    repertoire: Literal['white', 'black'] = 'white'
    lines: List[Dict[str, Any]] = Field(default_factory=list, max_length=MAX_SCOUT_TREE_LINES)

class ScoutFavoritePayload(BaseModel):
    source_id: str = Field(..., max_length=MAX_SOURCE_ID_LEN)
    source_type: Optional[str] = Field(None, max_length=32)
    title: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN * 2)
    channel: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)
    opening_name: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)
    eco: Optional[str] = Field(None, max_length=16)
    thumbnail_url: Optional[str] = Field(None, max_length=512)
    duration: Optional[int] = Field(None, ge=0, le=86400 * 7)

class MoveAttemptPayload(BaseModel):
    # `ge/le` on the id keeps a value outside SQLite's signed 64-bit INTEGER out
    # of `cursor.execute` (audit ABUSE-07 / SYNC-07: `?id=9223372036854775808`
    # used to be a 500).
    blunder_id: int = Field(..., ge=SQLITE_INT64_MIN, le=SQLITE_INT64_MAX)
    attempted_uci: str = Field(..., max_length=16)

class RepertoireMovePayload(BaseModel):
    repertoire: Literal['white', 'black']
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
    repertoire: Literal['white', 'black']
    parent_fen: str = Field(..., max_length=MAX_FEN_LEN)
    move_uci: str = Field(..., max_length=MAX_UCI_LEN)
    is_important: Optional[bool] = None
    comment: Optional[str] = Field(None, max_length=MAX_COMMENT_LEN)
    arrows: Optional[str] = Field(None, max_length=MAX_ARROWS_LEN)
    study_name: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)

class DeleteMovePayload(BaseModel):
    repertoire: Literal['white', 'black']
    parent_fen: str = Field(..., max_length=MAX_FEN_LEN)
    move_uci: str = Field(..., max_length=MAX_UCI_LEN)

class TrainerMovePayload(BaseModel):
    repertoire: Literal['white', 'black']
    current_fen: str = Field(..., max_length=MAX_FEN_LEN)
    attempted_uci: str = Field(..., max_length=MAX_UCI_LEN)
    study_name: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)
    opening_filter: Optional[str] = Field(None, max_length=MAX_OPENING_NAME_LEN)
    is_retry: Optional[bool] = False
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
    repertoire: Literal['white', 'black']
    description: Optional[str] = Field("", max_length=MAX_DESCRIPTION_LEN)

class GroupSubtreePayload(BaseModel):
    repertoire: Literal['white', 'black']
    start_fen: str = Field(..., max_length=MAX_FEN_LEN)
    study_name: str = Field(..., min_length=1, max_length=MAX_STUDY_NAME_LEN)
    include_path_from_root: Optional[bool] = True
    path_uci: Optional[List[str]] = None

MAX_PGN_TEXT_LEN = 1_500_000

class PgnPreviewPayload(BaseModel):
    pgn_text: str = Field(..., max_length=MAX_PGN_TEXT_LEN)

class PgnImportPayload(BaseModel):
    pgn_text: str = Field(..., max_length=MAX_PGN_TEXT_LEN)
    repertoire: Literal["white", "black", "auto"] = "auto"
    study_name: Optional[str] = Field(None, max_length=MAX_STUDY_NAME_LEN)
    split_chapters_as_studies: bool = False
    replace_conflicts: bool = True
    clean_repertoire: bool = False

# --- Endpoints ---

@app.get("/api/account")
def get_account():
    client = LichessClient()
    profile = client.get_account_profile()
    username = get_setting("lichess_username") or (profile.get("username") if profile else None)
    
    return {
        "connected": profile is not None,
        "username": username,
        "perfs": profile.get("perfs", {}) if profile else {},
        "token_present": bool(client.token)
    }

@app.get("/api/settings")
def get_settings():
    return {
        "lichess_username": get_setting("lichess_username", ""),
        "has_token": bool(get_setting("lichess_token") or LichessClient().token),
        "has_gemini_key": bool(get_setting("gemini_api_key") or os.environ.get("GEMINI_API_KEY")),
        "max_opening_ply": _setting_int("max_opening_ply", 24),
        "blunder_threshold_cp": _setting_int("blunder_threshold_cp", 150),
        "gap_threshold": _setting_float("gap_threshold", 5.0),
        "sync_perf_types": get_setting("sync_perf_types", DEFAULT_PERF_TYPES)
    }

@app.post("/api/settings")
def update_settings(payload: SettingsPayload):
    if payload.lichess_token is not None:
        set_setting("lichess_token", payload.lichess_token.strip())
    if payload.lichess_username is not None:
        set_setting("lichess_username", payload.lichess_username.strip())
    if payload.gemini_api_key is not None:
        set_setting("gemini_api_key", payload.gemini_api_key.strip())
    if payload.max_opening_ply is not None:
        set_setting("max_opening_ply", str(payload.max_opening_ply))
    if payload.blunder_threshold_cp is not None:
        set_setting("blunder_threshold_cp", str(payload.blunder_threshold_cp))
    if payload.gap_threshold is not None:
        set_setting("gap_threshold", str(payload.gap_threshold))
    if payload.sync_perf_types is not None:
        set_setting("sync_perf_types", payload.sync_perf_types.strip())
    
    return {"status": "ok", "message": "Settings saved successfully."}

@app.post("/api/sync")
def sync_user_games(
    max_games: int = Query(15, ge=1, le=50),
    speeds: Optional[str] = Query(None, max_length=120)
):
    """Fetches recent Lichess games, stores them, and analyses their openings.

    Response contract (aligned with `/api/repertoire/sync_games*`):
      * every branch returns `status`, `synced_games`, `new_blunders`,
        `failed_games` and `errors`, so a client can branch on `status` alone;
      * `analyzed_at` is stamped ONLY after a game's analysis succeeded, so a
        crashed/failed analysis leaves the game pending and it is retried on
        the next sync instead of being suppressed forever (audit CODE-02-04);
      * one failing game never aborts the rest of the library (per-game error
        isolation);
      * a failure that affects the whole request is a non-2xx, never a 200
        with a success-looking body (audit CODE-02-11);
      * the Stockfish process and the SQLite connection are released on every
        path, including the error path.
    """
    client = LichessClient()
    username = get_setting("lichess_username")

    if not username:
        profile = client.get_account_profile()
        if profile and "username" in profile:
            username = profile["username"]
            set_setting("lichess_username", username)
        else:
            raise HTTPException(status_code=400, detail="Lichess username or token not configured.")

    # 1. Fetch recent games
    custom_perf = speeds or get_setting("sync_perf_types")
    try:
        if custom_perf:
            try:
                raw_games = client.fetch_user_games(username, max_games=max_games, perf_type=custom_perf)
            except TypeError:
                raw_games = client.fetch_user_games(username, max_games=max_games)
        else:
            try:
                raw_games = client.fetch_user_games(username, max_games=max_games)
            except TypeError:
                raw_games = client.fetch_user_games(username)
    except Exception:
        logger.exception("Lichess game fetch failed for %s", username)
        raise HTTPException(status_code=502, detail="Could not reach Lichess. Please try again.")

    if not raw_games:
        # `LichessClient.fetch_user_games` collapses "no games" and "the request
        # failed" into an empty list. The only signal available from here is the
        # account probe: a configured token that cannot read its own profile
        # means the upstream call failed, which must not be reported as success.
        if client.token and client.get_account_profile() is None:
            logger.error("Lichess returned no games and the account probe failed for %s", username)
            raise HTTPException(status_code=502, detail="Lichess rejected or could not serve the request. Please try again.")
        return {
            "status": "no_games",
            "message": "No new games found for this account.",
            "synced_games": 0,
            "new_blunders": 0,
            "skipped_games": 0,
            "failed_games": 0,
            "count": 0,
            "errors": []
        }

    conn = get_db()
    cursor = conn.cursor()
    analyzer = GameAnalyzer()

    synced_games = 0
    skipped_games = 0
    new_blunders_count = 0
    failed_games = 0
    errors: List[Dict[str, str]] = []

    try:
        for g in raw_games:
            game_id = g.get("id")
            if not game_id:
                failed_games += 1
                errors.append({"game_id": "", "error": "Lichess record has no game id."})
                continue

            # Check if already processed
            cursor.execute("SELECT id, analyzed_at FROM games WHERE id = ?", (game_id,))
            existing = cursor.fetchone()
            if existing and existing["analyzed_at"] > 0:
                skipped_games += 1
                continue

            # Per-game isolation: a malformed record, a dead engine or a DB
            # error must not skip the rest of the library. The game is left
            # pending (analyzed_at = 0) so the next sync retries it.
            try:
                # Safely determine user color and player ratings without crashing on None values
                white_user, white_rating = extract_player_info(g, "white")
                black_user, black_rating = extract_player_info(g, "black")
                user_color = "white" if white_user.lower() == (username or "").lower() else "black"

                opening_data = g.get("opening")
                if not isinstance(opening_data, dict):
                    opening_data = {}
                opening_name = opening_data.get("name", "")
                eco = opening_data.get("eco", "")
                moves = g.get("moves", "")

                # Three-way outcome. Lichess sends `"winner": null` for a draw,
                # which used to be stored as NULL and rendered as a 0-1 loss
                # (audit CODE-02-05).
                result = normalize_game_result(g.get("winner"))

                # Save the game as PENDING. `analyzed_at` is preserved for an
                # existing row and set to 0 for a new one; the "work is done"
                # flag is only written after the analysis below succeeds.
                # `ON CONFLICT DO UPDATE` is used instead of `INSERT OR REPLACE`
                # because the latter is a DELETE + INSERT in SQLite and would
                # orphan the game's `blunders` rows.
                cursor.execute("""
                INSERT INTO games (
                    id, speed, rated, played_at, white_player, black_player,
                    white_rating, black_rating, user_color, result, opening_name, eco, moves_san, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    speed = excluded.speed,
                    rated = excluded.rated,
                    played_at = excluded.played_at,
                    white_player = excluded.white_player,
                    black_player = excluded.black_player,
                    white_rating = excluded.white_rating,
                    black_rating = excluded.black_rating,
                    user_color = excluded.user_color,
                    result = excluded.result,
                    opening_name = excluded.opening_name,
                    eco = excluded.eco,
                    moves_san = excluded.moves_san
                """, (
                    game_id, g.get("speed"), 1 if g.get("rated") else 0,
                    normalize_played_at(g.get("createdAt", int(time.time()))),
                    white_user, black_user,
                    white_rating, black_rating,
                    user_color, result, opening_name, eco, moves, 0
                ))
                conn.commit()

                # Run blunder analysis on opening moves
                g["user_color"] = user_color
                blunders = analyzer.analyze_game_openings(g)
                if blunders:
                    analyzer.save_blunders_to_db(blunders)
                    new_blunders_count += len(blunders)

                # Analysis finished cleanly -> only NOW mark the game as done.
                cursor.execute("UPDATE games SET analyzed_at = ? WHERE id = ?", (int(time.time()), game_id))
                conn.commit()
                synced_games += 1
            except EngineBusy:
                # Hand-off FIXER-C section 6.2. The shared Stockfish pool is
                # saturated; `acquire()` raised after its bounded wait. This is
                # a "try again later" condition, not a per-game failure: it must
                # not be counted as a failed game (which would make the whole
                # request answer 500) and must not be waited out indefinitely.
                raise HTTPException(
                    status_code=503,
                    detail="The analysis engine is busy. Please retry in a few seconds.",
                    headers={"Retry-After": str(ENGINE_BUSY_RETRY_AFTER)},
                )
            except Exception:
                failed_games += 1
                errors.append({"game_id": str(game_id), "error": "Analysis failed for this game."})
                logger.exception("Analysis failed for game %s during /api/sync", game_id)
                try:
                    conn.rollback()
                except Exception:
                    pass
    finally:
        analyzer.close()
        conn.close()

    if synced_games == 0 and failed_games > 0:
        # Every game failed: this is a server-side failure, not a quiet success.
        raise HTTPException(
            status_code=500,
            detail=f"Analysis failed for all {failed_games} game(s). The games stay pending and will be retried."
        )

    return {
        "status": "success" if failed_games == 0 else "partial",
        "synced_games": synced_games,
        "new_blunders": new_blunders_count,
        "skipped_games": skipped_games,
        "failed_games": failed_games,
        "errors": errors
    }

class ImportGameOpeningRequest(BaseModel):
    game_id: str = Field(..., max_length=32)
    repertoire: Literal["white", "black"]
    max_ply: Optional[int] = Field(None, ge=1, le=100)
    replace_existing: Optional[bool] = False

@app.get("/api/games/recent")
def get_recent_games(
    color: Optional[str] = Query(None, pattern="^(white|black)$"),
    speed: Optional[str] = Query(None, max_length=32),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None, max_length=120)
):
    # Audit ABUSE-18: this route used to call `migrate_played_at_units()` first,
    # i.e. a table-wide `UPDATE` -- a *write* -- on every single read. That
    # needs SQLite's write lock, so a concurrent sync or analysis turned the
    # game list into a 3.6 s (and eventually 500-on-lock-timeout) operation, and
    # an unguarded write in a GET is a read-only route with a write dependency
    # by construction. ATK-5 A/B-proved the write was dead weight: the
    # `ORDER BY` CASE below and `normalize_played_at()` in the row loop
    # already produce byte-identical output with and without it.
    #
    # The migration is a boot concern, so it lives in `startup_event()` and
    # only there. Nothing about the *response* depends on it.
    conn = get_db()
    cursor = conn.cursor()

    query = "SELECT * FROM games"
    conditions = []
    params = []

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

    count_sql = f"SELECT COUNT(*) as cnt FROM ({query})"
    cursor.execute(count_sql, params)
    total_count = cursor.fetchone()["cnt"]

    # Sort on the NORMALISED value, not the raw column: writers this module does
    # not own (`backend/repertoire.py`) can still persist a seconds-valued row,
    # and a raw ORDER BY would bury it below every millisecond-valued game
    # (audit CODE-02-06).
    query += (
        " ORDER BY CASE WHEN played_at > 0 AND played_at < {thr} "
        "THEN played_at * 1000 ELSE played_at END DESC LIMIT ? OFFSET ?"
    ).format(thr=MS_EPOCH_THRESHOLD)
    params.extend([limit, offset])

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    now = time.time()
    opening_book = OpeningBook.get_instance()
    games = []

    for r in rows:
        row = dict(r)
        played_at_ms = normalize_played_at(row.get("played_at"))
        played_at_sec = played_at_ms / 1000.0  # canonical unit is milliseconds

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
        # Three-way outcome. A NULL/empty `games.result` is a legacy drawn game
        # (Lichess `winner: null`), NOT a loss (audit CODE-02-05).
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
            "game_url": f"https://lichess.org/{row.get('id')}"
        })

    return {
        "games": games,
        "total": total_count,
        "limit": limit,
        "offset": offset
    }

@app.post("/api/games/sync_recent")
def sync_recent_games_endpoint(
    max_games: int = Query(20, ge=1, le=50),
    speeds: Optional[str] = Query(None, max_length=120)
):
    rm = RepertoireManager.get_instance()
    if speeds:
        try:
            return rm.sync_recent_games(max_games=max_games, perf_type=speeds)
        except TypeError:
            return rm.sync_recent_games(max_games=max_games)
    return rm.sync_recent_games(max_games=max_games)

@app.get("/api/games/sync_recent_stream")
async def stream_sync_recent_games_endpoint(
    request: Request,
    max_games: int = Query(30, ge=1, le=100),
    speeds: Optional[str] = Query(None, max_length=120)
):
    """Streams real-time progress while syncing recent games from Lichess via Server-Sent Events."""
    rm = RepertoireManager.get_instance()
    if speeds:
        try:
            gen = rm.stream_sync_recent_games(max_games=max_games, request=request, perf_type=speeds)
        except TypeError:
            gen = rm.stream_sync_recent_games(max_games=max_games, request=request)
    else:
        gen = rm.stream_sync_recent_games(max_games=max_games, request=request)
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
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

@app.get("/api/dashboard")
def get_dashboard():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as count FROM games")
    total_games = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM blunders")
    total_blunders = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM drill_reviews WHERE status = 'mastered'")
    mastered_count = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM drill_reviews WHERE next_review_date <= ?", (int(time.time()),))
    due_reviews_count = cursor.fetchone()["count"]

    # Repertoire breakdown: blunders per opening.
    #
    # Audit SYNC-09: `eco` was a bare column in a `GROUP BY opening_name, color`
    # query, so SQLite returned the ECO of an *arbitrary* row of the group (the
    # reported label could change after a VACUUM or an index rebuild). The ECO
    # is now the one belonging to the group's worst blunder -- deterministic
    # (`cpl_loss DESC, id DESC` is a total order) and actually meaningful
    # ("this is the ECO where you lose most in this opening"). The row COUNT is
    # unchanged: `eco` is still not part of the grouping key, so the panel
    # shows the same number of rows as before.
    # `ORDER BY` also gained a tie-break, so two openings with an identical
    # `blunder_count` no longer swap places between requests.
    cursor.execute("""
    SELECT opening_name,
           (
               SELECT b2.eco
               FROM blunders b2
               WHERE IFNULL(b2.opening_name, '') = IFNULL(blunders.opening_name, '')
                 AND IFNULL(b2.color, '') = IFNULL(blunders.color, '')
               ORDER BY b2.cpl_loss DESC, b2.id DESC
               LIMIT 1
           ) AS eco,
           color,
           COUNT(*) as blunder_count,
           AVG(cpl_loss) as avg_loss
    FROM blunders
    GROUP BY opening_name, color
    ORDER BY blunder_count DESC, opening_name ASC
    LIMIT 10
    """)
    repertoire_blunders = [dict(row) for row in cursor.fetchall()]

    # Win / Loss / Draw stats. A NULL or empty `result` is a legacy drawn game
    # (Lichess exports `"winner": null` for a draw), so it is grouped as 'draw'
    # instead of forming its own bucket / being read as a loss
    # (audit CODE-02-05). The response shape is unchanged.
    cursor.execute("""
    SELECT CASE
               WHEN result IS NULL OR TRIM(result) = '' THEN 'draw'
               WHEN LOWER(TRIM(result)) IN ('white', 'black') THEN LOWER(TRIM(result))
               ELSE 'draw'
           END AS result,
           user_color,
           COUNT(*) as count
    FROM games
    GROUP BY result, user_color
    """)
    game_results = [dict(row) for row in cursor.fetchall()]

    # Recent blunders
    cursor.execute("""
    SELECT b.*, g.white_player, g.black_player, g.speed
    FROM blunders b
    JOIN games g ON b.game_id = g.id
    ORDER BY b.created_at DESC
    LIMIT 5
    """)
    recent_blunders = [dict(row) for row in cursor.fetchall()]

    conn.close()

    return {
        "total_games": total_games,
        "total_blunders": total_blunders,
        "mastered_count": mastered_count,
        "due_reviews_count": due_reviews_count,
        "repertoire_blunders": repertoire_blunders,
        "game_results": game_results,
        "recent_blunders": recent_blunders
    }

@app.get("/api/blunders")
def list_blunders(
    color: Optional[str] = Query(None, pattern="^(white|black)$"),
    eco: Optional[str] = Query(None, max_length=8),
    min_loss: Optional[int] = Query(None, ge=0, le=100000),
    category: Optional[str] = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000)
):
    """Blunder list.

    Audit ABUSE-07 / SYNC-07: `limit` used to be a bare `int = 50` with no
    `ge`/`le` and no `offset`:
      * `?limit=-1` reached SQLite, where a negative LIMIT means *no limit* --
        one unauthenticated request dumped the whole blunder table (each row
        plus a parsed `refutation_pv`);
      * `?limit=999999999999999999999` is a legal Python int but not a legal
        SQLite `INTEGER`, so `cursor.execute` raised an uncaught
        `OverflowError` -> 500.
    Both are now validation errors (422) or a bounded page, and `offset` gives
    the client a way to page *without* asking for an unbounded limit
    (escalation item 3). `min_loss` is bounded too, and `is not None` rather
    than truthiness so `?min_loss=0` is expressible.
    """
    conn = get_db()
    cursor = conn.cursor()

    query = """
    SELECT b.*, g.white_player, g.black_player, g.speed, g.played_at,
           d.status as drill_status, d.attempts, d.successes
    FROM blunders b
    JOIN games g ON b.game_id = g.id
    LEFT JOIN drill_reviews d ON b.id = d.blunder_id
    WHERE 1=1
    """
    params = []

    if color:
        query += " AND b.color = ?"
        params.append(color.lower())
    if eco:
        query += " AND b.eco LIKE ?"
        params.append(f"{eco}%")
    if min_loss is not None:
        query += " AND b.cpl_loss >= ?"
        params.append(min_loss)
    if category:
        query += " AND b.category = ?"
        params.append(category)

    # `b.id` is the tie-break so a page boundary is stable while blunders are
    # being inserted (the trainer writes new drill rows on every attempt).
    query += " ORDER BY b.cpl_loss DESC, b.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor.execute(query, params)
    blunders = [dict(row) for row in cursor.fetchall()]
    conn.close()

    for b in blunders:
        if b.get("refutation_pv"):
            try:
                b["refutation_pv"] = json.loads(b["refutation_pv"])
            except Exception:
                b["refutation_pv"] = []
        # Same unit contract as /api/games/recent (audit CODE-02-06).
        b["played_at"] = normalize_played_at(b.get("played_at"))

    return blunders

# ---------------------------------------------------------------------------
# Master/opening statistics for the blunder detail view (audit SYNC-08)
# ---------------------------------------------------------------------------
# `GET /api/blunders/{id}` used to call
# `LichessClient.get_opening_explorer_stats()` on *every* request: an
# un-cached, un-rate-limited, fully blocking `requests.get(timeout=8)` (twice on
# a 401, so up to 16 s) issued from a `def` route, i.e. from a worker thread in
# the shared anyio pool that also serves the SSE endpoints. Re-opening the same
# blunder -- which the detail view does constantly -- repeated the call.
#
# Three layers, cheapest first:
#   1. `explorer_cache`, the table every *other* explorer read in this app
#      already uses (`get_candidate_moves_2000` is its writer);
#   2. an in-process TTL memo, so a cache miss costs one outbound call per
#      position per TTL window rather than one per view;
#   3. a single-flight lock, so N concurrent views of the same position make
#      ONE outbound call, not N.
# A negative result (timeout / 429 / no data) is memoised for only
# `EXPLORER_FAILURE_TTL` seconds, so a transient failure is never turned into a
# permanent "no statistics".
EXPLORER_STATS_TTL = 6 * 3600
EXPLORER_FAILURE_TTL = 120
EXPLORER_STATS_MEMO_LIMIT = 512
#: Audit ABUSE-20: the lock table was unbounded and never pruned -- one
#: `threading.Lock` per distinct FEN, retained for the lifetime of the process,
#: sitting next to a memo that *is* bounded. An entry only has to exist while a
#: lookup is in flight, so the table is pruned back to this limit. It is half
#: the memo limit because the locks are the larger of the two objects and the
#: shorter-lived of the two states.
EXPLORER_STATS_LOCK_LIMIT = 256
EXPLORER_STATS_RATINGS = "2000,2200,2500"

_explorer_stats_memo: Dict[str, tuple] = {}
_explorer_stats_locks: Dict[str, "threading.Lock"] = {}
_explorer_stats_locks_guard = threading.Lock()


def _prune_explorer_stats_locks() -> None:
    """Drop the oldest entries until the table is back under its limit.

    Caller must hold `_explorer_stats_locks_guard`. A *currently held* lock is
    never dropped: removing it would let a second caller mint a second lock for
    the same key, and two concurrent outbound calls for one position is exactly
    what the single-flight exists to prevent. `dict` preserves insertion order,
    so the iteration is oldest-first. The table therefore settles at
    `limit + (locks held right now)`, which concurrency already bounds.
    """
    for key, lock in list(_explorer_stats_locks.items()):
        if len(_explorer_stats_locks) <= EXPLORER_STATS_LOCK_LIMIT:
            return
        try:
            held = lock.locked()
        except Exception:  # pragma: no cover - a foreign Lock implementation
            held = True
        if not held:
            del _explorer_stats_locks[key]


def _explorer_stats_lock(key: str) -> "threading.Lock":
    with _explorer_stats_locks_guard:
        lock = _explorer_stats_locks.get(key)
        if lock is None:
            if len(_explorer_stats_locks) >= EXPLORER_STATS_LOCK_LIMIT:
                _prune_explorer_stats_locks()
            lock = threading.Lock()
            _explorer_stats_locks[key] = lock
        return lock


def _normalize_explorer_fen(fen: str) -> str:
    """The ONE canonical cache key: the 4-token FEN form.

    Delegates to `backend.repertoire.normalize_fen`, the function AGENTS.md
    section 4F designates as the single source of truth for the
    `repertoire_moves.parent_fen` / `explorer_cache.fen` / `blunders.fen_before`
    join key. Audit ABUSE-19: the memo and the single-flight lock in
    `get_master_stats` were keyed on the **raw 6-token** FEN while the
    `explorer_cache` read underneath them used this 4-token form, so the two
    could never agree. A real transposition proves it --
    `1.e4 c6 2.d4 d5 3.Nc3` and `1.e4 c6 2.Nc3 d5 3.d4` are the same position
    with halfmove clocks 1 and 0, hence *different* 6-token FENs and *one*
    4-token key -- so 48 views of one position made 47 outbound calls.
    """
    return normalize_fen(fen) or (fen or "").strip()


def _master_stats_from_explorer_cache(fen: str):
    """Read the persisted explorer payload for `fen`, or `None`."""
    try:
        conn = get_db()
    except Exception:
        return None
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT data_json, updated_at FROM explorer_cache WHERE fen = ? AND ratings = ?",
            (_normalize_explorer_fen(fen), EXPLORER_STATS_RATINGS),
        )
        cached = cursor.fetchone()
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not cached:
        return None
    try:
        age = time.time() - float(cached["updated_at"] or 0)
        if age > 7 * 86400:
            return None
        payload = json.loads(cached["data_json"])
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    total = (payload.get("white_total") or 0) + (payload.get("draws_total") or 0) + (payload.get("black_total") or 0)
    return {
        "white": payload.get("white_total") or 0,
        "draws": payload.get("draws_total") or 0,
        "black": payload.get("black_total") or 0,
        "moves": payload.get("moves") or [],
        "total_games": total,
        "source": "explorer_cache",
    }


def get_master_stats(fen: str):
    """Master opening statistics for a position: cached, single-flight, bounded.

    Never raises: a failure upstream yields `None`, exactly as
    `LichessClient.get_opening_explorer_stats` did, so the detail view still
    renders without statistics instead of failing.
    """
    if not fen:
        return None

    # Audit ABUSE-19: the memo, the single-flight lock and the `explorer_cache`
    # read must all be keyed identically, or the single-flight does not
    # single-flight. The goal stated above ("N concurrent views of the same
    # position make ONE outbound call") is only true for the 4-token key.
    key = _normalize_explorer_fen(fen)

    memo = _explorer_stats_memo.get(key)
    if memo:
        value, stamp = memo
        ttl = EXPLORER_STATS_TTL if value else EXPLORER_FAILURE_TTL
        if (time.time() - stamp) < ttl:
            return value

    cached = _master_stats_from_explorer_cache(fen)
    if cached:
        _remember_explorer_stats(key, cached)
        return cached

    lock = _explorer_stats_lock(key)
    with lock:
        # Another waiter may have filled the memo while we queued on the lock.
        memo = _explorer_stats_memo.get(key)
        if memo:
            value, stamp = memo
            if (time.time() - stamp) < (EXPLORER_STATS_TTL if value else EXPLORER_FAILURE_TTL):
                return value
        try:
            stats = LichessClient().get_opening_explorer_stats(fen)
        except Exception:
            logger.exception("Explorer lookup failed for a blunder position")
            stats = None
        _remember_explorer_stats(key, stats)
        return stats


def _remember_explorer_stats(key: str, value) -> None:
    if len(_explorer_stats_memo) > EXPLORER_STATS_MEMO_LIMIT:
        _explorer_stats_memo.clear()
    _explorer_stats_memo[key] = (value, time.time())


@app.get("/api/blunders/{blunder_id}")
def get_blunder_detail(
    # Audit ABUSE-07 / SYNC-07: a legal Python int outside SQLite's signed
    # 64-bit range raised an uncaught `OverflowError` at `cursor.execute` (a
    # 500 for `?id=9223372036854775808`). It is now a 422, like every other
    # out-of-range value in this file.
    blunder_id: int = Path(..., ge=SQLITE_INT64_MIN, le=SQLITE_INT64_MAX),
    include_master_stats: bool = Query(True),
):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT b.*, g.white_player, g.black_player, g.speed, g.played_at,
           d.status as drill_status, d.attempts, d.successes, d.next_review_date
    FROM blunders b
    JOIN games g ON b.game_id = g.id
    LEFT JOIN drill_reviews d ON b.id = d.blunder_id
    WHERE b.id = ?
    """, (blunder_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Blunder position not found.")

    data = dict(row)
    data["played_at"] = normalize_played_at(data.get("played_at"))
    if data.get("refutation_pv"):
        try:
            data["refutation_pv"] = json.loads(data["refutation_pv"])
        except Exception:
            data["refutation_pv"] = []
    else:
        data["refutation_pv"] = []

    if data.get("best_pv"):
        try:
            data["best_pv"] = json.loads(data["best_pv"])
        except Exception:
            data["best_pv"] = []
    else:
        data["best_pv"] = []

    # GM opening stats from the Lichess Explorer: `explorer_cache` -> in-process
    # TTL memo -> single-flight live call (audit SYNC-08). `include_master_stats=false`
    # skips the network entirely for a caller that only wants the local row.
    data["master_stats"] = get_master_stats(data["fen_before"]) if include_master_stats else None

    # Get thematic ideas from OpeningBook
    ideas = OpeningBook.get_instance().get_strategic_ideas(data.get("eco", ""), data.get("opening_name", ""))
    data["opening_theme"] = ideas

    return data

@app.get("/api/train/paths")
def get_training_paths():
    conn = get_db()
    cursor = conn.cursor()

    # Repertoire paths grouped by opening family and color
    cursor.execute("""
    SELECT 
        COALESCE(NULLIF(
            CASE 
                WHEN opening_name LIKE '%Sicilian%' THEN 'Sicilian Defense'
                WHEN opening_name LIKE '%French%' THEN 'French Defense'
                WHEN opening_name LIKE '%Italian%' THEN 'Italian Game'
                WHEN opening_name LIKE '%Ruy Lopez%' OR opening_name LIKE '%Spanish%' THEN 'Ruy Lopez'
                WHEN opening_name LIKE '%Queen%' THEN 'Queen''s Gambit / Queen''s Pawn'
                WHEN opening_name LIKE '%King''s Indian%' THEN 'King''s Indian Defense'
                WHEN opening_name LIKE '%Caro%' THEN 'Caro-Kann Defense'
                WHEN opening_name IS NOT NULL AND opening_name != '' THEN substr(opening_name, 1, instr(opening_name || ':', ':') - 1)
                ELSE 'Other'
            END, ''), 'Other') as group_name,
        color,
        COUNT(*) as total_positions,
        SUM(CASE WHEN d.status = 'mastered' THEN 1 ELSE 0 END) as mastered_count,
        AVG(b.cpl_loss) as avg_loss
    FROM blunders b
    LEFT JOIN drill_reviews d ON b.id = d.blunder_id
    GROUP BY group_name, color
    HAVING total_positions > 0
    ORDER BY total_positions DESC
    """)
    paths = [dict(row) for row in cursor.fetchall()]

    # Also check total due for daily spaced repetition
    cursor.execute("""
    SELECT COUNT(*) as due_count FROM drill_reviews WHERE next_review_date <= ?
    """, (int(time.time()),))
    due_count = cursor.fetchone()["due_count"]

    conn.close()

    return {
        "paths": paths,
        "due_today": due_count
    }

@app.post("/api/train/evaluate_move")
def evaluate_training_move(payload: MoveAttemptPayload):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT b.*, d.attempts, d.successes, d.interval_days, d.ease_factor
    FROM blunders b
    LEFT JOIN drill_reviews d ON b.id = d.blunder_id
    WHERE b.id = ?
    """, (payload.blunder_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Blunder not found.")

    blunder = dict(row)
    fen_before = blunder["fen_before"]

    try:
        board = chess.Board(fen_before)
        if not board.is_valid():
            conn.close()
            raise HTTPException(status_code=400, detail="Invalid FEN string in blunder record.")
    except Exception:
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid FEN string in blunder record.")

    # Sanitize and validate move legality
    raw_uci = payload.attempted_uci.strip().lower()
    if len(raw_uci) == 5:
        try:
            from_sq = chess.parse_square(raw_uci[:2])
            piece = board.piece_at(from_sq)
            if not (piece and piece.piece_type == chess.PAWN and raw_uci[3] in ('1', '8')):
                raw_uci = raw_uci[:4]
        except Exception:
            pass

    try:
        user_move = chess.Move.from_uci(raw_uci)
        if user_move not in board.legal_moves:
            conn.close()
            return {
                "valid": False,
                "message": f"Illegal move in this position ({raw_uci})."
            }
    except Exception:
        conn.close()
        return {"valid": False, "message": "Invalid move format."}

    user_san = board.san(user_move)
    best_uci = blunder["best_move"]
    played_blunder_uci = blunder["played_move"]

    # Spaced repetition calculation
    now = int(time.time())
    attempts = (blunder["attempts"] or 0) + 1
    successes = (blunder["successes"] or 0)
    interval = blunder["interval_days"] or 1.0
    ease = blunder["ease_factor"] or 2.5
    status = "learning"

    is_exact_best = (user_move.uci() == best_uci)
    is_original_blunder = (user_move.uci() == played_blunder_uci)
    alt_pv = []

    if is_exact_best:
        successes += 1
        status = "mastered" if successes >= 2 else "learning"
        interval = min(interval * ease, 3650.0) if successes > 1 else 1.0
        ease = min(3.0, round(float(ease) + 0.1, 2))
        next_review = now + int(interval * 86400)
        feedback_type = "correct"
        feedback_msg = f"Best Move! {user_san} is the top engine choice."
    elif is_original_blunder:
        interval = 1.0
        ease = max(1.3, round(float(ease) - 0.25, 2))
        next_review = now + 86400
        feedback_type = "blunder"
        feedback_msg = f"Game Move: {user_san}?! (-{blunder['cpl_loss']} cp). Refutation line playing below."
    else:
        # User played an alternative legal move: evaluate with local Stockfish!
        analyzer = GameAnalyzer()
        try:
            eng = analyzer._get_engine()
            # Score user's alternative
            board.push(user_move)
            eval_after = eng.analyse(board, chess.engine.Limit(depth=10))
            score_after = eval_after["score"].pov(not board.turn).score(mate_score=10000)
            score_best = blunder["eval_before"]
            cpl_diff = score_best - score_after

            # Extract continuation line for alternative move
            raw_alt_pv = eval_after.get("pv", [])
            _, alt_pv = analyzer._get_line_details(board, raw_alt_pv, max_moves=6)

            if cpl_diff <= 35:
                successes += 1
                feedback_type = "good_alternative"
                diff_str = f"+{abs(cpl_diff)}" if cpl_diff < 0 else f"-{cpl_diff}"
                feedback_msg = f"Good alternative: {user_san} (Diff: {diff_str} cp). Engine top choice: {blunder['best_san']}."
                status = "learning"
                next_review = now + 86400
            else:
                feedback_type = "suboptimal"
                feedback_msg = f"{user_san} is suboptimal (-{cpl_diff} cp). Engine top choice: {blunder['best_san']}."
                interval = 1.0
                ease = max(1.3, round(float(ease) - 0.15, 2))
                next_review = now + 86400
        except EngineBusy:
            # Hand-off FIXER-C section 6.2: the shared Stockfish pool is
            # saturated. A drill attempt must not be reported as an engine
            # error *and* written to `drill_reviews` as a failed attempt, so
            # bail out with a retryable status before any state is persisted.
            try:
                conn.close()
            except Exception:
                pass
            raise HTTPException(
                status_code=503,
                detail="The analysis engine is busy. Please retry in a few seconds.",
                headers={"Retry-After": str(ENGINE_BUSY_RETRY_AFTER)},
            )
        except Exception:
            feedback_type = "engine_error"
            feedback_msg = f"Move {user_san} is legal, but could not evaluate position due to engine error."
            next_review = now + 86400
        finally:
            analyzer.close()

    # Update or insert review record (prevents silent progress loss)
    cursor.execute("""
    INSERT INTO drill_reviews (blunder_id, attempts, successes, last_attempt_date, next_review_date, interval_days, ease_factor, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(blunder_id) DO UPDATE SET
        attempts = excluded.attempts,
        successes = excluded.successes,
        last_attempt_date = excluded.last_attempt_date,
        next_review_date = excluded.next_review_date,
        interval_days = excluded.interval_days,
        ease_factor = excluded.ease_factor,
        status = excluded.status
    """, (payload.blunder_id, attempts, successes, now, next_review, max(1.0, min(float(interval), 3650.0)), ease, status))
    conn.commit()
    conn.close()

    # Continuation lines
    refutation_pv = []
    if blunder.get("refutation_pv"):
        try:
            refutation_pv = json.loads(blunder["refutation_pv"])
        except Exception:
            refutation_pv = []

    best_pv = []
    if blunder.get("best_pv"):
        try:
            best_pv = json.loads(blunder["best_pv"])
        except Exception:
            best_pv = []

    continuation_line = best_pv if is_exact_best else (alt_pv if feedback_type == "good_alternative" else (refutation_pv if is_original_blunder else []))

    return {
        "valid": True,
        "feedback_type": feedback_type,
        "message": feedback_msg,
        "is_correct": is_exact_best or feedback_type == "good_alternative",
        "best_san": blunder["best_san"],
        "best_uci": blunder["best_move"],
        "played_blunder_san": blunder["played_san"],
        "played_blunder_uci": blunder["played_move"],
        "refutation_line": refutation_pv,
        "best_line": best_pv,
        "continuation_pv": continuation_line,
        "strategic_idea": blunder["strategic_idea"],
        "explanation": blunder["explanation"],
        "drill_status": status,
        "attempts": attempts,
        "successes": successes
    }

# --- Chessbook-Style Repertoire Endpoints ---

@app.get("/api/repertoire/explore")
def explore_position_2000(
    fen: str = Query(...),
    ratings: str = Query("2000,2200,2500"),
    color: Literal["white", "black"] = Query("white")
):
    """Returns candidate moves played by 2000+ players with win rates, Stockfish evals, and user personal stats."""
    if not is_valid_fen(fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    rm = RepertoireManager.get_instance()
    return rm.get_candidate_moves_2000(fen, ratings=ratings, color=color)

@app.get("/api/repertoire/tree")
def get_repertoire_tree(
    color: Literal["white", "black"] = Query("white"),
    threshold: float = Query(5.0, ge=0.0, le=100.0),
    min_games: int = Query(30, ge=0),
    include_patched: bool = Query(False)
):
    """Returns the full repertoire move tree, coverage %, and gap analysis."""
    rm = RepertoireManager.get_instance()
    return rm.get_repertoire_tree(color, gap_threshold=threshold, min_games=min_games, include_patched=include_patched)

@app.post("/api/repertoire/move")
def add_repertoire_move(payload: RepertoireMovePayload):
    """Adds a prepared move or opponent branch to the repertoire."""
    if not is_valid_fen(payload.parent_fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    rm = RepertoireManager.get_instance()
    res = rm.add_repertoire_move(
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

@app.get("/api/repertoire/studies")
def get_repertoire_studies(repertoire: Optional[str] = Query(None)):
    """Returns all studies for a repertoire or all repertoires."""
    rm = RepertoireManager.get_instance()
    return rm.get_studies(repertoire)

@app.post("/api/repertoire/studies")
def create_repertoire_study(payload: StudyCreatePayload):
    """Creates a new named study."""
    rm = RepertoireManager.get_instance()
    res = rm.create_study(payload.name, payload.repertoire, payload.description or "")
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message", "Error creating study"))
    return res

@app.delete("/api/repertoire/studies/{study_id}")
def delete_repertoire_study(study_id: int, delete_moves: bool = Query(False)):
    """Deletes a named study, optionally deleting associated moves."""
    rm = RepertoireManager.get_instance()
    res = rm.delete_study(study_id, delete_moves=delete_moves)
    if res.get("status") == "error":
        raise HTTPException(status_code=404, detail=res.get("message", "Study not found"))
    return res

@app.post("/api/repertoire/studies/group_subtree")
def group_repertoire_subtree(payload: GroupSubtreePayload):
    """Groups all descendant moves starting from start_fen into a named study."""
    if not is_valid_fen(payload.start_fen):
        raise HTTPException(status_code=400, detail="Invalid start FEN position")
    rm = RepertoireManager.get_instance()
    res = rm.group_subtree_into_study(
        repertoire=payload.repertoire,
        start_fen=payload.start_fen,
        study_name=payload.study_name,
        include_path_from_root=bool(payload.include_path_from_root),
        path_uci=payload.path_uci
    )
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message", "Error grouping subtree"))
    return res

@app.post("/api/repertoire/move_meta")
def update_move_meta(payload: UpdateMoveMetaPayload):
    """Updates importance flag, personal notes/comments, and board arrows for a repertoire move."""
    if not is_valid_fen(payload.parent_fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    rm = RepertoireManager.get_instance()
    res = rm.update_move_metadata(
        repertoire=payload.repertoire,
        parent_fen=payload.parent_fen,
        move_uci=payload.move_uci,
        is_important=payload.is_important,
        comment=payload.comment,
        arrows=payload.arrows,
        study_name=payload.study_name
    )
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message", "Error updating move metadata"))
    return res

@app.delete("/api/repertoire/move")
def delete_repertoire_move(payload: DeleteMovePayload):
    """Deletes a move and all children branches from the repertoire."""
    if not is_valid_fen(payload.parent_fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    rm = RepertoireManager.get_instance()
    rm.delete_repertoire_branch(payload.repertoire, payload.parent_fen, payload.move_uci)
    return {"status": "ok", "message": "Branch deleted successfully."}

@app.get("/api/repertoire/train/next")
def get_next_repertoire_drill(
    color: Literal["white", "black", "mixed", "both", "all"] = Query("mixed"),
    opening: Optional[str] = Query(None),
    mode: Literal["adaptive", "due_only", "frontier_only", "weak_only", "mastered_audit", "full", "lapses_only", "deviations"] = Query("adaptive"),
    lapses: Optional[str] = Query(None),
    recent_ids: Optional[str] = Query(None),
    index: Optional[int] = Query(None, ge=0, le=10000),
    game_id: Optional[str] = Query(None, max_length=64)
):
    """Returns a test position from the user's repertoire, selected via pedagogical curriculum."""
    if mode == "deviations":
        recent_list = []
        if recent_ids:
            recent_list = [x.strip() for x in recent_ids.split(",") if x.strip()]
        dev_color = None if color in ("mixed", "both", "all") else color
        drill = get_deviation_drill(color=dev_color, game_id=game_id, recent_ids=recent_list, index=index)
        if not drill:
            return {"all_deviations_completed": True, "message": "No real game deviations found for this repertoire!"}
        return drill

    parsed_lapses = []
    if lapses:
        for x in lapses.split(","):
            x = x.strip()
            if x.isdigit():
                parsed_lapses.append(int(x))

    parsed_recent = []
    if recent_ids:
        for x in recent_ids.split(","):
            x = x.strip()
            if x.isdigit():
                parsed_recent.append(int(x))

    rm = RepertoireManager.get_instance()
    drill = rm.get_next_trainer_drill(
        color,
        opening_filter=opening,
        session_mode=mode,
        recent_session_lapses=parsed_lapses,
        recent_drilled_ids=parsed_recent
    )
    if not drill:
        if opening:
            raise HTTPException(status_code=404, detail=f"No prepared moves found for {color} in '{opening}'.")
        raise HTTPException(status_code=404, detail="No repertoire moves configured yet for this color.")
    return drill

@app.get("/api/repertoire/deviations")
def get_repertoire_deviations_endpoint(
    color: Optional[str] = Query(None, pattern="^(white|black)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None, max_length=120)
):
    """Analyzes stored user games against repertoire_moves to identify positions
    where the user deviated from their prepared repertoire lines."""
    return find_game_deviations(color=color, limit=limit, offset=offset, search=search)

@app.get("/api/repertoire/train/deviation_drill")
def get_trainer_deviation_drill_endpoint(
    color: Literal["white", "black", "mixed", "both", "all"] = Query("white"),
    game_id: Optional[str] = Query(None, max_length=64),
    index: Optional[int] = Query(None, ge=0, le=10000)
):
    """Returns a specific game deviation as an interactive drill for RepertoireTrainer."""
    dev_color = None if color in ("mixed", "both", "all") else color
    drill = get_deviation_drill(color=dev_color, game_id=game_id, index=index)
    if not drill:
        raise HTTPException(status_code=404, detail="No deviation found for this color/game.")
    return drill

@app.get("/api/repertoire/train/openings")
def get_trainer_openings(color: Optional[str] = Query(None)):
    """Returns distinct openings in the repertoire and the most recently learned opening."""
    rm = RepertoireManager.get_instance()
    return rm.get_repertoire_openings(color)

@app.get("/api/repertoire/train/progress")
def get_trainer_progress_endpoint(
    color: Literal["white", "black", "mixed", "both", "all"] = Query("mixed"),
    study: Optional[str] = Query(None)
):
    """Returns Chessbook-style repertoire mastery progress (mastered, learning, unlearned) for color and study."""
    rm = RepertoireManager.get_instance()
    return rm.get_trainer_progress(color, study_name=study)

@app.post("/api/repertoire/train/recent_opening")
def set_recent_learned_opening(payload: Dict[str, Any]):
    """Sets the most recently learned/scouted opening.

    The body model is a bare `Dict[str, Any]` (the frontend posts exactly
    `{"name": ..., "eco": ...}`), so the two values that are persisted are
    length-checked here -- audit ABUSE-09: an unbounded string in an
    `Any`-typed body is written verbatim into the `settings` table and is
    re-read by `/api/repertoire/train/openings`.
    """
    name = (payload.get("name") or payload.get("opening_name") or "").strip()
    eco = (payload.get("eco") or "").strip()
    if len(name) > MAX_OPENING_NAME_LEN:
        raise HTTPException(status_code=400,
                            detail=f"Opening name is too long (max {MAX_OPENING_NAME_LEN} characters).")
    if len(eco) > 8:
        raise HTTPException(status_code=400, detail="ECO code is too long (max 8 characters).")
    if name:
        set_setting("recent_learned_opening", json.dumps({"name": name, "eco": eco}))
        return {"status": "success", "recent_opening": {"name": name, "eco": eco}}
    return {"status": "error", "message": "Missing opening name"}

@app.post("/api/repertoire/deviations/resolve")
def resolve_repertoire_deviation_endpoint(payload: ResolveDeviationPayload):
    """Marks a game deviation as corrected/resolved on first try and removes it from active list."""
    success = mark_deviation_resolved(
        game_id=payload.game_id,
        fen_before=payload.fen_before or "",
        played_uci=payload.played_uci or "",
        prepared_uci=payload.prepared_uci or "",
        user_color=payload.user_color or "white"
    )
    res = find_game_deviations(color=payload.user_color)
    return {
        "status": "success" if success else "ignored",
        "resolved": success,
        "remaining_deviations": res["total_deviations"],
        "compliance_pct": res["compliance_pct"]
    }

@app.post("/api/repertoire/deviations/reset")
def reset_repertoire_deviations_endpoint():
    """Resets all resolved deviations back to active."""
    reset_resolved_deviations()
    return {"status": "success", "message": "All resolved deviations reset."}

@app.post("/api/repertoire/train/attempt")
def evaluate_repertoire_attempt(payload: TrainerMovePayload):
    """Validates user's played move in the trainer and picks the opponent's reply."""
    if not is_valid_fen(payload.current_fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    rm = RepertoireManager.get_instance()
    filter_val = payload.study_name or payload.opening_filter
    result = rm.evaluate_trainer_move(
        payload.repertoire,
        payload.current_fen,
        payload.attempted_uci,
        study_filter=filter_val,
        is_retry=bool(payload.is_retry)
    )
    # If drilling a game deviation and user answered correctly on the first try:
    if (payload.is_deviation or payload.game_id) and result.get("correct") and not payload.is_retry:
        mark_deviation_resolved(
            game_id=payload.game_id,
            fen_before=payload.current_fen,
            played_uci=payload.played_uci or "",
            prepared_uci=result.get("expected_uci") or payload.attempted_uci,
            user_color=payload.repertoire
        )
        result["deviation_resolved"] = True
    return result

def _count_repertoire_moves() -> int:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM repertoire_moves")
        return cursor.fetchone()["cnt"]
    finally:
        conn.close()


@app.post("/api/repertoire/seed")
def seed_default_repertoires(force: bool = Query(False)):
    """Seeds the generic default lines into the repertoire.

    Safety contract (AGENTS.md section 9.3/9.4): these lines must never be
    seeded into the user's own studies. `force=true` is the explicit, documented
    opt-in; without it a repertoire that already contains moves is REFUSED
    (HTTP 409) rather than silently left alone, so a 2xx is never returned for
    a request that did nothing.

    Audit CODE-05-04: `RepertoireManager.seed_default_repertoires` had no
    `return` on its success path, so FastAPI serialised the body as JSON `null`
    and `frontend/repertoire_builder.js` (`data.status === 'ok'`) threw on
    `None`. The route no longer trusts that return value: it verifies the
    post-condition (moves really exist afterwards) and synthesises a truthful
    body when the manager says nothing, so the route is correct whether or not
    the manager layer grows a proper return value.
    """
    # `force` is a declared bool: FastAPI/pydantic answers 422 for
    # `?force=maybe`, `?force=` (empty) and other non-boolean input rather than
    # silently coercing them.
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
        # Guard tripped: nothing was written. Truthful, non-2xx, and the body
        # keeps `status` so a client can still branch on it.
        return JSONResponse(
            status_code=409,
            content={
                **payload,
                "status": "skipped",
                "message": payload.get("message") or "Repertoire already contains moves. Seeding skipped to protect user data. Re-run with ?force=true to override.",
                "seeded_moves": 0,
                "total_moves": after,
                "force_required": True,
            },
        )

    if status in ("ok", "success", "seeded"):
        return {
            **payload,
            "status": "ok",
            "seeded_moves": seeded,
            "total_moves": after,
        }

    # The manager returned None / an unexpected shape. Trust the verified
    # post-condition instead of guessing from the return value.
    if seeded > 0:
        logger.warning("seed_default_repertoires returned %r; reporting verified post-condition (%d moves added)", type(result).__name__, seeded)
        return {
            "status": "ok",
            "message": "Default repertoires seeded successfully.",
            "seeded_moves": seeded,
            "total_moves": after,
        }

    logger.error("seed_default_repertoires returned %r and wrote no moves", type(result).__name__)
    raise HTTPException(status_code=500, detail="Seeding did not complete. The repertoire was not changed.")

@app.post("/api/repertoire/clear")
def clear_repertoire(repertoire: Optional[str] = Query(None)):
    """Completely resets and clears repertoire moves from the database."""
    rm = RepertoireManager.get_instance()
    clean_rep = repertoire.lower().strip() if repertoire else None
    if clean_rep and clean_rep not in ("white", "black"):
        raise HTTPException(status_code=400, detail="Invalid repertoire color. Expected 'white', 'black', or omit for all.")
    deleted_count = rm.clear_repertoire_database(clean_rep)
    scope = f"{clean_rep.capitalize()} repertoire" if clean_rep else "Entire repertoire database"
    return {
        "status": "ok",
        "message": f"{scope} reset successfully.",
        "deleted_count": deleted_count
    }

@app.post("/api/repertoire/preview_pgn")
def repertoire_preview_pgn(payload: PgnPreviewPayload):
    """Parses a PGN repertoire string and returns preview metadata without writing to the database."""
    rm = RepertoireManager.get_instance()
    try:
        return rm.preview_pgn_repertoire(payload.pgn_text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("PGN preview parsing failed")
        raise HTTPException(status_code=500, detail="Could not parse PGN preview.")

@app.post("/api/repertoire/import_pgn")
def repertoire_import_pgn(payload: PgnImportPayload):
    """Imports an entire PGN opening repertoire (e.g. from Chessbook or Lichess) into the repertoire database."""
    rm = RepertoireManager.get_instance()
    try:
        return rm.import_pgn_repertoire(
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
    include_patched: bool = Query(False)
):
    """Returns opening blunders from user's actual games based on >= min_games in 2000+ Elo database, sorted by cpl_loss DESC."""
    rm = RepertoireManager.get_instance()
    return rm.get_game_gaps(color, min_games=min_games, include_patched=include_patched)

@app.get("/api/repertoire/eval_stream")
async def stream_position_eval(request: Request, fen: str = Query(...), depth: int = Query(16, ge=1, le=30)):
    """Progressively streams Stockfish engine evaluation for any position on the board."""
    if not is_valid_fen(fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    evaluator = AsyncEngineEvaluator.get_instance()
    return StreamingResponse(
        evaluator.stream_eval(fen, max_depth=depth, request=request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@app.get("/api/repertoire/opening_info")
def get_opening_info(fen: str = Query(...), moves: str = Query("")):
    """Names the opening played on the board: the general family and the particular line.

    Backs the single small line under the Repertoire Builder board.  The answer
    is always the same shape -- ``in_book`` says whether the position itself is
    a position the ECO encyclopedia knows, and ``label`` is the ready-to-display
    ``"Sicilian Defense › Smith-Morra Gambit"``.

    `moves` is the optional SAN/UCI line leading to `fen` from the starting
    position.  It is what keeps naming the opening *after* the position has left
    book theory, which is the normal state of affairs in a repertoire builder:
    without it a position six plies past the last book move has no name at all.
    It is bounded twice -- by `MAX_OPENING_INFO_MOVES` here and by
    `OpeningBook._normalize_move_list` in the book -- so the GET cannot be turned
    into a long chess replay.
    """
    if not is_valid_fen(fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    line = [t for t in re.split(r"[\s,]+", moves or "") if t][:MAX_OPENING_INFO_MOVES]
    return OpeningBook.get_instance().get_opening_context(fen, line)


@app.post("/api/repertoire/sync_games")
def sync_repertoire_games(
    max_games: int = Query(30, ge=1, le=100),
    speeds: Optional[str] = Query(None, max_length=120)
):
    """Fetches the user's most recent games and analyzes their opening blunders.

    Audit SYNC-10: `max_games` was `Optional[int] = Query(None)`, and
    `LichessClient.fetch_user_games` treats a falsy value as "send no `max`
    parameter at all", so omitting it -- which is exactly what
    `frontend/repertoire_builder.js:758` does -- queued a depth-12 Stockfish
    analysis of the user's **entire** game history. The bound is now the same
    shape as every sibling endpoint (`/api/sync` `le=50`, the recent-games
    stream `le=100`), and the default of 30 matches the progress bar's own
    `?max_games=30`.
    """
    rm = RepertoireManager.get_instance()
    if speeds:
        try:
            return rm.analyze_user_games(max_games=max_games, max_ply=30, perf_type=speeds)
        except TypeError:
            return rm.analyze_user_games(max_games=max_games, max_ply=30)
    return rm.analyze_user_games(max_games=max_games, max_ply=30)

@app.get("/api/repertoire/sync_games_stream")
async def stream_sync_repertoire_games(
    request: Request,
    max_games: int = Query(30, ge=1, le=100),
    speeds: Optional[str] = Query(None, max_length=120)
):
    """Streams real-time progress while fetching and analyzing user games via Server-Sent Events.

    Audit SYNC-10: `ge=1` used to reject `0`/negatives only when they were
    passed *explicitly*; omitting the parameter still yielded `None` =
    "analyse everything". It is now bounded and defaulted like its siblings.
    """
    rm = RepertoireManager.get_instance()
    if speeds:
        try:
            gen = rm.stream_analyze_user_games(max_games=max_games, max_ply=30, request=request, perf_type=speeds)
        except TypeError:
            gen = rm.stream_analyze_user_games(max_games=max_games, max_ply=30, request=request)
    else:
        gen = rm.stream_analyze_user_games(max_games=max_games, max_ply=30, request=request)
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

# --- Opening Scout Endpoints ---
#
# One canonical handler per operation (audit CODE-02-12). The `/api/repertoire/*`
# spellings are thin aliases that delegate to the canonical `/api/scout/*`
# handler below; both are live because `frontend/scout.js` calls the scout
# paths and `frontend/repertoire_builder.js` calls the repertoire paths.
#
#   operation      canonical                        alias (kept: used by frontend)
#   import line    POST /api/scout/import_repertoire  POST /api/repertoire/import_line
#   check one line POST /api/scout/check_line         POST /api/repertoire/check_line
#   check tree     POST /api/scout/check_tree         POST /api/repertoire/check_tree


def _scout_call(operation: str, **kwargs):
    """Invoke a `ContentScout` operation with a single, consistent error policy.

    Audit CODE-02-13: the seven scout routes each repeated
    `except Exception as e: raise HTTPException(400, str(e))`, which reflected
    arbitrary internal text (a yt-dlp error, a traceback fragment) to the
    browser and reported server-side failures as client errors. `ContentScout`
    raises `ValueError` for genuinely bad user input (unknown source, invalid
    study URL, invalid colour) -- those stay informative 400s. Everything else
    is an internal failure: logged, generic 500.
    """
    scout = ContentScout.get_instance()
    try:
        return getattr(scout, operation)(**kwargs)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Scout operation %r failed", operation)
        raise HTTPException(status_code=500, detail="The opening scout could not complete the request.")


@app.get("/api/scout/search")
def scout_search(query: str = Query("Italian Game"), limit: int = Query(12, ge=1, le=30)):
    """Searches online for opening video courses, master guides, and encyclopedia theory.

    The legacy, side-blind search.  It is **kept byte-identical** because it is
    the cheapest way to run a pre-existing suite against a shape nobody is
    changing: the intent-aware replacement is `GET /api/scout/discover`, which
    adds a side, a play-or-against mode, ranked results and disambiguation
    without touching this contract.
    """
    scout = ContentScout.get_instance()
    return scout.search_online_content(query=query, limit=limit)

# --------------------------------------------------------------------------- #
# Intent-aware discovery.  `side` is the colour the user plays, `mode` is
# "play" / "against" / "either", and `chosen_eco` is the disambiguation chip the
# user clicked.  `intent_side=""`/`intent_mode=""` mean "no preference, infer it
# from the query", which is why they are optional strings and not Literals.
# --------------------------------------------------------------------------- #
SCOUT_SIDE = Query("", pattern="^(white|black|)$")
SCOUT_MODE = Query("", pattern="^(play|against|either|)$")

@app.get("/api/scout/discover")
def scout_discover(
    query: str = Query("Italian Game", max_length=200),
    side: str = SCOUT_SIDE,
    mode: str = SCOUT_MODE,
    chosen_eco: str = Query("", max_length=64),
    limit: int = Query(12, ge=4, le=24),
):
    """Resolve what the query means, then return only what answers it.

    Two-phase by design.  This call returns in about a second with everything the
    flat YouTube search already knows; `needs_enrichment` names the videos whose
    chapters, upload date and full description are still missing, and the client
    asks for those with `POST /api/scout/enrich` as a second paint.

    When the query is genuinely ambiguous -- "Alapin" resolves to eight
    encyclopedia entries across five families -- this returns
    `awaiting_choice: true` with **no network call at all** and the candidate
    list, so the user picks a chip instead of the Scout guessing.
    """
    try:
        return discover(
            query=query,
            side_pref=side or None,
            mode_pref=mode or None,
            chosen_eco=chosen_eco or None,
            limit=limit,
        )
    except Exception:
        logger.exception("Scout discovery failed for %r", query)
        raise HTTPException(status_code=500, detail="The opening scout could not complete the request.")

class ScoutEnrichPayload(BaseModel):
    """The second paint: metadata for videos the first search could not classify."""
    # `min_length=1`: an empty list is a client bug, not a no-op request, and
    # answering 200 for it hides a caller that never populated the shelf.  The
    # per-item cap bounds each id too -- `max_length` on a `List` bounds the
    # *number* of items, not their size.
    ids: List[Annotated[str, Field(max_length=64)]] = Field(..., min_length=1, max_length=8)
    named_eco: str = Field("", max_length=64)

@app.post("/api/scout/enrich")
def scout_enrich(payload: ScoutEnrichPayload):
    """Fetches chapters, upload date and description for a handful of results.

    Ids already in the cache are served from it, so repeating a search costs
    nothing.  A video that cannot be fetched is simply absent from the map: the
    card keeps what the flat search gave it and the shelf is one item poorer,
    which beats a 500 for a cosmetic field.
    """
    book = OpeningBook.get_instance()
    named_side, named_name = None, ""
    if payload.named_eco:
        for item in book.search_openings(payload.named_eco, limit=1) or []:
            named_side = book.classify_opening_side(item.get("eco", ""), item.get("name", ""))["side"]
            named_name = item.get("name", "")
            break
    try:
        return {"meta": enrich_videos(payload.ids, named_side, named_name)}
    except Exception:
        logger.exception("Scout enrichment failed for %d ids", len(payload.ids))
        raise HTTPException(status_code=500, detail="The opening scout could not complete the request.")

@app.get("/api/scout/suggest")
def scout_suggest(q: str = Query("", max_length=64), limit: int = Query(8, ge=1, le=12)):
    """Autocomplete over the local encyclopedia -- instant, offline, no quota.

    Served entirely from `openings_data`, so it answers while the user is still
    typing and it works with YouTube unreachable.  Each suggestion carries the
    side and the first moves, which is what makes "which Alapin?" a decision the
    user can actually make.
    """
    book = OpeningBook.get_instance()
    try:
        matches = book.suggest_openings(q, limit=limit) if q.strip() else []
    except Exception:
        logger.warning("Suggestion lookup failed for %r", q)
        matches = []
    out, seen = [], set()
    for item in matches or []:
        name = item.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        side_info = book.classify_opening_side(item.get("eco", ""), name)
        pgn = item.get("pgn", "")
        sans = [t for t in pgn.split() if not t.endswith(".") and not t[:1].isdigit()]
        out.append({
            "eco": item.get("eco", ""),
            "name": name,
            "label": book.name_label(name) or name,
            "side": side_info["side"],
            "opening_moves": sans[:6],
        })
    return out

@app.post("/api/scout/analyze")
def scout_analyze(payload: ScoutAnalyzePayload):
    """Analyzes a YouTube video, study, or PGN, extracting moves, positions, and timeline."""
    return _scout_call("analyze_source", source=payload.source, force_refresh=bool(payload.force_refresh))

@app.get("/api/scout/recent")
def scout_recent(limit: int = Query(20, ge=1, le=50)):
    """Returns recently scouted content from database cache."""
    return list_recent_scouted(limit=limit)

@app.post("/api/scout/import_repertoire")
def scout_import_repertoire(payload: ScoutImportPayload):
    """Canonical: imports an extracted line or variation into the user's Repertoire tree."""
    return _scout_call(
        "import_line_to_repertoire",
        repertoire=payload.repertoire,
        moves=payload.moves,
        study_name=payload.study_name,
    )

@app.post("/api/scout/check_line")
def scout_check_line(payload: ScoutCheckLinePayload):
    """Canonical: detects if a scouted line or its subline is already present in the user's repertoire."""
    return _scout_call("check_line_overlap", repertoire=payload.repertoire, moves=payload.moves)

@app.post("/api/scout/check_tree")
def scout_check_tree(payload: ScoutCheckTreePayload):
    """Canonical: batch repertoire-overlap detection for all explored lines in a session tree."""
    return _scout_call("check_lines_overlap", repertoire=payload.repertoire, lines=payload.lines)

@app.post("/api/scout/favorite")
def scout_toggle_favorite(payload: ScoutFavoritePayload):
    """Toggles bookmark/favorite status for a scouted item."""
    meta = {
        "source_type": payload.source_type,
        "title": payload.title,
        "channel": payload.channel,
        "opening_name": payload.opening_name,
        "eco": payload.eco,
        "thumbnail_url": payload.thumbnail_url,
        "duration": payload.duration,
    }
    new_fav = toggle_favorite_scouted(payload.source_id, meta=meta)
    return {"status": "ok", "source_id": payload.source_id, "is_favorite": new_fav}

# --- Aliases (kept for the frontend; delegate to the canonical handlers) ---

@app.post("/api/repertoire/import_line", include_in_schema=False)
def repertoire_import_line(payload: ScoutImportPayload):
    """Alias of POST /api/scout/import_repertoire (frontend/repertoire_builder.js:3024,3084)."""
    return scout_import_repertoire(payload)

@app.post("/api/repertoire/check_line", include_in_schema=False)
def repertoire_check_line(payload: ScoutCheckLinePayload):
    """Alias of POST /api/scout/check_line (frontend/repertoire_builder.js:2617,2622)."""
    return scout_check_line(payload)

@app.post("/api/repertoire/check_tree", include_in_schema=False)
def repertoire_check_tree(payload: ScoutCheckTreePayload):
    """Alias of POST /api/scout/check_tree (frontend/repertoire_builder.js:2672,2677)."""
    return scout_check_tree(payload)

# --- /api/* fall-through (audit CODE-02-02) ---------------------------------
#
# `app.mount("/", StaticFiles(...))` at the very bottom matches EVERY remaining
# path with Match.FULL, so Starlette never reaches its own `Match.PARTIAL`
# branch and a wrong-method call under `/api/` was answered with StaticFiles'
# 404 instead of 405 -- making "route exists but wrong verb" indistinguishable
# from "route does not exist". This catch-all is registered BEFORE the mount so
# it wins for `/api/*`, and it reproduces Starlette's 405-vs-404 decision by
# asking the route table which paths match with a different method.

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

# Mount static frontend files
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
