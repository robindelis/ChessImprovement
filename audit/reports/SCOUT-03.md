# SCOUT-03 — Overlapping import hijacks study membership and leaves dangling `study_moves` links
- **Severity:** high
- **Type:** data-loss
- **File:** backend/repertoire.py (`add_repertoire_move`)
- **Line(s):** 528-563
- **Component:** Repertoire study grouping (reached from Scout's whole-line import)
- **Repro script:** `audit/scripts/atk4_03_study_hijack_repro.py`
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
Re-importing a line that is already in a study must not silently move those moves into a
different study, and `study_moves` must not keep links to moves that no longer exist.

## Actual
`add_repertoire_move` always passes the caller-supplied `study_name` into an
`INSERT OR REPLACE`, overwriting whatever the row had:

```python
final_study = (study_name.strip() if study_name and study_name.strip() else (... existing_meta["study_name"] ...))
...
INSERT OR REPLACE INTO repertoire_moves (..., study_name, created_at) VALUES (...)
```

`study_moves` is only ever `INSERT OR IGNORE`d — never reconciled, never deleted.

Observed (`/tmp` probe re-run, reproduced verbatim in the repro script):

```
repertoire_moves.study_name :
  e2e4 e4  -> 'Video Import'      (was 'Anti French')
  e7e6 e6  -> 'Video Import'      (was 'Anti French')
  d2d4 d4  -> 'Video Import'      (was 'Anti French')
  d7d5 d5  -> 'Video Import'      (was 'Anti French')
  b1c3 Nc3 -> 'Video Import'

study_moves links :
  'Anti French' -> e2e4, e7e6, d2d4, d7d5, e4e5     <-- e4e5 no longer exists in repertoire_moves
  'Video Import'-> e2e4, e7e6, d2d4, d7d5, b1c3

repertoire_studies : [{'name': 'Anti French'}, {'name': 'Video Import'}]

GET /api/repertoire/studies :
  Anti French    : total_moves 5, user_moves 2   <-- claims 5 moves, owns 0
  Video Import   : total_moves 5, user_moves 3
```

So after one overlapping import the "Anti French" study is a ghost: it still appears in the
Scout study dropdown (`loadStudies()` → `/api/repertoire/studies`) and still counts 5 moves,
but 4 of them are labelled with another study and the 5th (`e4e5`) was deleted outright.

## Root cause
Unconditional `INSERT OR REPLACE` of the whole row (including `study_name`) plus a
write-only `study_moves` link table with no reconciliation step.

## Exploit / escalation
1. Any Scout import that overlaps an existing study silently re-labels the user's curated
   study; the trainer's per-opening study filter then trains the wrong set.
2. Repeatedly importing the same line with a *new* `study_name` each time creates one
   `repertoire_studies` row per click (`INSERT OR IGNORE ... (name, repertoire)`) — unbounded
   study-list growth and a dropdown full of duplicates.
3. `migrate_studies_and_cleanup_blunders()` "restores" moves from `study_moves`, so a dangling
   `study_moves` row can resurrect a deleted move on the next app start.
