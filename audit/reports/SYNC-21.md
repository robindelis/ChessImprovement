# SYNC-21 — one `"createdAt": Infinity` from the stream is stored verbatim in `games.played_at` and makes `GET /api/games/recent` answer **400 for the entire Recent Games tab**, permanently
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/repertoire.py
- **Line(s):** 1946, 2038 (`int(time.time() * 1000)` default), 2319, 2427 (`int(time.time())` default) — the writers; backend/api.py:95-110 (`normalize_played_at`), 170-190 (`migrate_played_at_units`), 1084 (the call site)
- **Component:** game ingestion → Recent Games list
- **Repro script:** `audit/scripts/atk3_re06_gameslist_retest.py` (sections `RE06-A`, `RE06-F`)
- **Detected by:** ATK-3 (RETEST) — the writer side of SYNC-06 / CODE-02-06

## Expected
`games.played_at` is a canonical millisecond epoch. The writer normalises whatever the
upstream sent (as `POST /api/sync` does via `normalize_played_at(g.get("createdAt", …))`), and
the reader is total: one bad row must never break the list endpoint for every other row.

## Actual
The four `repertoire.py` writers pass `createdAt` **straight through**, with a bare fallback:

```python
# backend/repertoire.py:1946 / 2038  (sync_recent_games / stream_sync_recent_games)
g.get("createdAt", int(time.time() * 1000)),
# backend/repertoire.py:2319 / 2427  (analyze_user_games / stream_analyze_user_games)
g.get("createdAt", int(time.time())),
```
Python's `json.loads` accepts the non-standard literals `Infinity`, `-Infinity` and `NaN`, so
an `Infinity` in the stream becomes a Python `float('inf')`, `sqlite3` binds it as a **REAL**,
and it is stored:

```
   sync_recent_games -> {'status': 'success', 'synced_games': 2, 'total_games': 2}
   stored: [{'id': 'infG', 'played_at': inf, 't': 'real'}, {'id': 'okG', 'played_at': 1700000000000, 't': 'integer'}]
   GET /api/games/recent -> HTTP 400 {"detail":"Numeric value out of range."}
```

The 400 is `normalize_played_at`:
```python
# backend/api.py:104-110
v = int(float(value))        # <-- float('inf') -> OverflowError
```
whose global handler answers `400 {"detail": "Numeric value out of range."}` for **every**
page, offset, `color` and `search` — one poisoned row, one dead tab.

And the repair migration **cannot** fix it, by construction:
```python
# backend/api.py:182-184
"UPDATE games SET played_at = played_at * 1000 "
"WHERE played_at IS NOT NULL AND played_at > 0 AND played_at < ?", (MS_EPOCH_THRESHOLD,)
```
`inf < 1e11` is `False`, so the row is never matched (measured: `SELECT COUNT(*) … WHERE
played_at > 0 AND played_at < 100000000000` → `0` rows). It survives reboots, cache clears and
re-syncs forever.

## Root cause
`normalize_played_at` is the *reader*'s guard and `migrate_played_at_units` is a
*read-time repair*; neither is on the *write* path of the four `repertoire.py` writers, and
the repair's `WHERE` clause assumes any value `>= 1e11` is already correct milliseconds. A
non-finite or out-of-range value therefore has no path back to sanity.

## Exploit / escalation
1. **One line of upstream JSON kills the Recent Games tab permanently.** The row is not
   deletable through any UI; the user's only recovery is `DELETE FROM games WHERE played_at
   > 9223372036854775807 OR played_at IS NULL OR played_at < 0` by hand.
2. **The other `createdAt` values are stored just as raw** (measured, all via
   `sync_recent_games`, all of which return `status: success`):
   | `createdAt` in the stream | stored | `GET /api/games/recent` |
   |---|---|---|
   | `Infinity` | `inf` (REAL) | **HTTP 400** |
   | `1e300` | `1e300` (REAL) | HTTP 200, `played_at_formatted: "Just now"` |
   | `2**62` | `4611686018427387904` | HTTP 200, `"Just now"` (year 211 000 000) |
   | `NaN` | `NULL` | HTTP 200, `NULL` (an unrelated NULL `played_at`) |
   | `"soon"` | `'soon'` (TEXT in an INTEGER column!) | HTTP 200 |
   | `-1` | `-1` | HTTP 200 |
   | `[1, 2]` | — | `sync_recent_games` **raises** `ProgrammingError` (see SYNC-22) |
   A `1e300`/`2**62` row sorts above every real game in `ORDER BY played_at DESC` forever and
   is reported as "Just now" — the newest game in the list is a lie.
3. **`GET /api/games/recent` performs an unbounded `UPDATE` on every call** (200 rows rewritten
   in one request in the measurement) while offering `offset` pagination up to any page. A
   hostile row therefore forces a full-table write on every single page view, and the fix is
   applied *lazily at read time* rather than once, which is why it is still a write on a GET
   (measured: 12 concurrent GETs all returned 200 in 0.18 s, so this is a design defect rather
   than a live 500 — reported for completeness, not as an availability bug).
4. **A TEXT value in an INTEGER column is accepted** (`'soon'`), because nothing validates the
   type at any write site. `normalize_played_at` swallows it to `0` → the game displays as
   "Unknown" with no diagnostic.

## Fix direction
Route all four writers through `normalize_played_at` (move it into `backend/db.py` or
`backend/repertoire.py` so there is no import cycle, exactly as SYNC-17 requires for
`normalize_game_result`), make it total (`math.isfinite` guard, clamp to `[0, 2**53*1000]`),
replace the read-time `migrate_played_at_units()` write with a one-shot boot migration plus a
`CHECK`-style validation, and reject/repair any `played_at` that is not a finite
non-negative integer inside `normalize_played_at` so the reader can never raise.
