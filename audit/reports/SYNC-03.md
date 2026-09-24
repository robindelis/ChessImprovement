# SYNC-03 — one non-dict NDJSON line aborts the whole game sync with a 500 and leaves the write transaction open for 30 s
- **Severity:** high
- **Type:** crash
- **File:** backend/lichess_client.py
- **Line(s):** 89-95 (parser) → backend/repertoire.py:1594, 1684, 1842, 1945 (unguarded `g.get`)
- **Component:** Lichess NDJSON streaming → `sync_recent_games`
- **Repro script:** `audit/scripts/atk3_ndjson_hostile_repro.py`, `audit/scripts/atk3_reverify_matrix.py` (section `SYNC-03`)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
A malformed or unexpected line in a third-party stream is skipped (as blank/broken lines
already are) and the rest of the batch is stored.

## Actual
`fetch_user_games` only catches `json.JSONDecodeError`. Any line that is *valid JSON but not
an object* (`[1,2,3]`, `"text"`, `42`, `null`) is appended to the list, and every caller then
does `g.get("id")` → `AttributeError` → unhandled 500. Because the whole loop shares one
transaction whose `commit()` is at the end, **every** game already written in that batch is
rolled back, and the connection is never closed, so its open write transaction keeps the
SQLite write lock until the traceback object is garbage-collected.

Observed:

```
  STEP A — parser on hostile lines
  raw lines in : 7
  games out    : 3 types: ['dict', 'list', 'dict']
  ids          : ['ok1', [1, 2, 3], 'ok2']

  STEP B — POST /api/games/sync_recent
  HTTP 500 in 0.00s; games rows: []
     (db locked: database is locked) gc.collect()
  [LIVE ] SYNC-03: HTTP 500, 0 games stored, next writer blocked 30s
```

`games rows: []` even though the *first* line was a perfectly valid game — it was inserted
and then lost. The 30 s wait is SQLite's `busy_timeout` in `get_db()`
(`sqlite3.connect(..., timeout=30.0)`).

## Root cause
```python
# backend/lichess_client.py:89-95
for line in resp.iter_lines(decode_unicode=True):
    if line:
        try:
            g = json.loads(line)
            games.append(g)          # <-- no isinstance(g, dict) check
        except json.JSONDecodeError:
            continue
```
```python
# backend/repertoire.py:1594 (sync_recent_games; identical at 1684/1842/1945)
for g in games:
    game_id = g.get("id")            # <-- AttributeError for list/str/int/None
```
and the loop has no `try/except` / `finally`, so `conn.commit()` (line ~1636) and
`conn.close()` are both skipped on the error path.

## Exploit / escalation
1. **`null` line:** a bare `null` line is valid JSON → `games.append(None)` →
   `AttributeError: 'NoneType' object has no attribute 'get'`. Any proxy, mirror or
   corporate TLS terminator that emits a keep-alive blank JSON line kills the sync.
2. **Which endpoints are affected today:** `POST /api/sync` was hardened this round
   (`backend/api.py:341+` now rejects an id-less record and isolates per-game errors),
   but the three `repertoire.py` paths are untouched and still abort the whole batch:
   `sync_recent_games` (:1594), `stream_sync_recent_games` (:1684), `analyze_user_games`
   (:1842), `stream_analyze_user_games` (:1945). For the two *stream* variants the
   generator raises mid-SSE, so the client gets a truncated event stream with no
   `error`/`completed` event at all.
3. **Lock amplification:** because the connection is only released by GC, a burst of
   malformed syncs serialises every other writer in the app behind 30 s busy-timeouts
   (`set_setting`, `/api/repertoire/move`, trainer attempts, …).
4. **Related:** `resp.text[:150]` is interpolated into a `print()` on the non-200 path
   (`lichess_client.py:86`), so upstream error bodies land in the server log verbatim.
5. **Related:** `fetch_user_games` collapses "HTTP 500 / 404 / network error" and "the
   account genuinely has no games" into the same empty list, which is why
   `sync_recent_games` reports `{"status": "no_games"}` for a failed request.
