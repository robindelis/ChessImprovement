# BUILDER-03 — "📥 Add Opening" (`import_game_opening`) silently overwrites prepared lines, and it never checks which side the user played
- **Severity:** high
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 1754-1818 (`import_game_opening`), specifically 1798
- **Component:** Repertoire Builder — Recent Games card "Add Opening" button
- **Repro script:** `audit/scripts/atk1_b04_silent_writes.py` (Part B/C) and `audit/scripts/atk1_b09_tree_cost.py` (part a)
- **Detected by:** ATK-1 (BUILDER)

## Expected

`POST /api/repertoire/import_game_opening` replays a *past game* into the repertoire. It must
either

* only add plies that do not conflict with anything already prepared, or
* report a diff ("this will replace your prepared 2.Nf3 and delete 4 moves below it") and
  require confirmation.

It must also validate that the repertoire colour matches the side the user actually played in
that game, because the whole import is driven by "is this ply the user's move?".

## Actual — variant 1: a deviating game replaces the prepared line

Prepared: `1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Ba4` (study "My Line"). The user then plays a game
`e4 e5 Nc3 Nc6 Bb5 a6 ...` in which they deviated at move 2, and clicks **📥 Add Opening** on
that game card:

```
POST /api/repertoire/import_game_opening -> 200
  {'status': 'success', 'imported_moves': 6, 'target_ply': 6,
   'opening_name': 'Ruy Lopez', 'eco': 'C60', 'repertoire': 'white'}

--- tree[white] after import
  id=9   e4     uci=e2e4  user=1 study='My Line'
  id=10  e5     uci=e7e5  user=0 study='My Line'
  id=18  Nc3    uci=b1c3  user=1 study=''      <-- the game's move, silently substituted
  id=19  Nc6    uci=b8c6  user=0 study=''
  id=20  Bb5    uci=f1b5  user=1 study=''
  id=21  a6     uci=a7a6  user=0 study=''

Any warning that a prepared move was replaced?  response keys:
  ['eco', 'imported_moves', 'opening_name', 'repertoire', 'status', 'target_ply']

(destroyed: 2.Nf3 2...Nc6 3.Bb5 3...a6 4.Ba4 — five moves of the prepared study)
```

The response contains no field describing a replacement, and the UI
(`frontend/repertoire_builder.js`, the `btn-quick-add-opening` handler) only flips the button
to "✓ Added!" — no confirm dialog, no diff, no undo. After the next app restart the destroyed
branch is resurrected (BUILDER-01), so the study ends up with **both** `2.Nf3` and `2.Nc3` as
user moves.

## Actual — variant 2: importing a game played as Black fills the White repertoire with the opponent's moves

The stored game says the user was Black:

```
game 'asblack': white_player=somebody, black_player=RobiDeli, user_color='black'
moves_san: e4 e5 Nc3 Nc6 Bb5 a6      (Nc3/Bb5 are the OPPONENT's moves)

POST /api/repertoire/import_game_opening {repertoire: 'white'} -> 200
  {'status': 'success', 'imported_moves': 6, 'target_ply': 6,
   'opening_name': 'Ruy Lopez', 'eco': 'C60', 'repertoire': 'white'}

--- tree[white] after importing a BLACK game into the WHITE repertoire
  id=10  Nc3    uci=b1c3  user=1 study=''       <-- the OPPONENT's move, now a *user* move
  ...
user moves at the position after 1.e4 e5 (should still be the prepared 2.Nf3):
  [{'move_uci': 'b1c3', 'move_san': 'Nc3', 'is_user_move': 1}]
```

The opponent's `2.Nc3` replaced the user's prepared `2.Nf3` and pruned the rest of the study.

## Root cause

`import_game_opening` decides "is this ply the user's move?" from the *requested repertoire
colour* and the ply parity, and never looks at the game's `user_color` column:

```python
# backend/repertoire.py:1798
is_user_turn = (repertoire == "white" and ply % 2 == 0) or (repertoire == "black" and ply % 2 == 1)
res = self.add_repertoire_move(repertoire=repertoire, parent_fen=p_fen,
                               move_uci=uci, is_user_move=is_user_turn)
```

The query that loads the game does select `user_color` … and then never uses it:

```python
# backend/repertoire.py:1762-1763
cursor.execute("SELECT id, moves_san, opening_name, eco, user_color FROM games WHERE id = ?", (game_id,))
game = cursor.fetchone()
```

and the endpoint takes `repertoire` straight from the request body with no cross-check
(`backend/api.py`, `repertoire_import_game` → `ScoutImportPayload.repertoire:
Literal['white','black']`).

Because every ply is then pushed through `add_repertoire_move`, each of the user's plies that
disagrees with the repertoire triggers the destructive one-user-move replacement
(`backend/repertoire.py:511-557`), and the API reports success with a count only.

`target_ply` also makes the blast radius larger than the user expects:
`target_ply = min(max(book_ply, 6), len(san_tokens))` — for an opening the encyclopedia knows
out to 20 plies, twenty plies of the user's own (possibly improvised) play are imported as
"prepared".


## Cross-reference / dedup note

`SYNC-12` ("the one-click '📥 Add Opening' on a game card silently replaces and prunes the
user's prepared repertoire line") already reports **variant 1** of this report. The distinct
content here is **variant 2** (the endpoint never checks the stored `games.user_color`, so a
game the user played as Black installs the *opponent's* moves as the user's prepared White
moves) and the `target_ply = min(max(book_ply, 6), …)` blast radius.

## Exploit / escalation

Adjacent weaker variants:

1. **The UI already passes the colour from the card** (`g.user_color`), so the wrong-colour case
   needs one bad API call or one mis-labelled game row — but nothing prevents it, and the
   endpoint is the only line of defence.
2. **Repeat clicks are destructive and silent.** The button is guarded by
   `beginWrite(key)` only against concurrent clicks, not against repeated clicks on *different*
   games of the same opening; each one can replace the previous import.
3. **Interaction with BUILDER-01.** Every destructive import leaves dangling `study_moves`
   rows, so the startup migration resurrects the replaced line — the user sees the import
   "not stick" and clicks Add Opening again, which replaces the replacement, ad infinitum.
