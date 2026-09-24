# SYNC-16 — `analyze_user_games` / `stream_analyze_user_games` still stamp and COMMIT `analyzed_at` BEFORE the analysis, and one bad game 500s the whole request
- **Severity:** high
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 2313-2326 (write + commit, then analyse at 2328) and 2421-2434 / 2438-2439 (SSE twin); 2271-2342, 2355-2461
- **Component:** `POST /api/repertoire/sync_games` and `GET /api/repertoire/sync_games_stream` → `RepertoireManager.analyze_user_games`
- **Repro script:** `audit/scripts/atk3_re02_writer_retest.py` (section `RE02-E`)
- **Detected by:** ATK-3 (RETEST) — the CODE-02-04 variant FIXER-K handed over

## Expected
`games.analyzed_at` is the "do not re-analyse" flag (AGENTS.md §5.1: *"0 = opening not
analyzed yet"*). It must be written **after** the analysis succeeds, inside the same
transaction as the blunder write, so that a crashed or failed analysis leaves the game
pending and the next sync retries it. One failing game must never abort the library.

`POST /api/sync` was fixed to exactly this contract (`backend/api.py:993-1029`: `ON CONFLICT
DO UPDATE`, `analyzed_at = 0` on the pending write, then `UPDATE games SET analyzed_at = ?`
only after `analyze_game_openings` returned). `repertoire.py` was never given the same
treatment.

## Actual
```python
# backend/repertoire.py:2313-2326  (analyze_user_games)
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
    user_color, g.get("winner", "draw"),
    opening_data.get("name", ""), opening_data.get("eco", ""),
    g.get("moves", ""), int(time.time())      # <-- analyzed_at = NOW
))
conn.commit()                                   # <-- COMMITTED

blunders = analyzer.analyze_game_openings(g)    # <-- line 2328: the analysis runs AFTER
```
The loop has **no `try`/`except` per game and no `try`/`finally`** around the connection or
the engine, so the first failure propagates out of the whole endpoint.

Observed (isolated temp DB, real `analyze_user_games` with a scripted engine that raises,
`audit/scripts/atk3_re02_writer_retest.py` RE02-E):

```
      analyze_user_games RAISED RuntimeError('engine died / database locked')
      games row after the FAILED analysis: {'id': 'c1', 'analyzed_at': 1790449954}
   -> BROKEN  a failed analysis leaves the game pending (analyzed_at = 0)
              analyzed_at=1790449954  -> the game is skipped forever by every later sync
      POST /api/repertoire/sync_games -> HTTP 500 {"detail":"Internal server error."}
```

The game is permanently marked "already analysed" with **zero blunders stored**, and the
endpoint answers a bare 500 for the whole library — every other game in the batch is lost
too, not just the one that failed.

## Root cause
`analyzed_at` is the *output* of a successful analysis but is written as *input* to it, and
the whole write loop is unguarded:

```python
# backend/repertoire.py:2271-2342 — no per-game try/except, no try/finally
for g in games:
    ...
    cursor.execute("INSERT OR REPLACE INTO games (... analyzed_at) VALUES (?, ...)")
    conn.commit()
    blunders = analyzer.analyze_game_openings(g)   # may raise
    if blunders:
        ...
        analyzer.save_blunders_to_db(blunders)
analyzer.close()      # 2340 - skipped when the loop raises
conn.close()          # 2341 - skipped when the loop raises
```

`INSERT OR REPLACE` is a second, independent problem: it is DELETE+INSERT in SQLite, so the
row is destroyed and recreated (new `rowid`, all unlisted columns reset) on **every** sync of
an already-analysed game. With `PRAGMA foreign_keys` off (AGENTS.md §5) the `blunders` rows
survive, but nothing about the row identity is preserved, and the day `foreign_keys` is ever
turned on this becomes a real cascade.

## Exploit / escalation
1. **Silent permanent loss (verified).** Any engine/pool error, DB lock, or
   `OperationalError` inside `analyze_game_openings` marks the game done. It never appears in
   the gap list, never enters the drill rotation, and the user is told nothing — the sync
   reports `status: "success"` on the *next* run because the skip guard fires first.
2. **Whole-library 500 (verified).** `POST /api/repertoire/sync_games?max_games=50` returns
   500 for one bad game, so the desktop app's "sync & analyse" button loses 49 good analyses
   with no per-game error list (contrast `api.py:1042-1049`, which does report
   `failed_games` + `errors`).
3. **Leaked engine + leaked connection.** `analyzer.close()` and `conn.close()` are after the
   loop, so the Stockfish handle from the process-wide pool is **never returned** to
   `get_sync_engine_pool()` on this path. The pool has `CHESS_ENGINE_MAX_PROCS = 2`, so two
   such failures permanently shrink the pool to zero and every later `/api/sync` starts
   raising `EngineBusy` → 503. (Companion to the 30 s lock leak in SYNC-22.)
4. **The SSE twin is identical (2421-2439).** `stream_analyze_user_games` catches per game but
   still stamps `analyzed_at` first, and its `finally` block only runs if the `try` is
   entered — a failure *before* the loop (e.g. `get_db()`) still leaks.

## Evidence (verbatim probe output)

```
==============================================================================
  RE02-E  CODE-02-04 RESIDUAL: repertoire.analyze_user_games
==============================================================================
      analyze_user_games RAISED RuntimeError('engine died / database locked')
      games row after the FAILED analysis: {'id': 'c1', 'analyzed_at': 1790449954}
   -> BROKEN a failed analysis leaves the game pending (analyzed_at = 0) analyzed_at=1790449954  -> the game is skipped forever by every later sync

      POST /api/repertoire/sync_games -> HTTP 500 {"detail":"Internal server error."}
```
