# SYNC-07 — `/api/blunders` has an unbounded `limit` (SQLite `LIMIT -1` = no limit) and `/api/blunders/{id}` 500s on out-of-int64 ids
- **Severity:** medium
- **Type:** crash
- **File:** backend/api.py
- **Line(s):** 739-748 (`limit: int = 50`), 791-800 (`blunder_id: int` bound to SQLite)
- **Component:** blunder list + detail endpoints
- **Repro script:** `audit/scripts/atk3_api_bounds_repro.py`, `audit/scripts/atk3_reverify_matrix.py` (section `SYNC-08`)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
Pagination parameters are bounded like every other list endpoint in the file
(`/api/games/recent` uses `Query(25, ge=1, le=100)`), and an id that cannot exist in an
`INTEGER PRIMARY KEY` returns 404/422, not 500.

## Actual
Observed:

```
  ATK3-11b — /api/blunders limit is UNBOUNDED (SQLite: LIMIT -1 == no limit)
  limit=50        HTTP 200 rows=4
  limit=-1        HTTP 200 rows=4
  limit=0         HTTP 200 rows=0
  limit=1000000   HTTP 200 rows=4
  limit=1e9       HTTP 422 body[:180]=1
  limit=abc       HTTP 422 body[:180]=1
  -> /api/blunders has NO ge/le bound and NO offset; LIMIT -1 disables the cap entirely

  ATK3-11c — /api/blunders/{id} bounds
  id=10157                        HTTP 200 {...}
  id=0                            HTTP 404
  id=-1                           HTTP 404
  id=99999999                     HTTP 404
  id=abc                          HTTP 422
  id=1.5                          HTTP 422
  id=9223372036854775808          HTTP 500 Internal Server Error
  id=99999999999999999999999999   HTTP 500 Internal Server Error
  [LIVE ] SYNC-08: limit=-1 unbounded; out-of-int64 id -> 500
```

Each returned row also carries a parsed `refutation_pv` array, so `limit=-1` on a large
library is a full dump of the blunder corpus (hundreds of rows × PV arrays) in one
unauthenticated response.

## Root cause
```python
# backend/api.py:739-748
def list_blunders(color=None, eco=None, min_loss=None, category=None, limit: int = 50):
    ...
    query += " ORDER BY b.cpl_loss DESC LIMIT ?"
    params.append(limit)
```
`limit` is a bare `int` with no `Query(..., ge=1, le=…)`, and there is no `offset`.
```python
# backend/api.py:791-800
def get_blunder_detail(blunder_id: int):
    ...
    cursor.execute("... WHERE b.id = ?", (blunder_id,))
```
Python's `int` accepts arbitrarily large values; `sqlite3` raises
`OverflowError: Python int too large to convert to SQLite INTEGER` and nothing catches it.

## Exploit / escalation
1. **Unauthenticated dump:** with `allow_origins=["*"]` on the CORS middleware, any web page
   the user visits can read the whole blunder corpus from `http://127.0.0.1:38281` with
   `?limit=-1`; combined with `/api/games/recent?limit=100` and `/api/repertoire/tree` this
   is the entire private dataset.
2. **Same overflow elsewhere:** any endpoint that binds a path/query `int` straight into a
   SQLite parameter has the same 500. `min_loss` is only applied `if min_loss:`, so
   `min_loss=0` is silently ignored (currently harmless, but it means the filter cannot be
   expressed).
3. **No `offset`:** the only way to page is to fetch everything and slice client-side, which
   is what forces the unbounded limit in the first place.
