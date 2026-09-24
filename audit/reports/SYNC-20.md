# SYNC-20 — `/api/repertoire/tree` calls `get_candidate_moves_2000()` **internally**, so any guard on `explore_position_2000` protects nothing: the same 4 malformed payloads 500 the tree, and one poisoned `explorer_cache` row breaks four other endpoints
- **Severity:** high
- **Type:** crash
- **File:** backend/repertoire.py
- **Line(s):** 1696-1722 (`get_repertoire_tree` → the bypass), 1717 (`opp_m["san"]` subscript); 234 (`get_explorer_game_count` read), 1800 (`get_game_gaps` filter), 330-393 (the unvalidated writer)
- **Component:** 2000+ opening explorer — the writer **and** three further readers of `explorer_cache`
- **Repro script:** `audit/scripts/atk3_re05_explorer_retest.py` (sections `RE05-B`, `RE05-C`, `RE05-F`)
- **Detected by:** ATK-3 (RETEST) — the "different route that bypasses the api.py boundary" the retest brief asked for

## Expected
There is exactly one place where an untrusted upstream JSON payload becomes a
`explorer_cache` row, and it is behind a validated boundary; and no other endpoint reads
`explorer_cache.data_json` without validating it.

## Actual
`explore_position_2000` (`backend/api.py:1804-1813`) validates only the FEN and calls
`get_candidate_moves_2000(fen, ratings=ratings, color=color)`. But the **same function is
called from inside `get_repertoire_tree`**, once per opponent position in the user's tree:

```python
# backend/repertoire.py:1696-1722  (inside get_repertoire_tree)
cand_cache: Dict[str, Dict] = {}
...
for pos_fen, op_name, eco in opp_positions:
    if pos_fen not in cand_cache:
        cand_cache[pos_fen] = self.get_candidate_moves_2000(pos_fen, color=repertoire)
    ...
        gaps.append({... "opponent_san": opp_m["san"], ...})   # 1717: bare subscript
```
`/api/repertoire/explore` is therefore *not* the only route into the SYNC-04/SYNC-05 code.
`/api/repertoire/tree` — the endpoint the Repertoire Builder calls on every position
navigation — reaches the identical unvalidated arithmetic, the identical
`INSERT OR REPLACE INTO explorer_cache` write, and the identical 500s.

### Variant A — the same 4 payload shapes, a different endpoint
```
   moves is a dict                HTTP 500  <-- from /api/repertoire/tree
   a move is a string             HTTP 500  <-- from /api/repertoire/tree
   a move has string counts       HTTP 500  <-- from /api/repertoire/tree
   root is a list                 HTTP 500  <-- from /api/repertoire/tree
```
(Identical results through `/api/repertoire/explore`, so FIXER-B's claimed api.py boundary is
currently **absent** rather than bypassed — `explore_position_2000` contains no payload
validation at all, and `get_candidate_moves_2000` still does
`data.get("white", 0) + data.get("draws", 0) + data.get("black", 0)` and
`m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)` outside any `try`.)

### Variant B — a move with **no `san` key** poisons the cache and 500s the *next* request
`"a move has NO 'san' key"` → **HTTP 200 and `cached=True`**: the payload is written, and
`get_repertoire_tree:1717` reads `opp_m["san"]` from it. Any later tree request that hits that
cache row hits a `KeyError`.

### Variant C — ONE poisoned `explorer_cache` row, FOUR other consumers
`explorer_cache` is read by three functions that FIXER-B did not touch, and each one has a
different failure mode (`audit/scripts/atk3_re05_explorer_retest.py` RE05-C):

| stored `total_games` | `get_explorer_game_count` | `GET /api/repertoire/tree` | `GET /api/blunders/{id}` | `analyze_user_games` |
|---|---|---|---|---|
| `Infinity` | `inf` | **HTTP 400** | **HTTP 422** | writes `opening_games = inf` (REAL) |
| `NaN` | `nan` | **HTTP 500** | **HTTP 422** | writes `opening_games = NULL` |
| `1e300` | `1e300` | HTTP 200 | **HTTP 422** | writes `opening_games = 1e300` (REAL) |
| `10**300` | huge int | HTTP 200 | **HTTP 422** | **raises `OverflowError`** |

Verbatim:
```
   --- total_games = Infinity ---
   GET /api/repertoire/tree  -> HTTP 400 {"detail":"Numeric value out of range."}
   GET /api/blunders/9      -> HTTP 422 {"detail":"Invalid float value: NaN and Infinity are not allowed."}
   analyze_user_games -> blunders.opening_games = {'opening_games': inf, 't': 'real'}

   --- total_games = NaN ---
   GET /api/repertoire/tree  -> HTTP 500 {"detail":"Internal server error."}
   analyze_user_games -> blunders.opening_games = {'opening_games': None, 't': 'null'}

   --- total_games = a huge int ---
   analyze_user_games RAISED OverflowError('Python int too large to convert to SQLite INTEGER')
```
The `NaN` → 500 is `repertoire.py:1800`: `int(r["opening_games"] or 0) < min_games` →
`int(nan)` → `ValueError`. The `Infinity` → 400 is the same line via
`int(inf)` → `OverflowError`. The `10**300` case aborts `analyze_user_games` mid-batch, i.e.
it is a remote-input-triggered 500 on a *write* endpoint (compound with SYNC-16 and SYNC-22).

A non-integer REAL is now **persisted** into `blunders.opening_games`, an `INTEGER` column,
so the corruption outlives the cache row (7 days) and outlives a manual cache clear.

## Root cause
`explorer_cache` is treated as a trusted table with exactly one reader, but it has four, and
the *writer* is a shared helper reachable from two endpoints. The `ratings` column is also
part of the primary key and is completely unvalidated on the route
(`ratings: str = Query("2000,2200,2500")`), so every distinct `ratings` value is a fresh
upstream query **and** a fresh row for the same position — an unbounded row multiplier behind
a single `GET`.

## Exploit / escalation
1. **A single poisoned row takes out the whole Repertoire Builder tab** (400/500 on
   `/api/repertoire/tree`), the blunder review screen (422 on `/api/blunders/{id}`), and any
   sync. The `explorer_cache` TTL is 7 days, and there is no admin UI to clear it.
2. **Put the malformed payload in through the tree route and it persists.** Variant B shows a
   payload shape that is *accepted* (200) and cached; a shape that is *rejected* (500) is not,
   so an attacker/buggy upstream only needs to find an accepted-but-hostile shape, and there
   is no second validation pass on the read side.
3. **Persisted REAL in `blunders.opening_games`** breaks the `min_games` cut-off permanently
   for that blunder: `int(inf) < 30` raises, `int(1e300) < 30` is False → the row is silently
   *included* for every cut-off value, and the `ORDER BY cpl_loss` list now contains a row
   whose filter cannot be evaluated.
4. **`ratings` as an unbounded cache key.** `GET /api/repertoire/explore?fen=…&ratings=<20 000
   chars>` returns 200 and is a guaranteed cache miss, so a loop over distinct `ratings`
   values is a loop over live upstream requests plus a new `explorer_cache` row each time.

## Fix direction
Validate and coerce the explorer payload in **one** place — inside
`get_candidate_moves_2000`, before the arithmetic and before the cache write — not at an
`api.py` route that the tree path bypasses. Add a `coerce_count(x) -> int` that rejects
non-finite/out-of-range values, `math.isfinite()`-guard before `json.dumps`, drop any move
entry that is not a dict or has no parseable `san`, and validate `total_games` in
`get_explorer_game_count` and `get_game_gaps` (`isinstance(int)` + a sane upper bound) so a
corrupt row degrades to "unknown" instead of raising. Bound `ratings` on the route.
