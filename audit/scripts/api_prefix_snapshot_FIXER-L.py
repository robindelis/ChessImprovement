"""PRE-FIX SNAPSHOT of backend/api.py as of FIXER-L start (= FIXER-B finished).

EVIDENCE ONLY -- never imported by the application. Tests the before/after of the
FIXER-L CORS/CSRF, resource-limit, header, performance and correctness patches.
Run with:  /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-L_before_after.py
"""
import os
import json
import logging
import math
import time
import chess
from fastapi import FastAPI, Query, HTTPException, Body, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.routing import Match
from typing import Optional, List, Literal, Dict, Any

from backend.db import get_db, init_db, get_setting, set_setting, list_recent_scouted, toggle_favorite_scouted
from backend.lichess_client import LichessClient
from backend.analyzer import GameAnalyzer
from backend.openings import OpeningBook
from backend.repertoire import RepertoireManager, extract_player_info
from backend.evaluator import AsyncEngineEvaluator
from backend.scout import ContentScout

def is_valid_fen(fen: str) -> bool:
    """Strictly validates chess FEN string structure and board legality."""
    if not fen or not isinstance(fen, str):
        return False
    parts = fen.strip().split()
    if len(parts) < 4 or parts[0].count('/') != 7:
        return False
    try:
        board = chess.Board(fen)
        return board.is_valid()
    except Exception:
        return False

logger = logging.getLogger("chess.api")

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

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    def sanitize(val):
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        if isinstance(val, dict):
            return {str(k): sanitize(v) for k, v in val.items()}
        if isinstance(val, (list, tuple)):
            return [sanitize(x) for x in val]
        return val
    cleaned_errors = sanitize(exc.errors())
    return JSONResponse(status_code=422, content={"detail": cleaned_errors})

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    try:
        rewritten = migrate_played_at_units()
        if rewritten:
            logger.info("Normalised played_at unit for %d game row(s) to milliseconds", rewritten)
    except Exception:
        logger.exception("played_at unit migration failed; continuing with read-time normalisation")

@app.on_event("shutdown")
async def shutdown_event():
    evaluator = AsyncEngineEvaluator.get_instance()
    await evaluator.close()

# --- Pydantic Models ---
class SettingsPayload(BaseModel):
    lichess_token: Optional[str] = None
    lichess_username: Optional[str] = None
    gemini_api_key: Optional[str] = None
    max_opening_ply: Optional[int] = Field(None, ge=1, le=100)
    blunder_threshold_cp: Optional[int] = Field(None, ge=1, le=2000)
    gap_threshold: Optional[float] = Field(None, ge=0.1, le=100.0, allow_inf_nan=False)

class ScoutAnalyzePayload(BaseModel):
    source: str
    force_refresh: Optional[bool] = False

class ScoutImportPayload(BaseModel):
    repertoire: Literal['white', 'black']
    moves: List[Dict[str, Any]]
    study_name: Optional[str] = None

class ScoutCheckLinePayload(BaseModel):
    repertoire: Literal['white', 'black'] = 'white'
    moves: List[Dict[str, Any]] = []

class ScoutCheckTreePayload(BaseModel):
    repertoire: Literal['white', 'black'] = 'white'
    lines: List[Dict[str, Any]] = []

class ScoutFavoritePayload(BaseModel):
    source_id: str

class MoveAttemptPayload(BaseModel):
    blunder_id: int
    attempted_uci: str

class RepertoireMovePayload(BaseModel):
    repertoire: Literal['white', 'black']
    parent_fen: str
    move_uci: str
    is_user_move: Optional[bool] = True
    popularity_pct: Optional[float] = Field(0.0, ge=0.0, le=100.0, allow_inf_nan=False)
    win_rate: Optional[float] = Field(0.0, ge=0.0, le=100.0, allow_inf_nan=False)
    eval_score: Optional[int] = 0
    is_important: Optional[bool] = None
    comment: Optional[str] = None
    arrows: Optional[str] = None
    study_name: Optional[str] = None

class UpdateMoveMetaPayload(BaseModel):
    repertoire: Literal['white', 'black']
    parent_fen: str
    move_uci: str
    is_important: Optional[bool] = None
    comment: Optional[str] = None
    arrows: Optional[str] = None
    study_name: Optional[str] = None

class DeleteMovePayload(BaseModel):
    repertoire: Literal['white', 'black']
    parent_fen: str
    move_uci: str

class TrainerMovePayload(BaseModel):
    repertoire: Literal['white', 'black']
    current_fen: str
    attempted_uci: str
    study_name: Optional[str] = None
    opening_filter: Optional[str] = None

class StudyCreatePayload(BaseModel):
    name: str
    repertoire: Literal['white', 'black']
    description: Optional[str] = ""

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
        "gap_threshold": _setting_float("gap_threshold", 5.0)
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
    
    return {"status": "ok", "message": "Settings saved successfully."}

@app.post("/api/sync")
def sync_user_games(max_games: int = Query(15, ge=1, le=50)):
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
    try:
        raw_games = client.fetch_user_games(username, max_games=max_games)
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
    game_id: str
    repertoire: Literal["white", "black"]
    max_ply: Optional[int] = Field(None, ge=1, le=100)

@app.get("/api/games/recent")
def get_recent_games(
    color: Optional[str] = Query(None, regex="^(white|black)$"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None)
):
    # Bring any historical second-valued `played_at` rows up to the canonical
    # millisecond unit before ordering. Idempotent (no-op once migrated).
    migrate_played_at_units()

    conn = get_db()
    cursor = conn.cursor()

    query = "SELECT * FROM games"
    conditions = []
    params = []

    if color:
        conditions.append("user_color = ?")
        params.append(color)

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
def sync_recent_games_endpoint(max_games: int = Query(20, ge=1, le=50)):
    rm = RepertoireManager.get_instance()
    return rm.sync_recent_games(max_games=max_games)

@app.get("/api/games/sync_recent_stream")
async def stream_sync_recent_games_endpoint(
    request: Request,
    max_games: int = Query(30, ge=1, le=100)
):
    """Streams real-time progress while syncing recent games from Lichess via Server-Sent Events."""
    rm = RepertoireManager.get_instance()
    return StreamingResponse(
        rm.stream_sync_recent_games(max_games=max_games, request=request),
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
        return rm.import_game_opening(payload.game_id, payload.repertoire, payload.max_ply)
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

    # Repertoire breakdown: blunders per opening
    cursor.execute("""
    SELECT opening_name, eco, color, COUNT(*) as blunder_count, AVG(cpl_loss) as avg_loss
    FROM blunders
    GROUP BY opening_name, color
    ORDER BY blunder_count DESC
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
    color: Optional[str] = None,
    eco: Optional[str] = None,
    min_loss: Optional[int] = None,
    category: Optional[str] = None,
    limit: int = 50
):
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
    if min_loss:
        query += " AND b.cpl_loss >= ?"
        params.append(min_loss)
    if category:
        query += " AND b.category = ?"
        params.append(category)

    query += " ORDER BY b.cpl_loss DESC LIMIT ?"
    params.append(limit)

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

@app.get("/api/blunders/{blunder_id}")
def get_blunder_detail(blunder_id: int):
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

    # Get GM opening stats from Lichess Explorer
    client = LichessClient()
    explorer_stats = client.get_opening_explorer_stats(data["fen_before"])
    data["master_stats"] = explorer_stats

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
        next_review = now + int(interval * 86400)
        feedback_type = "correct"
        feedback_msg = f"Best Move! {user_san} is the top engine choice."
    elif is_original_blunder:
        interval = 1.0
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
                next_review = now + 86400
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
        study_name=payload.study_name
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
    color: Literal["white", "black"] = Query("white"),
    opening: Optional[str] = Query(None),
    mode: Literal["adaptive", "due_only", "frontier_only", "full"] = Query("adaptive"),
    lapses: Optional[str] = Query(None),
    recent_ids: Optional[str] = Query(None)
):
    """Returns a test position from the user's repertoire, selected via pedagogical curriculum."""
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

@app.get("/api/repertoire/train/openings")
def get_trainer_openings(color: Optional[str] = Query(None)):
    """Returns distinct openings in the repertoire and the most recently learned opening."""
    rm = RepertoireManager.get_instance()
    return rm.get_repertoire_openings(color)

@app.get("/api/repertoire/train/progress")
def get_trainer_progress_endpoint(
    color: Literal["white", "black"] = Query("white"),
    study: Optional[str] = Query(None)
):
    """Returns Chessbook-style repertoire mastery progress (mastered, learning, unlearned) for color and study."""
    rm = RepertoireManager.get_instance()
    return rm.get_trainer_progress(color, study_name=study)

@app.post("/api/repertoire/train/recent_opening")
def set_recent_learned_opening(payload: Dict[str, Any]):
    """Sets the most recently learned/scouted opening."""
    name = (payload.get("name") or payload.get("opening_name") or "").strip()
    eco = (payload.get("eco") or "").strip()
    if name:
        set_setting("recent_learned_opening", json.dumps({"name": name, "eco": eco}))
        return {"status": "success", "recent_opening": {"name": name, "eco": eco}}
    return {"status": "error", "message": "Missing opening name"}

@app.post("/api/repertoire/train/attempt")
def evaluate_repertoire_attempt(payload: TrainerMovePayload):
    """Validates user's played move in the trainer and picks the opponent's reply."""
    if not is_valid_fen(payload.current_fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    rm = RepertoireManager.get_instance()
    filter_val = payload.study_name or payload.opening_filter
    return rm.evaluate_trainer_move(payload.repertoire, payload.current_fen, payload.attempted_uci, study_filter=filter_val)

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

@app.post("/api/repertoire/sync_games")
def sync_repertoire_games(max_games: Optional[int] = Query(None)):
    """Fetches user's games (all games if max_games is None or 0) and analyzes opening blunders."""
    rm = RepertoireManager.get_instance()
    return rm.analyze_user_games(max_games=max_games, max_ply=30)

@app.get("/api/repertoire/sync_games_stream")
async def stream_sync_repertoire_games(
    request: Request,
    max_games: Optional[int] = Query(None, ge=1)
):
    """Streams real-time progress while fetching and analyzing user games via Server-Sent Events."""
    rm = RepertoireManager.get_instance()
    return StreamingResponse(
        rm.stream_analyze_user_games(max_games=max_games, max_ply=30, request=request),
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
    """Searches online for opening video courses, master guides, and encyclopedia theory."""
    scout = ContentScout.get_instance()
    return scout.search_online_content(query=query, limit=limit)

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
    new_fav = toggle_favorite_scouted(payload.source_id)
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
FRONTEND_DIR = "/home/robin/Projects/ChessImprovement/frontend"  # pinned: this snapshot lives in audit/scripts/
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
