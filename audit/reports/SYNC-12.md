# SYNC-12 — the one-click "📥 Add Opening" on a game card silently replaces and prunes the user's prepared repertoire line
- **Severity:** medium
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 1754-1820 (`import_game_opening`) → 483-560 (`add_repertoire_move` prune)
- **Component:** game → repertoire import
- **Repro script:** `audit/scripts/atk3_patched_import_repro.py` (section ATK3-13), `audit/scripts/atk3_reverify_matrix.py` (section `SYNC-12`)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
Importing an opening from a game that deviates from the prepared line either (a) is
confirmed first, or (b) only appends the *new* sub-variation while keeping the existing user
move, or (c) reports that it replaced N moves. Chessbook-style tools never silently discard
a prepared line.

## Actual
`import_game_opening` walks the game's moves and calls `add_repertoire_move(...)` for every
ply, including the user's own moves. `add_repertoire_move` enforces "one user move per
position" by **deleting** the existing user move *and recursively pruning every descendant*:

```python
# backend/repertoire.py:483-520
existing_user_move = cursor.fetchone()
if existing_user_move and existing_user_move["move_uci"] != move_uci:
    old_child_fen = existing_user_move["fen"]
    cursor.execute("DELETE FROM repertoire_moves WHERE repertoire=? AND parent_fen=? AND is_user_move=1", ...)
    to_delete = [old_child_fen]
    ...  # recursive orphan-child DELETE
```

Observed — the user has a prepared `1.e4 e5 2.Nf3 Nc6 3.Bb5` line (5 moves, 3 of them
their own), then plays a game starting `1.d4` and clicks the single "📥 Add Opening" button
on the game card (`frontend/repertoire_builder.js:1031` → `importGameOpening(g.id, g.user_color)`):

```
  SYNC-12 import_game_opening replaces + prunes the prepared line
  before: ['e2e4', 'e7e5', 'g1f3', 'b8c6', 'f1b5']
  after : ['d2d4', 'd7d5', 'b1c3', 'g8f6']
  [LIVE ] SYNC-12: one 'Add Opening' click replaced the whole prepared line
```

The prepared `1.e4` line and its whole sub-tree are gone; there is no confirmation dialog,
no toast, and the response
(`{"status":"success","imported_moves":4,"target_ply":4,...}`) reports success without
mentioning a single deletion.

## Root cause
The destructive `add_repertoire_move` semantics (replace + prune) are appropriate for an
explicit "set as my move" click, but `import_game_opening` reuses them for a bulk import of
a *game* whose early moves may just be a different, unprepared choice. The import path never
compares against the existing tree before writing.

## Exploit / escalation
1. **Repertoire cross-import:** the endpoint accepts any `repertoire` value, so importing a
   game the user played as White with `repertoire="black"` writes the *opponent's* moves as
   the user's own Black moves and prunes the real ones (verified in
   `atk3_patched_import_repro.py`, section ATK3-11g).
2. **Bounded by `max_ply`:** `max_ply` is validated (`Field(None, ge=1, le=100)`) so at most
   100 plies of the game are imported — but 100 plies is the entire game, i.e. up to 50 of
   the user's own moves can be overwritten in one call.
3. **Study linkage side-effect:** every imported move that carries a `study_name` creates the
   study and a `study_moves` row, so a bulk import can silently create dozens of studies.
4. **Adjacent:** `import_game_opening` parses `moves_san` with `board.parse_san(san)` and
   `break`s on the first failure, so a game whose PGN contains a `+`/`#` suffix (Lichess
   never sends those, a re-imported PGN might) silently truncates the import with no error.
