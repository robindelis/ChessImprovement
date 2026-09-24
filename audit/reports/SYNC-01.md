# SYNC-01 — `blunders.best_pv` is never created by the schema, so the analyzer cannot persist a single blunder
- **Severity:** critical
- **Type:** data-loss
- **File:** backend/db.py
- **Line(s):** 118-150 (schema) vs backend/analyzer.py:236-268 (writer)
- **Component:** blunder persistence / `init_db()` migration
- **Repro script:** `audit/scripts/atk3_bestpv_repro.py`, `audit/scripts/atk3_reverify_matrix.py` (section `SYNC-18`)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
`GameAnalyzer.save_blunders_to_db()` writes every column of a detected blunder, and
`backend/db.py` guarantees a schema that contains all of them (either in the
`CREATE TABLE` or through an idempotent `ALTER TABLE ... ADD COLUMN` migration).

## Actual
`backend/db.py` creates `blunders` with **no `best_pv` column and no migration that adds
it**, while `backend/analyzer.py` unconditionally inserts into it. On any database created
by the current `init_db()` the very first blunder write raises `OperationalError`.

Observed (isolated temp DB, `audit/scripts/atk3_bestpv_repro.py`):

```
  ATK3-18a — columns created by backend.db.init_db() for `blunders`
   ['id', 'game_id', 'move_number', 'ply', 'color', 'fen_before', 'fen_after', 'played_move',
    'played_san', 'best_move', 'best_san', 'eval_before', 'eval_after', 'cpl_loss',
    'opening_name', 'eco', 'is_book_move', 'refutation_pv', 'explanation', 'strategic_idea',
    'category', 'opening_games', 'created_at']
  'best_pv' present: False
  analyzer.save_blunders_to_db() INSERT list (backend/analyzer.py:243-257):
    INSERT INTO blunders ( game_id, move_number, ply, color, fen_before, fen_after,
    played_move, played_san, best_move, best_san, eval_before, eval_after, cpl_loss,
    opening_name, eco, is_book_move, refutation_pv, best_pv, explanation, strategic_idea,
    category, opening_games, created_at )

  ATK3-18b — actually try to persist one blunder on this database
  save_blunders_to_db() RAISED OperationalError: table blunders has no column named best_pv
```

and end-to-end through the public API (`POST /api/repertoire/sync_games`):

```
  POST /api/repertoire/sync_games -> 500 Internal Server Error
  blunders rows   : 0
  File "backend/repertoire.py", line 1885, in analyze_user_games
    analyzer.save_blunders_to_db(blunders)
  File "backend/analyzer.py", line 242, in save_blunders_to_db
    cursor.execute("""
sqlite3.OperationalError: table blunders has no column named best_pv
```

`git show HEAD:backend/db.py` also lacks the column, so this is a pre-existing schema gap,
not a regression of this audit round: the only DBs that can store a blunder are ones whose
`blunders` table was created by an older schema revision.

## Root cause
`backend/db.py` (schema):
```sql
CREATE TABLE IF NOT EXISTS blunders (
    ...
    is_book_move INTEGER DEFAULT 0,
    refutation_pv TEXT,
    explanation TEXT,
    ...
)
```
`backend/analyzer.py:243-257` (writer):
```python
INSERT INTO blunders (
    game_id, move_number, ply, color, fen_before, fen_after,
    played_move, played_san, best_move, best_san,
    eval_before, eval_after, cpl_loss, opening_name, eco,
    is_book_move, refutation_pv, best_pv, explanation, strategic_idea,
    category, opening_games, created_at
) VALUES (...)
```
There is no `ALTER TABLE blunders ADD COLUMN best_pv` migration anywhere in `init_db()`,
even though the codebase's own migration policy (docstring in `init_db`, audit CODE-05-03)
is "additive, idempotent `ALTER TABLE ... ADD COLUMN` for every column the app writes".

## Exploit / escalation
1. **Blast radius:** the whole blunder feature is dead on any fresh install — no blunders,
   no `drill_reviews`, no `#1 BIGGEST BLUNDER`, no `get_game_gaps`. `GET /api/dashboard`
   reports `total_blunders: 0` forever, and the user is never told anything is wrong.
2. **The test suite is blind to it** because `tests/fixtures.py` seeds blenders with its own
   INSERT that omits `best_pv`, and no unit test calls `save_blunders_to_db()` against a
   migrated schema. A regression test that does so would have caught it.
3. **Same class, adjacent columns:** `backend/api.py:819-825` and `:1020-1039` *read*
   `blunder["best_pv"]` for the trainer feedback payload, so even a database that somehow
   gains the column later is read through a path that has never been executed on a
   migrated schema.
4. **`drill_reviews` is written in the same transaction** — so the spaced-repetition
   schedule (`INSERT OR IGNORE INTO drill_reviews`) is lost together with the blunder,
   and `due_reviews_count` on the dashboard is derived from it.
