"""
Rebuild the PRE-FIX copy of `backend/api.py` (the state immediately before
FIXER-B's patch) as `audit/scripts/api_prefix_snapshot.py`.

The snapshot is produced by reverse-applying exactly the edits that FIXER-B made
to `backend/api.py`, using marker-based slicing so a missing/duplicated marker
fails loudly instead of silently producing a wrong file.

The snapshot is a *drop-in module*: it does `from backend.db import ...` like the
real thing, so a probe can import it under any module name, build its own
`FastAPI()` app, and exercise it with `TestClient` without touching the live
`backend/api.py`.

Run:  /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-B_make_prefix_snapshot.py
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(PROJECT_ROOT, "backend", "api.py")
DST = os.path.join(PROJECT_ROOT, "audit", "scripts", "api_prefix_snapshot.py")


def slice_between(text, start_marker, end_marker, replacement, *, keep_end=True):
    """Replace text[start_marker : end_marker] with `replacement`."""
    i = text.index(start_marker)
    j = text.index(end_marker, i)
    if not keep_end:
        j += len(end_marker)
    return text[:i] + replacement + text[j:]


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected exactly 1 occurrence of:\n{old!r}\ngot {text.count(old)}"
    return text.replace(old, new)


def main():
    with open(SRC, "r", encoding="utf-8") as f:
        text = f.read()

    # 1. imports
    text = replace_once(
        text,
        "import os\nimport json\nimport logging\nimport math\nimport time\n",
        "import os\nimport json\nimport math\nimport time\n",
    )
    text = replace_once(text, "from starlette.routing import Match\n", "")

    # 2. remove the shared normalisation helpers block
    text = slice_between(
        text,
        "logger = logging.getLogger(\"chess.api\")",
        "app = FastAPI(title=\"Chess Opening Improvement Platform\")",
        "",
    )

    # 3. ValueError handler
    text = slice_between(
        text,
        "@app.exception_handler(ValueError)",
        "app.add_middleware(",
        '''@app.exception_handler(ValueError)
async def value_error_handler(request, exc: ValueError):
    if "Out of range float values" in str(exc):
        return JSONResponse(status_code=422, content={"detail": "Invalid float value: NaN and Infinity are not allowed."})
    return JSONResponse(status_code=400, content={"detail": str(exc)})

''',
    )

    # 4. startup event
    text = slice_between(
        text,
        "# Startup and Shutdown events",
        "@app.on_event(\"shutdown\")",
        '''# Startup and Shutdown events
@app.on_event("startup")
def startup_event():
    init_db()

''',
    )

    # 5. /api/settings
    text = slice_between(
        text,
        '@app.get("/api/settings")',
        '@app.post("/api/settings")',
        '''@app.get("/api/settings")
def get_settings():
    return {
        "lichess_username": get_setting("lichess_username", ""),
        "has_token": bool(get_setting("lichess_token") or LichessClient().token),
        "has_gemini_key": bool(get_setting("gemini_api_key") or os.environ.get("GEMINI_API_KEY")),
        "max_opening_ply": int(get_setting("max_opening_ply", "24")),
        "blunder_threshold_cp": int(get_setting("blunder_threshold_cp", "150")),
        "gap_threshold": float(get_setting("gap_threshold", "5.0"))
    }

''',
    )

    # 6. POST /api/sync
    text = slice_between(
        text,
        '@app.post("/api/sync")',
        "class ImportGameOpeningRequest(BaseModel):",
        '''@app.post("/api/sync")
def sync_user_games(max_games: int = Query(15, ge=1, le=50)):
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
    raw_games = client.fetch_user_games(username, max_games=max_games)
    if not raw_games:
        return {"status": "no_games", "message": "No new games retrieved or rate limit hit.", "count": 0}

    conn = get_db()
    cursor = conn.cursor()
    analyzer = GameAnalyzer()

    synced_games = 0
    new_blunders_count = 0

    for g in raw_games:
        game_id = g.get("id")
        # Check if already processed
        cursor.execute("SELECT id, analyzed_at FROM games WHERE id = ?", (game_id,))
        existing = cursor.fetchone()
        if existing and existing["analyzed_at"] > 0:
            continue

        # Safely determine user color and player ratings without crashing on None values
        white_user, white_rating = extract_player_info(g, "white")
        black_user, black_rating = extract_player_info(g, "black")
        user_color = "white" if white_user.lower() == username.lower() else "black"

        opening_data = g.get("opening")
        if not isinstance(opening_data, dict):
            opening_data = {}
        opening_name = opening_data.get("name", "")
        eco = opening_data.get("eco", "")
        moves = g.get("moves", "")
        result = g.get("winner", "draw") if "winner" in g else "draw"

        # Save game record
        cursor.execute("""
        INSERT OR REPLACE INTO games (
            id, speed, rated, played_at, white_player, black_player,
            white_rating, black_rating, user_color, result, opening_name, eco, moves_san, analyzed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            game_id, g.get("speed"), 1 if g.get("rated") else 0,
            g.get("createdAt", int(time.time())),
            white_user, black_user,
            white_rating, black_rating,
            user_color, result, opening_name, eco, moves, int(time.time())
        ))
        conn.commit()

        # Run blunder analysis on opening moves
        g["user_color"] = user_color
        blunders = analyzer.analyze_game_openings(g)
        if blunders:
            analyzer.save_blunders_to_db(blunders)
            new_blunders_count += len(blunders)

        synced_games += 1

    analyzer.close()
    conn.close()

    return {
        "status": "success",
        "synced_games": synced_games,
        "new_blunders": new_blunders_count
    }

''',
    )

    # 7. GET /api/games/recent header/normalisation block
    text = slice_between(
        text,
        "    # Bring any historical second-valued `played_at` rows",
        '        if user_color == "white":\n            opponent_name = row.get("black_player")',
        '''    conn = get_db()
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

    query += " ORDER BY played_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    now = time.time()
    opening_book = OpeningBook.get_instance()
    games = []

    for r in rows:
        row = dict(r)
        played_at_raw = row.get("played_at") or 0
        played_at_sec = (played_at_raw / 1000.0) if played_at_raw > 1e11 else float(played_at_raw)

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
            formatted_date = time.strftime("%b %d, %Y", time.localtime(played_at_sec))

        user_color = row.get("user_color") or "white"
        raw_result = (row.get("result") or "").lower()

        if raw_result == user_color:
            result = "win"
            score_text = "1-0" if user_color == "white" else "0-1"
        elif raw_result == "draw":
            result = "draw"
            score_text = "\\u00bd-\\u00bd"
        else:
            result = "loss"
            score_text = "0-1" if user_color == "white" else "1-0"

''',
    )
    text = replace_once(text, '"played_at": played_at_ms,', '"played_at": played_at_raw,')

    # 8. GET /api/dashboard game_results
    text = slice_between(
        text,
        "    # Win / Loss / Draw stats.",
        "    # Recent blunders",
        '''    # Win / Loss / Draw stats
    cursor.execute("SELECT result, user_color, COUNT(*) as count FROM games GROUP BY result, user_color")
    game_results = [dict(row) for row in cursor.fetchall()]

''',
    )

    # 9. played_at normalisation on the two /api/blunders responses
    text = replace_once(
        text,
        '        # Same unit contract as /api/games/recent (audit CODE-02-06).\n        b["played_at"] = normalize_played_at(b.get("played_at"))\n',
        "",
    )
    text = replace_once(text, '    data["played_at"] = normalize_played_at(data.get("played_at"))\n', "")

    # 10. POST /api/repertoire/seed
    text = slice_between(
        text,
        "def _count_repertoire_moves() -> int:",
        '@app.post("/api/repertoire/clear")',
        '''@app.post("/api/repertoire/seed")
def seed_default_repertoires(force: bool = Query(False)):
    """Seeds default repertoire lines only if empty or force=True."""
    rm = RepertoireManager.get_instance()
    res = rm.seed_default_repertoires(force=force)
    return res

''',
    )

    # 11. scout routes + the /api/* catch-all (removed back to the pre-fix block)
    text = slice_between(
        text,
        "# --- Opening Scout Endpoints ---",
        "# Mount static frontend files",
        '''# --- Opening Scout Endpoints ---

@app.get("/api/scout/search")
def scout_search(query: str = Query("Italian Game"), limit: int = Query(12, ge=1, le=30)):
    """Searches online for opening video courses, master guides, and encyclopedia theory."""
    scout = ContentScout.get_instance()
    return scout.search_online_content(query=query, limit=limit)

@app.post("/api/scout/analyze")
def scout_analyze(payload: ScoutAnalyzePayload):
    """Analyzes a YouTube video, study, or PGN, extracting moves, positions, and timeline."""
    scout = ContentScout.get_instance()
    try:
        return scout.analyze_source(source=payload.source, force_refresh=bool(payload.force_refresh))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/scout/recent")
def scout_recent(limit: int = Query(20, ge=1, le=50)):
    """Returns recently scouted content from database cache."""
    return list_recent_scouted(limit=limit)

@app.post("/api/scout/import_repertoire")
def scout_import_repertoire(payload: ScoutImportPayload):
    """Imports an extracted line or variation into the user's Repertoire tree."""
    scout = ContentScout.get_instance()
    try:
        return scout.import_line_to_repertoire(repertoire=payload.repertoire, moves=payload.moves, study_name=payload.study_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/scout/check_line")
def scout_check_line(payload: ScoutCheckLinePayload):
    """Detects if a scouted line or its subline is already present in the user's repertoire."""
    scout = ContentScout.get_instance()
    try:
        return scout.check_line_overlap(repertoire=payload.repertoire, moves=payload.moves)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/scout/check_tree")
def scout_check_tree(payload: ScoutCheckTreePayload):
    """Batch detects repertoire overlap for all explored lines in a session tree."""
    scout = ContentScout.get_instance()
    try:
        return scout.check_lines_overlap(repertoire=payload.repertoire, lines=payload.lines)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/scout/favorite")
def scout_toggle_favorite(payload: ScoutFavoritePayload):
    """Toggles bookmark/favorite status for a scouted item."""
    new_fav = toggle_favorite_scouted(payload.source_id)
    return {"status": "ok", "source_id": payload.source_id, "is_favorite": new_fav}

@app.post("/api/repertoire/import_line")
def repertoire_import_line(payload: ScoutImportPayload):
    """Imports a sequence of moves as a whole line into the user's Repertoire tree."""
    scout = ContentScout.get_instance()
    try:
        return scout.import_line_to_repertoire(repertoire=payload.repertoire, moves=payload.moves, study_name=payload.study_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/repertoire/check_line")
def repertoire_check_line(payload: ScoutCheckLinePayload):
    """Checks if a whole line or subline is already present in the user's repertoire."""
    scout = ContentScout.get_instance()
    try:
        return scout.check_line_overlap(repertoire=payload.repertoire, moves=payload.moves)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/repertoire/check_tree")
def repertoire_check_tree(payload: ScoutCheckTreePayload):
    """Batch checks multiple explored lines against the user's repertoire."""
    scout = ContentScout.get_instance()
    try:
        return scout.check_lines_overlap(repertoire=payload.repertoire, lines=payload.lines)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

''',
    )

    # The snapshot lives in audit/scripts/, so the pre-fix
    # `os.path.dirname(os.path.dirname(__file__))/frontend` would resolve to a
    # non-existent directory and silently skip `app.mount("/", StaticFiles(...))`
    # -- which is exactly the mount whose shadowing is under test (CODE-02-02).
    # Pin the real frontend directory so the snapshot is a faithful drop-in.
    text = text.replace(
        'FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")',
        'FRONTEND_DIR = %r' % os.path.join(PROJECT_ROOT, "frontend"),
    )
    assert 'FRONTEND_DIR = %r' % os.path.join(PROJECT_ROOT, "frontend") in text

    with open(DST, "w", encoding="utf-8") as f:
        f.write(text)

    import ast
    ast.parse(text)
    print(f"wrote {DST} ({len(text.splitlines())} lines) -- syntax OK")


if __name__ == "__main__":
    sys.exit(main())
