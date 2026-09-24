# TRAINER-03 — Re-adding a repertoire move that already exists silently wipes its spaced-repetition progress
- **Severity:** high
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 477-525 (delete-then-insert), 540-552 (`INSERT OR REPLACE`)
- **Component:** Repertoire trainer — progress persistence
- **Repro script:** `audit/scripts/atk2_09_progress_dataloss.py`
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
Re-saving / re-importing a move that is already in the repertoire must be idempotent for
training statistics: `times_practiced`, `times_correct`, `last_practiced`, `next_review` and
`interval_days` are *training* state owned by the trainer and must be preserved.
`INSERT OR REPLACE` in particular must never be used on a row that carries counters, because
`REPLACE` deletes the old row and re-inserts with column defaults.

## Actual
For a user move, `add_repertoire_move` first **deletes** the existing row and then re-inserts
it — and the `INSERT OR REPLACE` statement does not list any of the scheduling columns:

```python
elif existing_user_move:                       # same move, same parent -> still deletes!
    cursor.execute("""DELETE FROM repertoire_moves
                      WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1""", …)
…
cursor.execute("""INSERT OR REPLACE INTO repertoire_moves (
    repertoire, parent_fen, fen, move_uci, move_san, is_user_move, popularity_pct,
    win_rate, eval_score, opening_name, eco, is_important, comment, arrows, study_name, created_at
) VALUES (?, …)""", …)                        # times_practiced/times_correct/… DEFAULT 0
```

Observed:

```
after 3 correct: tp=3 tc=3 interval=6.6 next_review=570240
progress: {'mastered_count': 1, 'learning_count': 0, 'unlearned_count': 2}

POST /api/repertoire/move with the identical (repertoire, parent_fen, move_uci):
   -> 200 success
after re-add : tp=0 tc=0 interval=1.0 next_review=0
progress: {'mastered_count': 0, 'learning_count': 0, 'unlearned_count': 3}
drill for 1.e4 is now: frontier_new
```

A card that was **mastered** with a 6.6-day interval is back to `unlearned` and is
immediately re-served by `mode=frontier_only`. `POST /api/repertoire/move_meta` does *not*
have this problem (it uses a plain `UPDATE`), which proves the loss is specific to the
add path.

## Root cause
`DELETE`-then-`INSERT OR REPLACE` on `repertoire_moves`, with the SM-2 columns absent from
the column list, so they fall back to the schema defaults (`times_practiced INTEGER DEFAULT 0`,
`next_review INTEGER DEFAULT 0`, `interval_days REAL DEFAULT 1.0`).

## Exploit / escalation
1. Every write path that funnels into `add_repertoire_move` can trigger it, all reachable
   from the UI: `POST /api/repertoire/move` (builder "save move"), the Scout line/tree import
   (`📥 Line → White`, `📥 All → Black`), `POST /api/repertoire/import_game_opening`, and
   `POST /api/repertoire/seed?force=true`. Re-importing a study the user already trains is a
   completely normal action.
2. **Silent partial loss**: the same `DELETE` also drops `is_important` / `comment` / `arrows`
   when they are not passed in the payload — `add_repertoire_move` tries to restore them from
   `existing_meta` (line 528-537), but `existing_meta` is queried *after* the delete of the
   user-move row, so for the "replace" path the previous metadata of the *replaced* move is
   what survives while the counters are gone.
3. Any script that re-seeds the repertoire (`seed_default_repertoires(force=True)`) resets
   every card in the tree to `unlearned`.
