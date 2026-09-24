# SYNC-04 — malformed Lichess-Explorer JSON causes five different unhandled 500s in `get_candidate_moves_2000`
- **Severity:** medium
- **Type:** crash
- **File:** backend/repertoire.py
- **Line(s):** 218-320 (payload parsing loop)
- **Component:** 2000+ opening explorer
- **Repro script:** `audit/scripts/atk3_explorer_hostile_repro.py`, `audit/scripts/atk3_reverify_matrix.py` (section `SYNC-05b`)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
Untrusted upstream JSON is validated/coerced before arithmetic; a malformed payload yields
the same `{"moves": [], "total_games": 0}` shape the code already produces for a network
failure, never a 500.

## Actual
`data = resp.json()` and the whole formatting loop run **outside** the `try/except` that
guards the HTTP call, and every count is fed straight into `+`:

```python
total_games = (data.get("white", 0) + data.get("draws", 0) + data.get("black", 0))
...
for m in raw_moves:                 # raw_moves = data.get("moves", [])
    san = m.get("san")              # AttributeError if m is a list/str/None
    ...
    w_count = m.get("white", 0)
    move_games = w_count + d_count + b_count   # TypeError if any count is a str/None
```

Observed (mocked explorer, `GET /api/repertoire/explore`):

```
  --- moves is a list of lists (wrong shape)
      HTTP 500  body[:180]=Internal Server Error
  --- counts are strings
      HTTP 500  body[:180]=Internal Server Error
  --- counts are null
      HTTP 500  body[:180]=Internal Server Error
  --- payload root is a list
      HTTP 500  body[:180]=Internal Server Error
  --- moves dict (not a list)
      HTTP 500  body[:180]=Internal Server Error
  [LIVE ] SYNC-05b: 3 distinct malformed explorer shapes -> unhandled 500
```

Note that a *legitimate* Lichess response is not the only way to reach this: the community
explorer omits `white`/`draws`/`black` for a single-move position in some deployments, and
any schema drift upstream (a rename to `results`) hits the same path.

## Root cause
No type validation between `resp.json()` and the arithmetic. `data = resp.json()` is inside
`try:` (so a non-JSON body is handled), but the subsequent `data.get(...)` / `m.get(...)` /
`+` expressions are not.

## Exploit / escalation
1. **`GET /api/repertoire/tree` inherits it:** `get_repertoire_tree` calls
   `get_candidate_moves_2000` for every opponent position, so a single bad upstream reply
   makes the whole Repertoire Builder tab 500.
2. **Cache-write amplification:** the negative counts are *accepted* and cached for 7 days:
   ```
     --- negative counts
         HTTP 200 ... "total_games":-150,"white_total":-50,"draws_total":-50,"black_total":-50
         explorer_cache row persisted: {"total_games": -150, ...
   ```
   so a single bad response poisons the position for a week.
3. **Hard-coded defaults hide partial data:** `m.get("white", 0)` turns a *missing* key into
   `0`, which is indistinguishable from "0 games played", so a schema rename silently
   produces an empty-but-successful explorer table.
4. **`averageRating` is passed through unvalidated** into `candidate_moves`, and the
   frontend interpolates it into `innerHTML` (see SYNC-12).
