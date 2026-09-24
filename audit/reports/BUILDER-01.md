# BUILDER-01 — Replacing a user move leaves dangling `study_moves`, and the next app start resurrects the whole discarded branch (two user moves per position)
- **Severity:** critical
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 511-557 (`add_repertoire_move`, the inline replacement-prune) vs. 671-712 (`_prune_orphaned_children`); the resurrection itself is backend/db.py:341-364
- **Component:** Repertoire Builder — repertoire tree / user-move replacement
- **Repro script:** `audit/scripts/atk1_b01_invariant.py` (also `audit/scripts/atk1_b10_fuzz_invariants.py`)
- **Detected by:** ATK-1 (BUILDER)

## Expected

`POST /api/repertoire/move` with a *different* `is_user_move=1` move at a position where the
user already has a prepared move must:

1. delete the old user move **and every row that pointed at it in `study_moves`**, and
2. prune the orphaned descendants, and
3. stay deleted — permanently, across restarts.

The tree must end up with exactly one `is_user_move = 1` row per `(repertoire, parent_fen)`,
which is the invariant the whole Trainer (`build_repertoire_tree_graph`, `get_next_trainer_drill`,
`evaluate_trainer_move`), the gap analysis (`rep_moves = {parent_fen: move_uci}`) and
AGENTS.md §9.3 all depend on.

## Actual

Step 1 of the repro replaces the prepared `3.e5` with `3.Nc3` in a study
("Anti French": `1.e4 e6 2.d4 d5 3.e5 Nf6 4.Bb5+`). The HTTP call succeeds and the
discarded branch disappears from `repertoire_moves` — but three orphan `study_moves` rows
survive:

```
POST /api/repertoire/move -> 200 {'status': 'success', 'san': 'Nc3', 'uci': 'b1c3', ...}
--- tree[white] after replace
  id=1   e4     uci=e2e4  user=1 study='AF'
  id=2   e6     uci=e7e6  user=0 study='AF'
  id=3   d4     uci=d2d4  user=1 study='AF'
  id=4   d5     uci=d7d5  user=0 study='AF'
  id=8   Nc3    uci=b1c3  user=1 study='AF'
--- studies after replace
  id=1   'AF'   white  study_moves=8            <-- 8 links for 5 moves
dangling study_moves (rows whose repertoire_moves row is gone):
  {'study_id': 1, 'repertoire': 'white', 'parent_fen': '...3PP3/8/PPP2PPP/RNBQKBNR w KQkq -', 'move_uci': 'e4e5'}
  {'study_id': 1, 'repertoire': 'white', 'parent_fen': '...3pP3/3P4/8/PPP2PPP/RNBQKBNR b KQkq -', 'move_uci': 'g8f6'}
  {'study_id': 1, 'repertoire': 'white', 'parent_fen': '...3pP3/3P4/8/PPP2PPP/RNBQKB1R w KQkq -', 'move_uci': 'f1b5'}
```

Then a single app restart (`start.py` → `init_db()` → `migrate_studies_and_cleanup_blunders`,
`backend/db.py:341` "Restore any moves from `study_moves` that are missing in
`repertoire_moves`") **re-inserts the entire discarded branch**:

```
STEP 2: simulate an APP RESTART (init_db -> migrate_studies_and_cleanup_blunders)
--- tree[white] after restart
  id=1   e4     uci=e2e4  user=1 study='AF'
  id=2   e6     uci=e7e6  user=0 study='AF'
  id=3   d4     uci=d2d4  user=1 study='AF'
  id=4   d5     uci=d7d5  user=0 study='AF'
  id=8   Nc3    uci=b1c3  user=1 study='AF'
  id=9   e5     uci=e4e5  user=1 study='AF'      <-- resurrected
  id=10  Nf6    uci=g8f6  user=0 study='AF'      <-- resurrected
  id=11  Bb5+   uci=f1b5  user=1 study='AF'      <-- resurrected

USER moves now at the 3rd-move parent position:
   {'move_uci': 'b1c3', 'move_san': 'Nc3', 'is_user_move': 1}
   {'move_uci': 'e4e5', 'move_san': 'e5', 'is_user_move': 1}

GET /api/repertoire/studies?repertoire=white ->
   {'id': 1, 'name': 'AF', ..., 'total_moves': 8, 'user_moves': 5}
```

`GET /api/repertoire/studies` now reports **5 user moves for a 3-user-move study**, and the
one-user-move-per-position invariant that the entire Trainer and the blunder-patching logic
depend on is permanently broken for that position.

`audit/scripts/atk1_b10_fuzz_invariants.py` shows how easily this is reached: a deterministic
200-action fuzz of realistic Builder actions (add/replace user move, add opponent branch,
delete branch, `move_meta`, "Add Opening", delete study) produces **114 dangling-`study_moves`
violations**, the first at step 16. The same fuzz found **zero** violations of the
dangling-`parent_fen` and study-isolation invariants, i.e. the delete paths are sound and this
one prune path is the outlier.

## Root cause

`add_repertoire_move()` does **not** use the shared pruning helper. It has an inlined copy of
the cascade that omits the `study_moves` cleanup:

```python
# backend/repertoire.py:511-557  (add_repertoire_move)
if is_user_move:
    cursor.execute("SELECT fen, move_uci FROM repertoire_moves "
                   "WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1", ...)
    existing_user_move = cursor.fetchone()
    if existing_user_move and existing_user_move["move_uci"] != move_uci:
        old_child_fen = existing_user_move["fen"]
        cursor.execute("DELETE FROM repertoire_moves "
                       "WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1", ...)
        # Recursively delete orphaned children of the replaced move
        ...
        cursor.execute("""
        DELETE FROM repertoire_moves
        WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?)
        """, (repertoire, norm_curr, curr_3, f"{curr_3} %"))   # <-- repertoire_moves only
        to_delete.extend(next_fens)
```

Compare the shared helper that `delete_repertoire_branch` and `delete_study` *do* use, which
deletes both tables at every level of the cascade:

```python
# backend/repertoire.py:704-711  (_prune_orphaned_children)
cursor.execute("""DELETE FROM repertoire_moves
    WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?)""", ...)
cursor.execute("""DELETE FROM study_moves
    WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?)""", ...)
```

The resurrection is then unconditional in the startup migration:

```python
# backend/db.py:341-364
cursor.execute("""
SELECT sm.study_id, rs.name as study_name, sm.repertoire, sm.parent_fen, sm.move_uci
FROM study_moves sm
JOIN repertoire_studies rs ON rs.id = sm.study_id
LEFT JOIN repertoire_moves rm ON rm.repertoire = sm.repertoire
   AND rm.parent_fen = sm.parent_fen AND rm.move_uci = sm.move_uci
WHERE rm.id IS NULL
""")
... INSERT OR IGNORE INTO repertoire_moves (...)   # re-inserts the pruned branch
```

The re-insert computes `is_user` from the side to move, so the resurrected `3.e5` is written
back as `is_user_move = 1` — producing the illegal two-user-moves state. Nothing in the
migration re-applies the "one user move per position" rule, and
`migrate_studies_and_cleanup_blunders`'s orphan purge is now opt-in
(`CHESS_PURGE_ORPHANS`, default off), so it does not clean this up either.


## Cross-reference / dedup note

`SCOUT-03` ("Overlapping import hijacks study membership and leaves dangling `study_moves`
links") reaches the same missing-`study_moves`-cleanup symptom through the Scout whole-line
import. The distinct content here is the **replacement-prune path specifically**
(`add_repertoire_move` lines 511-557) plus the **startup resurrection** that turns the dangling
link into a permanent two-user-moves-per-position state — the part that actually corrupts the
trainer, and the part no other report claims.

## Exploit / escalation

This is not an exotic path — it is the *primary* flow of the app:

1. **Blunder patching (AGENTS.md §4.D).** The Builder's "Address Next Blunder" flow adopts the
   engine's fix with `POST /api/repertoire/move`. Every time the user fixes a position where
   they deviated from their own study, the discarded branch is resurrected on the next launch.
2. **Same root cause via `POST /api/repertoire/move_meta`** — see BUILDER-02. Marking an
   unprepared move "important" triggers the same replacement prune, so a *note-taking* action
   destroys and then resurrects a prepared line.
3. **Same root cause via `POST /api/repertoire/import_game_opening`** ("📥 Add Opening") — see
   BUILDER-03.
4. **Blast radius beyond the tree.** Once two user moves exist at a position,
   `get_game_gaps` builds `rep_moves = {r["parent_fen"]: r["move_uci"] ...}` — a plain dict, so
   the `is_patched` verdict for a blunder is decided by an arbitrary one of the two, and
   `get_next_trainer_drill` / `evaluate_trainer_move` can only ever reach one of them.
5. **Amplifier:** because `get_studies` counts `study_moves` rows via a `LEFT JOIN`, the study
   dropdown's "N moves" figure inflates on every replacement and never comes back down, so the
   user has no signal that anything went wrong.
