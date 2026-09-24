# CODE-09 — `DELETE /api/repertoire/move` always returns `{"status":"ok"}`, so "Remove from Rep" always claims success
- **Severity:** high
- **Type:** wrong-result
- **File:** frontend/scout.js (1837-1905); backend/api.py:885-892
- **Component:** OpeningScout.removeCurrentLineFromRepertoire / removeTreeLineFromRepertoire
- **Repro script:** `audit/scripts/CODE4_delete_status_repro.py`
- **Detected by:** CODE-4

## Expected
A destructive repertoire delete must report whether anything was actually
removed, and the UI must not claim success otherwise. Combined with CODE-01
(board flip selects the wrong `repertoire`) this makes the delete a silent
no-op that reports success.

## Actual
The endpoint unconditionally answers success:

```python
# backend/api.py:885-892
@app.delete("/api/repertoire/move")
def delete_repertoire_move(payload: DeleteMovePayload):
    """Deletes a move and all children branches from the repertoire."""
    if not is_valid_fen(payload.parent_fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    rm = RepertoireManager.get_instance()
    rm.delete_repertoire_branch(payload.repertoire, payload.parent_fen, payload.move_uci)
    return {"status": "ok", "message": "Branch deleted successfully."}
```

`delete_repertoire_branch`'s row count is thrown away. Both clients then
unconditionally celebrate:

```js
// frontend/scout.js:1849-1868
const res = await fetch('/api/repertoire/move', { method: 'DELETE', ... });
const data = await res.json();
if (data.status === 'ok') {
    if (window.chessAudio) window.chessAudio.playSuccess();
    this._showQuickAddNotice(`✓ Removed from ${repName} Repertoire`, '#ef4444');
    if (window.repertoireBuilder) { window.repertoireBuilder.loadRepertoire(rep); }
    this.undoUserMove();
    this.scheduleOverlapCheck();
}
```

Note also the mismatched semantics: a **bright ascending success arpeggio**
(`playSuccess`, `audio.js:74-91`) is played for a *destructive* action, in the
same code path that pairs it with a red toast.

`removeTreeLineFromRepertoire` (1874-1905) is a byte-for-byte copy of the same
logic, including the unconditional `playSuccess()`.

## Root cause
The API has no "affected rows" signal, and the client equates
`status === 'ok'` with "the intended change happened". Combined with
CODE-01's `repertoire = orientation`, a single "Flip Board" click is enough to
make the delete target a repertoire that does not contain the move — and the UI
still says it was removed.

## Exploit / escalation
1. **Flip + delete = silent failure.** Flip the board, click "🗑️ Remove from
   Rep", confirm the `confirm()` dialog. The DELETE targets the other
   repertoire, removes nothing, the UI says "✓ Removed from Black Repertoire",
   plays the success chime, *and* calls `this.undoUserMove()` — so the user
   loses board position as well.
2. **Tree-card delete.** `🗑️ White` / `🗑️ Black` on a line card
   (`scout.js:1296`, `1301`) deletes only the **last** ply of the line
   (`lastMove = line.moves[line.moves.length - 1]`, line 1877), yet the
   confirm dialog says "Remove move X **and following lines**". If the line was
   never imported, nothing is removed but the toast still fires.
3. **No error surfacing at all.** The `catch` at line 1869-1871 only
   `console.error`s; a 400 (invalid FEN) or a network error produces no toast
   whatsoever, so the button appears inert.
4. Adjacent weaker variant: the *same* unconditional-`status` pattern makes
   `saveMoveNote` display "Saved ✓" (`scout.js:1115`) whenever the endpoint
   answers, even though `update_move_metadata` may have created a brand-new row
   in the wrong tree (CODE-01).
