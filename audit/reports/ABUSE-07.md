# ABUSE-07 — `/api/blunders?limit=` is unbounded: negative/huge limits dump the whole blunder table, and an out-of-int64 limit is an uncaught `OverflowError` → HTTP 500
- **Severity:** medium
- **Type:** crash / security (unbounded resource)
- **File:** `backend/api.py`
- **Line(s):** 739-745 (`limit: int = 50` with no `ge`/`le`), 774-777 (`ORDER BY b.cpl_loss DESC LIMIT ?`); contrast 518-521 (`/api/games/recent` uses `le=100`)
- **Component:** blunders API
- **Repro script:** `audit/scripts/atk5_inf_overflow_repro.py` (section B)
- **Detected by:** ATK-5

## Expected
Every `LIMIT` bound that reaches SQLite must be a validated, clamped integer
(`Query(50, ge=1, le=200)`) exactly as the sibling endpoint already does, so that
(a) a client cannot ask for the entire table and (b) no value can overflow SQLite's
64-bit `INTEGER` binding and raise an unhandled exception.

## Actual
```python
@app.get("/api/blunders")
def list_blunders(
    color: Optional[str] = None,
    eco: Optional[str] = None,
    min_loss: Optional[int] = None,
    category: Optional[str] = None,
    limit: int = 50            # <-- no ge=, no le=
):
    …
    query += " ORDER BY b.cpl_loss DESC LIMIT ?"
    params.append(limit)
    cursor.execute(query, params)
```

### Observed (re-run against the current tree, 2026-09-26)

```
B) /api/blunders  `limit` has NO upper bound
  200  /api/blunders                                 rows=50
  200  /api/blunders?limit=50                        rows=50
  200  /api/blunders?limit=0                         rows=0
  200  /api/blunders?limit=-1                        rows=153     <-- negative LIMIT == no limit in SQLite
  200  /api/blunders?limit=1000000                   rows=153
  200  /api/blunders?limit=9223372036854775807       rows=153
  500  /api/blunders?limit=999999999999999999999     rows=-  'Internal Server Error'
  422  /api/blunders?limit=1e309
  422  /api/blunders?limit=abc
  422  /api/blunders?limit=null
  200  /api/blunders?min_loss=-99999999999           rows=50

contrast /api/games/recent?limit=100000 -> 422 {"detail":[{"type":"less_than_equal",…}]}
```

The 500 is a genuine unhandled exception (the route declares handlers only for
`RequestValidationError` and `ValueError`; `OverflowError` is neither):

```
  RAISES under raise_server_exceptions=True: GET /api/blunders?limit=999999999999999999999
     -> OverflowError: Python int too large to convert to SQLite INTEGER
```

## Root cause
`limit` is declared as a bare `int` with no `ge`/`le` (and `min_loss` likewise
accepts arbitrary negatives), and it is bound straight into the SQL `LIMIT ?`.
Python ints are unbounded; SQLite's are 64-bit, so any `|limit| > 2**63-1`
raises `OverflowError` at `cursor.execute`. The endpoint is also the widest SELECT
in the app (`SELECT b.*, g.*, d.* … LEFT JOIN drill_reviews`), so there is no
index-backed cap protecting memory.

## Exploit / escalation
1. **Full-table exfiltration in one request.** `GET /api/blunders?limit=-1`
   returns all 153 blunder rows (and would return all rows for a user with more
   games), each containing `white_player`, `black_player`, `played_at`,
   `moves_san`, `refutation_pv`, `drill_reviews` counters — i.e. the complete
   private blunder archive. Chained with ABUSE-03 (`ACAO` reflects any origin)
   this is a one-line cross-origin dump of the user's chess history.
2. **Memory/CPU amplification.** `limit=9223372036854775807` removes the bound
   entirely, so a user with a large `blunders` table pays a full
   join + `json.loads` per row (`api.py:776-781` post-processes every row) on
   every request — an unauthenticated amplification primitive.
3. **Adjacent variants on the same route:** `min_loss=-99999999999` disables the
   loss filter (returns everything even at `limit=50`), and `eco`/`category`
   accept arbitrary strings that are only `LIKE`/`=`-bound — no injection, but no
   length cap either, so a 1 MB `eco` value is echoed into a `LIKE` scan.

**Required fix:** `limit: int = Query(50, ge=1, le=200)` (and clamp
`min_loss`/`eco`/`category` lengths), so out-of-range values become 422 instead of
a 500 and negative values can never disable the bound.
