# BUILDER-02 — `POST /api/repertoire/move_meta` is a hidden write endpoint: it INSERTS a move that is not in the tree, replacing and pruning the prepared line
- **Severity:** high
- **Type:** data-loss
- **File:** backend/repertoire.py
- **Line(s):** 922-966 (`update_move_metadata`, the "not found → add it" fall-through)
- **Component:** Repertoire Builder — Move Tools panel (`☆ Mark Important` / notes box)
- **Repro script:** `audit/scripts/atk1_b04_silent_writes.py` (Part A)
- **Detected by:** ATK-1 (BUILDER)

## Expected

`POST /api/repertoire/move_meta` is documented in `api.py` as *"Updates importance flag,
personal notes/comments, and board arrows for a repertoire move."* A metadata update on a
move that is not in the repertoire must be a no-op or a `404`, never a mutation of the
repertoire tree. In particular, attaching a note or a ⭐ to a move the user merely played on the
board must not be able to delete their prepared move and everything below it.

## Actual

Repro setup: the user's prepared White line is
`1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Ba4` in study "My Line". At the position after `1.e4 e5`
(White to move) the only stored move is the prepared `2.Nf3`.

The user plays `2.f4` **on the board** (a move that is not in the repertoire at all) and then
presses `☆ Mark Important` in the Move Tools panel. The panel is enabled for *every* non-root
position, and `toggleCurrentMoveImportant()` / `saveMoveComment()` both POST `move_meta` for
whatever move is currently displayed. Result:

```
position after 1.e4 e5: rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2
moves stored at that position: [{'move_uci': 'g1f3', 'move_san': 'Nf3', 'is_user_move': 1}]

-> user plays 2.f4 on the board (NOT in the repertoire), then clicks 'Mark Important'.
   -> 200 {'status': 'success', 'is_important': True, 'comment': 'looks better to me'}

--- tree[white] after move_meta on an un-prepared move
  id=1   e4     uci=e2e4  user=1 study='My Line'
  id=2   e5     uci=e7e5  user=0 study='My Line'
  id=8   f4     uci=f2f4  user=1 study=''

   the prepared 2.Nf3 and its whole 3.Bb5 line are GONE.
   user_moves at that position: [{'move_uci': 'f2f4', 'move_san': 'f4', 'is_user_move': 1}]
```

Four moves of a prepared study (`2.Nf3 2...Nc6 3.Bb5 3...a6 4.Ba4`) were destroyed by a
"mark it important" click, the new move was silently filed outside every study
(`study_name: ''`), and — per BUILDER-01 — the destroyed branch comes back on the next app
start, leaving two user moves at that position.

## Root cause

When the lookup misses, the function does not report "not found"; it *creates* the move:

```python
# backend/repertoire.py:941-966
cursor.execute("""
SELECT * FROM repertoire_moves
WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND move_uci = ?
""", (repertoire, norm_parent, norm_3, f"{norm_3} %", move_uci))
row = cursor.fetchone()
if not row:
    conn.close()
    # If move doesn't exist yet in repertoire, add it
    try:
        board = chess.Board(parent_fen)
        if repertoire == "white":
            is_user = (board.turn == chess.WHITE)
        else:
            is_user = (board.turn == chess.BLACK)
    except Exception:
        is_user = True
    return self.add_repertoire_move(          # <-- INSERT, with full replacement semantics
        repertoire=repertoire, parent_fen=parent_fen, move_uci=move_uci,
        is_user_move=is_user, is_important=is_important,
        comment=comment, arrows=arrows, study_name=study_name)
```

`add_repertoire_move` then applies the one-user-move rule, which *is* destructive:

```python
# backend/repertoire.py:511-523
if is_user_move:
    ... SELECT fen, move_uci ... WHERE parent_fen = ? AND is_user_move = 1
    if existing_user_move and existing_user_move["move_uci"] != move_uci:
        cursor.execute("DELETE FROM repertoire_moves "
                       "WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1", ...)
```

The endpoint also cannot tell the caller that a write happened: it returns the ordinary
`{"status": "success", ...}` metadata shape with no `created: true` flag, and
`api.py` only raises when `status == "error"`, which this path never produces.

The frontend makes the trap reachable because the Move Tools controls are gated on
`currentStepIndex !== 0`, not on the move being in the tree
(`frontend/repertoire_builder.js`, `renderMoveToolsPanel()`: the ⭐ button is set to
`visibility: visible; disabled = false` for every non-root step, and the note textarea to
`disabled = false`).

## Exploit / escalation

Adjacent weaker variants of the same root cause:

1. **The ✕ delete button invites the undo.** `removeBranch()` does not update
   `history`/`currentStepIndex`/the board, so after deleting a move the UI still shows it and
   still enables ⭐ and the note box; pressing either re-inserts it (see BUILDER-11).
2. **`arrows` is the cheapest trigger.** The arrows widget calls `saveMoveArrows()` →
   `move_meta` for the current move; drawing an arrow on an unprepared position destroys the
   prepared line.
3. **Any client, no UI needed.** `POST /api/repertoire/move_meta` with a legal but unprepared
   `(parent_fen, move_uci)` pair is a fully general "replace the user's prepared move and
   delete its subtree" primitive, reachable without touching the Builder at all.
