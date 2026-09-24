# SYNC-05 — an `Infinity`/`NaN` count from the explorer permanently poisons `explorer_cache` for that position
- **Severity:** high
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 325-341 (cache write)
- **Component:** explorer cache
- **Repro script:** `audit/scripts/atk3_explorer_hostile_repro.py` (section ATK3-08b), `audit/scripts/atk3_reverify_matrix.py` (section `SYNC-05`)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
A payload that cannot be serialised as strict JSON is rejected and **not** persisted, so the
next request re-queries the upstream and can recover.

## Actual
Python's `json.loads` accepts the non-standard literals `Infinity`, `-Infinity` and `NaN`.
The response is written into `explorer_cache` with `json.dumps(..., allow_nan=True)` (the
default), and the next request reads it back and hands it to FastAPI, whose `JSONResponse`
uses `allow_nan=False`. The position is then permanently broken for the whole 7-day cache
TTL — with **no network request at all** on the second attempt.

Observed:

```
  ATK3-08b — Infinity poison is PERSISTENT (7-day cache) and 500s forever
  1st request (network): 422 {"detail":"Invalid float value: NaN and Infinity are not allowed."}
  cached row: {"fen": "rnbqkbnr/... RNBQKBNR w KQkq - 0 1", "total_games": Infinity, "white_total": Infinity, "d
  2nd request (served from cache, no network at all): 422 {"detail":"Invalid float value: NaN and Infinity are not allowed."}
  -> the position is permanently broken until the cache row is deleted manually
  [LIVE ] SYNC-05: poisoned explorer_cache row persisted; every later request for that FEN -> 422
```

The identical shape happens with `NaN`:

```
  --- NaN counts
      HTTP 422 {"detail":"Invalid float value: NaN and Infinity are not allowed."}
      explorer_cache row persisted: {"...","total_games": NaN,"white_total": NaN,...
```

## Root cause
```python
# backend/repertoire.py:336-341
if req_success and (len(candidate_moves) > 0 or total_games == 0):
    cursor.execute("""INSERT OR REPLACE INTO explorer_cache (fen, ratings, data_json, updated_at)
                      VALUES (?, ?, ?, ?)""",
                   (norm_fen, ratings, json.dumps(result_payload), int(time.time())))
```
`json.dumps` defaults to `allow_nan=True`, so non-finite floats are happily serialised into
the cache. Nothing checks `math.isfinite()` before persisting, and the read path
(lines 218-245) happily re-serialises them into the HTTP response.

## Exploit / escalation
1. **Any position, permanently:** the cache key is the 4-token FEN, so one poisoned row
   breaks that specific position for 7 days. The user sees "Invalid float value" on a
   position that is perfectly valid, with no way to clear it from the UI.
2. **`NaN` reaches SQL too:** `get_explorer_game_count()` returns
   `d.get("total_games", 0)` from the same poisoned row, and `get_game_gaps` writes it into
   `blunders.opening_games`. `NaN >= min_games` is `False`, so the blunder is filtered out
   of the gap list; `Infinity` is stored as a REAL and always passes the cutoff.
3. **Absurd magnitudes are accepted and cached** (verified: `1e15` → `popularity_pct`
   100.0, cached for 7 days), so a single bogus response permanently distorts the
   candidate table.
4. **Fix direction:** reject the payload before caching
   (`math.isfinite(total_games)` and finite per-move counts), and/or use
   `json.dumps(result_payload, allow_nan=False)` inside a `try` so a non-finite value can
   never reach the cache in the first place.
