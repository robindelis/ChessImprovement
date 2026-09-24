# SYNC-22 — the other half of SYNC-03 is still open: an exception inside the `repertoire.py` write loop leaks the connection **with a live write transaction**, holding SQLite's write lock for the full 30 s timeout
- **Severity:** high
- **Type:** crash
- **File:** backend/repertoire.py
- **Line(s):** 1940-1957 (`sync_recent_games`), 2032-2066 (`stream_sync_recent_games`), 2313-2331 (`analyze_user_games`), 2421-2434 (`stream_analyze_user_games`)
- **Component:** game ingestion — connection lifecycle
- **Repro script:** `audit/scripts/atk3_re06_gameslist_retest.py` (section `RE06-D`)
- **Detected by:** ATK-3 (RETEST)

## Expected
Whatever happens inside the write loop, the batch is either fully committed or fully rolled
back, the connection is closed, and no other writer is blocked afterwards.

## Actual
All four writers open one connection, share one implicit transaction whose `commit()` is the
last statement of the function, and close the connection after it:

```python
# backend/repertoire.py:1912-1957 (sync_recent_games)
conn = get_db()
cursor = conn.cursor()
synced = 0
for g in games:                      # <-- no try / except / finally
    ...
    cursor.execute("""INSERT OR REPLACE INTO games (...)""", (...))   # 1940
    synced += 1
conn.commit()                          # 1956 - skipped on the error path
conn.close()                          # 1957 - skipped on the error path
```
sqlite3's implicit transaction opens at the **first** DML statement, so after game 2 of a batch
the connection holds SQLite's single write lock. When an exception escapes, neither
`commit()` nor `close()` runs, and the connection stays alive because the propagating
exception's traceback keeps the frame — and the frame's locals — referenced.

Measured, with the failure on the **third** record so the transaction is genuinely open
(`audit/scripts/atk3_re06_gameslist_retest.py` RE06-D):

```
   sync_recent_games raised: ProgrammingError("Error binding parameter 2: type 'dict' is not supported")
   games committed: 0
   a second writer (2 s timeout) was BLOCKED after 2.00s OperationalError('database is locked')
   -> BROKEN  a failed batch releases the SQLite write lock
   after dropping the traceback + gc.collect(), the lock is FREE
```
`backend/db.py:139` opens every connection with `timeout=30.0`, so the real block is **30
seconds**, during which every other write in the app — `/api/sync`, `/api/repertoire/move`,
the drill endpoint, the explorer's cache write, the next sync — waits, then raises
`database is locked`. It is released only when the traceback is dropped, which is
indeterminate.

## Root cause
Same class as the original SYNC-03, on the paths SYNC-03's repro did not exercise: the reader
is now robust, but the *writers* were never given a `try/finally`. `POST /api/sync` has one
(`api.py:1050-1052`); `repertoire.py` has none.

## Exploit / escalation
1. **Three untrusted field types reach the writers as raw Python objects** and raise inside the
   bind (measured, all via `sync_recent_games` and via
   `GET /api/games/sync_recent_stream`):
   * `"moves": ["e4","e5"]` (a list) → `ProgrammingError: Error binding parameter 13`
   * `"winner": {"a": 1}` (a dict) → `ProgrammingError: Error binding parameter 10`
   * `"speed": {"k": 1}` (a dict) → `ProgrammingError: Error binding parameter 2`
   * `"createdAt": [1, 2]` (a list) → `ProgrammingError: Error binding parameter 4`
   The same four records make `POST /api/sync` answer **HTTP 500** (`RE03-B`), so the "fixed"
   endpoint is not robust either — it just happens to have a `finally`.
2. **The lock outlives the response.** Starlette holds the exception (and therefore the
   traceback, and therefore the connection) until the 500 has been sent and the exception
   object is collected. Any client that syncs twice in a row is the victim of its own first
   request.
3. **A client that closes the connection does not help**: the SSE writers
   (`stream_sync_recent_games` / `stream_analyze_user_games`) already have a `try/finally`
   around the loop, but the `get_db()` call immediately before it (`2000`/`2373`) is not
   protected, and `analyze_user_games` (2329-2330) leaks the Stockfish handle as well
   (SYNC-16 escalation 3).
4. **Nothing rolls the batch back explicitly** either: the partial rows are only discarded when
   the leaked connection is eventually finalised, so between the failure and the GC the DB
   shows a half-written batch to any other reader.

## Fix direction
Wrap each writer's body in `try/except/finally` with `conn.rollback()` in the `except` and
`conn.close()` + `analyzer.close()` in the `finally` (the shape `api.py:1050-1052` already
uses), and coerce the three untrusted fields (`moves`, `speed`, `createdAt`, `winner`) to
`str`/`int`/`float` before binding them.
