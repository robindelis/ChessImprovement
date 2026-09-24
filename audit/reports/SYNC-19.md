# SYNC-19 — the SYNC-15 upsert made re-analysis *idempotent* but not *correct*: blunder rows a re-analysis no longer detects are never removed, and `analyzed_at` guarantees they never will be
- **Severity:** high
- **Type:** wrong-result
- **File:** backend/analyzer.py
- **Line(s):** 471-524 (`save_blunders_to_db`), 405-436 (`_upsert_blunder`), 438-469 (`_collapse_duplicate_rows`); callers backend/repertoire.py:2328-2336, 2438-2447
- **Component:** blunder persistence / gap analysis / spaced repetition
- **Repro script:** `audit/scripts/atk3_re01_schema_retest.py` (section `RE01-E`)
- **Detected by:** ATK-3 (RETEST)

## Expected
Analysis is a **recomputation**, not an append. Re-analysing a game replaces that game's
blunder set: rows that are still detected are updated in place, rows that are no longer
detected are deleted, and new rows are inserted. A row that survives a re-analysis in which
the engine did not produce it is stale data that the gap list and the drill rotation will keep
serving forever.

## Actual
`save_blunders_to_db` is now a correct **upsert** — the SELECT-then-INSERT/UPDATE on
`(game_id, ply, played_move)` and `_collapse_duplicate_rows` make repeated analysis converge
on the same row *count for the rows that still exist*. There is no path that ever **removes** a
row for a game in the batch:

```python
# backend/analyzer.py:496-510
for b in blunders:
    blunder_id, created = self._upsert_blunder(cursor, b, mate_columns)
    summary["inserted" if created else "updated"] += 1
    cursor.execute("""INSERT OR IGNORE INTO drill_reviews (...) VALUES (?, 0, 0, 0, ?, 1.0, 'new')""", ...)
for game_id in sorted({b.get("game_id") for b in blunders}, ...):
    summary["duplicates_removed"] += self._collapse_duplicate_rows(cursor, game_id)
# NOTE: no `DELETE FROM blunders WHERE game_id = ? AND id NOT IN (...)`
```

Observed (isolated temp DB; a ply-9 blunder is recorded, then the same game is re-analysed
with `max_opening_ply = 4` so the engine cannot see ply 9 at all,
`audit/scripts/atk3_re01_schema_retest.py` RE01-E):

```
      before re-analysis: blunders = 1  (a ply-9 blunder exists)
   -> BROKEN  re-analysis drops blunder rows it no longer detects
               ply-9 rows left after a 4-ply re-analysis = 1
      total blunders for staleG: 1
```

The upsert made the write idempotent, which is exactly the guarantee SYNC-15 asked for, and
left the *other* half of the same root cause — the `analyzed_at` flag being the only thing
that ever controlled a game's blunder set — untouched.

## Root cause
Idempotency was implemented as "same input → same rows" rather than "the stored set equals the
computed set". The pre-fix code got set-correctness *by accident*, because the
`if existing["analyzed_at"] > 0: continue` guard meant a game was analysed exactly once; the
moment a real upsert was added, the ability to re-analyse a game with a *different*
configuration became possible and the missing delete became load-bearing.

## Exploit / escalation
1. **Every setting change silently freezes stale blunders (verified).**
   * `max_opening_ply` lowered (the setting exists precisely to be tuned) → blunders beyond the
     new window stay in `blunders`, keep their `cpl_loss`, keep a `drill_reviews` row, and keep
     ranking in `get_game_gaps` → `ORDER BY cpl_loss DESC`.
   * `blunder_threshold_cp` **raised** → blunders that are no longer blunders at all
     (e.g. 160 cp with the threshold now at 200) stay forever.
   * The only way to clear them is to delete the game's rows by hand.
2. **Engine drift is invisible.** A different Stockfish build/depth, or a temporarily broken
   engine that reports a shallow score, produces *fewer* blunders — the extra ones are kept and
   they may no longer be mistakes at all.
3. **The drill rotation trains on phantoms.** `drill_reviews` has a `UNIQUE(blunder_id)`
   row per stale blunder, so `/api/repertoire/train` keeps drilling a position the user has
   already fixed, and `mastered_count` / `due_reviews_count` in `/api/dashboard` are inflated
   by rows that no longer correspond to a real mistake.
4. **The `_collapse_duplicate_rows` helper is the wrong shape for the fix** and gives a false
   sense of coverage: it only collapses rows that *duplicate an existing natural key* (i.e.
   exactly the duplicates the schema now forbids), so it is dead code in the fixed schema and
   does nothing about a surplus of *distinct* rows.

## Fix direction
Give `save_blunders_to_db` the game ids in the batch and end with a scoped
`DELETE FROM blunders WHERE game_id = ? AND (ply, played_move) NOT IN (<batch>)` inside the
same transaction — collapsing the removed ids' `drill_reviews` rows too, so
`mastered_count` stays truthful. Prefer soft deletion (`is_active = 0`) if the spaced-repetition
history is meant to survive, and add the id-less/`NOT NULL` guard from SYNC-18 first, since
`NOT IN` against a `NULL` column is a well-known footgun.
