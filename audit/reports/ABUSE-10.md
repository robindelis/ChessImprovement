# ABUSE-10 — `RepertoireManager.analyze_user_games()` leaks the Stockfish child process on any mid-sync exception (`analyzer.close()` is not in a `finally`)
- **Severity:** medium
- **Type:** security (resource exhaustion) / crash
- **File:** `backend/repertoire.py`
- **Line(s):** 1846 (`analyzer = GameAnalyzer()`), 1889 (`analyzer.close()`), 1890 (`conn.close()`)
- **Component:** engine lifecycle / sync pipeline
- **Repro script:** `audit/scripts/atk5_engine_leak_repro.py`
- **Detected by:** ATK-5

## Expected
Every code path that spawns a child process must release it in a `finally`.
`POST /api/repertoire/sync_games` calls `analyze_user_games`, which spawns a
Stockfish process configured `Hash: 64, Threads: 2` (~240–260 MB RSS, measured)
and analyses every fetched game's opening plies at depth 12 — a long,
failure-prone loop that makes network calls (`get_explorer_game_count`) and parses
untrusted PGN text from Lichess. Any exception inside the loop must still `quit()`
the engine.

## Actual
```python
        analyzer = GameAnalyzer()            # line 1846  -> spawns stockfish
        conn = get_db()
        cursor = conn.cursor()
        …
        for g in games:
            …
            blunders = analyzer.analyze_game_openings(g)     # deep engine work + parse
            if blunders:
                for b in blunders:
                    try:
                        b["opening_games"] = self.get_explorer_game_count(b["fen_before"])
                    except Exception:
                        b["opening_games"] = 0
                analyzer.save_blunders_to_db(blunders)
            synced_count += 1

        analyzer.close()      # line 1889  <-- NOT in a finally
        conn.close()          # line 1890
```

Note the contrast: a concurrent fixer has *already* hardened the two sibling
paths in this session — `backend/api.py:492-493` now has
`finally: analyzer.close(); conn.close()` for `POST /api/sync`, and
`backend/repertoire.py:2004-2007` has `finally: analyzer.close(); conn.close()`
for `stream_analyze_user_games`. This third path was missed.

### Observed (`atk5_engine_leak_repro.py`, Lichess fetch stubbed)
```
  analyze_user_games raised RuntimeError: simulated failure AFTER the engine was spawned
  stockfish processes before: 1  after: 2
  leftover stockfish pids: ['515557', '516553']

  (backend/repertoire.py:1889 `analyzer.close()` is NOT in a finally:)
      13:         analyzer = GameAnalyzer()
      69:         analyzer.close()
```

The engine process that was spawned inside the loop is still alive after the
exception propagated out of the function.

## Root cause
`analyzer.close()` and `conn.close()` are on the normal-exit path only. Any
exception raised between line 1846 and line 1889 — `analyze_game_openings`
(chess parse / engine `EngineTerminatedError` / `EngineError`),
`get_explorer_game_count`, `save_blunders_to_db` (sqlite `OperationalError` /
`IntegrityError`), or the `INSERT OR REPLACE INTO games` — skips both cleanups.
python-chess keeps a background thread per `SimpleEngine`, so the child is never
reaped by GC.

## Exploit / escalation
1. **Repeatable leak from a web page.** `POST /api/repertoire/sync_games` is
   unauthenticated and CORS-reachable (ABUSE-03). Each call that hits a failure
   mode (Lichess rate-limit → `fetch_user_games` returns `[]` → early `return`
   *without* spawning, so the attacker instead needs a partial failure: a game
   whose `moves` string makes `board.parse_san` blow up past the retry, or a
   `get_explorer_game_count` network timeout raising through the `except` guard's
   own `self.get_explorer_game_count` call) leaves one 250 MB engine behind.
   Fifty such calls ≈ 12 GB RSS.
2. **Adjacent — the leaked process is also a CPU liability:** it keeps its own
   2 Stockfish threads, so leaked engines compete with the one
   `AsyncEngineEvaluator` engine used by the eval bar.
3. **Adjacent — `conn` leaks too**, holding a WAL read lock, which is the classic
   precursor to `database is locked` for the rest of the app
   (`sqlite3.connect(..., timeout=30.0)` in `backend/db.py:19`).

**Required fix:** wrap the whole post-`GameAnalyzer()` body in
`try: … finally: analyzer.close(); conn.close()`, in all three call sites.
