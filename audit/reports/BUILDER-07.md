# BUILDER-07 — After deleting a branch the UI keeps showing the deleted move and keeps its ⭐ / ✕ / note controls enabled, so the next click silently re-inserts it
- **Severity:** medium
- **Type:** ui-regression
- **File:** frontend/repertoire_builder.js
- **Line(s):** `removeBranch()` (~line 2845 in the current tree) and `renderMoveToolsPanel()`
- **Component:** Repertoire Builder — Move Tools panel / board timeline
- **Repro script:** `audit/scripts/atk1_b11_board_desync.py`
- **Detected by:** ATTACKER-1 (BUILDER)

## Expected

`removeBranch()` deletes `(parent_fen, move_uci)` and its descendants from the repertoire. The
Builder's job description for the ✕ button is "Remove as primary move" / "Remove branch". If
the move being displayed is the one that was just deleted, the UI must step the board back to
the parent position (or otherwise leave the deleted position), and must disable/refresh the
Move Tools controls so the deleted move cannot be operated on.

## Actual

Verified against the **real shipped frontend** via `tests/frontend_harness.js`: `1.e4` is
played through the real `board.playMove()` + `onBoardMove()`, the panel is rendered, then the
exact call the ✕ button makes (`removeBranch(history[0].fen, 'e2e4')`) is issued with a
stubbed `fetch` that reports the delete succeeded and returns an empty tree.

State before:

```json
{ "boardFen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
  "historySans": ["Start", "e4"], "currentStep": "e4",
  "deleteBtn":   { "visibility": "visible", "disabled": false },
  "importantBtn":{ "visibility": "visible", "disabled": false },
  "noteDisabled": false, "moveStillInTree": true }
```

State after the delete:

```json
{ "deleteRequestsSent": ["/api/repertoire/move"],
  "serverTreeNowEmpty": true,
  "boardFen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
  "boardStillShowsDeletedMove": true,
  "historySans": ["Start", "e4"], "historyLength": 2, "currentStepSan": "e4",
  "moveStillInTree": false,
  "deleteBtn":    { "visibility": "visible", "disabled": false },
  "importantBtn": { "visibility": "visible", "disabled": false,
                    "label": "☆ Mark Important" },
  "breadcrumbs":  ["Start", "1. e4"] }
```

The board still shows the deleted `1.e4`, the history and the breadcrumbs still list it, the
✕ Delete button is still visible and enabled (pointing at a move that no longer exists), and
⭐ Mark Important is still enabled. Because `POST /api/repertoire/move_meta` silently *inserts*
moves that are not in the tree (BUILDER-02), pressing ⭐ or typing a note at this moment
re-creates `1.e4` — the UI is actively inviting the user to undo their own deletion by
accident.

## Root cause

`removeBranch()` refreshes the tree and the candidate table but never reconciles the board
timeline, and never re-renders the panel that owns the ✕/⭐/note controls:

```javascript
// frontend/repertoire_builder.js -- removeBranch()
async removeBranch(parentFen, moveUci) {
    try {
        const res = await fetch('/api/repertoire/move', { method: 'DELETE', ... });
        const data = await res.json();
        if (data.status === 'ok') {
            await this.refreshTree();
            const curr = this.history[this.currentStepIndex];
            const isUserTurn = curr ? (...) : true;
            await this.loadCandidateMoves(parentFen, isUserTurn);
            // <-- no jumpToStep / history truncation / board reload
            // <-- no renderMoveToolsPanel() re-render
        }
    } catch (e) { console.error('Error deleting branch:', e); }
}
```

The panel's enablement is derived from `currentStepIndex !== 0` rather than from whether the
current move is in the tree:

```javascript
// renderMoveToolsPanel()
if (this.currentStepIndex === 0) { ...hide/disable...; return; }
if (commentEl) commentEl.disabled = false;               // enabled for ANY non-root step
if (btnImportant) { btnImportant.style.visibility = 'visible'; btnImportant.disabled = false; }
```

Note that `findMoveInTree()` *does* correctly return `null` for the deleted move
(`moveStillInTree: false` above) — the information needed to disable the controls is available;
it is simply not used for gating.

## Exploit / escalation

Adjacent weaker variants:

1. **Arrow-drawing has the same trap.** `renderMoveToolsPanel()` loads the stored arrows for
   the current move; after a delete it still paints the deleted move's arrows, and drawing a new
   arrow posts `move_meta` for the deleted move → re-insert (BUILDER-02 variant 2).
2. **Deleting a *parent* move from the candidate table** (`✕` on an opponent branch row) leaves
   the board on a position several plies deep in a subtree that no longer exists, so
   `stepForward()`'s fallback (`this.candidateCache.get(cacheKey)`) happily walks the user into
   unprepared territory while the panel still claims the move is "in the tree".
3. **`stepForward()` after a delete** prefers `inTreeMoves.find(m => m.is_user_move === 1)`
   from the 3-token bucket; with the tree refreshed that is now empty, so it silently falls
   through to the "play the top 2000+ candidate" fallback, i.e. the delete silently changes the
   keyboard-navigation behaviour with no feedback.
