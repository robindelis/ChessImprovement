# SYNC-08 — every blunder-detail view performs a blocking, un-cached, un-rate-limited outbound HTTPS call
- **Severity:** medium
- **Type:** wrong-result
- **File:** backend/api.py
- **Line(s):** 826-830
- **Component:** `/api/blunders/{blunder_id}`
- **Repro script:** `audit/scripts/atk3_api_bounds_repro.py` (section ATK3-11d), `audit/scripts/atk3_reverify_matrix.py` (section `SYNC-09`)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
The master/opening statistics shown on the detail screen are read from the existing
`explorer_cache` (which every other explorer read in the app uses) and the endpoint is
declared `async` or otherwise non-blocking.

## Actual
```python
# backend/api.py:826-830 (inside a plain `def`, i.e. run in the threadpool)
client = LichessClient()
explorer_stats = client.get_opening_explorer_stats(data["fen_before"])
data["master_stats"] = explorer_stats
```
`get_opening_explorer_stats` (`backend/lichess_client.py:101-120`) issues
`requests.get(..., timeout=8)` to `explorer.lichess.org/masters`, and on a 401 a *second*
`requests.get(..., timeout=8)` to `explorer.lichess.org/lichess`. There is no read of
`explorer_cache`, no de-duplication and no rate limiting, so the worst case is 16 s of
blocked worker thread per request.

Observed:

```
  ATK3-11d — /api/blunders/{id} performs a blocking outbound explorer call
  one detail request took 0.30s, explorer calls=1 (fen='rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQK...')
  master_stats returned to the client: {"white": 1, "draws": 0, "black": 1, "moves": []}
  [LIVE ] SYNC-09: every detail view blocks the worker thread for the full explorer timeout
```

(The 0.30 s is the injected stub delay; the production ceiling is 8 s, doubled on 401.)

## Root cause
`explorer_cache` is only consulted by `get_candidate_moves_2000`; the detail endpoint
bypasses it entirely, and the endpoint is a synchronous `def`, so Starlette runs it in the
shared anyio threadpool where a blocking socket starves every other request.

## Exploit / escalation
1. **Threadpool starvation:** with a bounded default threadpool, a handful of concurrent
   detail requests against a slow/unreachable explorer pins every worker for up to 16 s each;
   the whole API (including the SSE endpoints that share the pool) stalls.
2. **Lichess rate limiting:** `/api/games/user` and the explorer have separate but related
   limits, and an unauthenticated caller can drive the detail endpoint in a loop to burn the
   app's IP budget — the app then gets 429s (handled by a silent `return None`, so the UI
   just shows no statistics with no error).
3. **Token scope leak:** the request carries `self.headers`, i.e. the Lichess API Bearer
   token, to `explorer.lichess.org` (see SYNC-13).
4. **Unvalidated passthrough:** the whole upstream dict is returned to the client as
   `master_stats` with no schema check.
