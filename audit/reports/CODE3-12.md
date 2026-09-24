# CODE3-12 — No in-flight guard on the candidate-table write buttons: double-click sends duplicate POSTs and interleaves three follow-up requests
- **Severity:** medium
- **Type:** race
- **File:** frontend/repertoire_builder.js
- **Line(s):** 2258-2281 (per-row handlers), 2305-2333 (`saveMove`), 1566-1599 (`toggleCurrentMoveImportant`)
- **Component:** Repertoire Builder — 2000+ explorer table / move tools
- **Repro script:** `audit/scripts/CODE-12_double_submit.js`
- **Detected by:** CODE-3

## Expected
Clicking "+ Set as My Move", "+ Add Branch" or "☆ Mark Important" produces exactly one write and one UI refresh; a
second click while the first is in flight is either ignored or idempotent.

## Actual
The handlers are one-liners with no `disabled` latch, no in-flight flag and no request token:

```js
// repertoire_builder.js:2261
btnSet.addEventListener('click', (e) => { e.stopPropagation(); this.saveMove(fen, m, true); });
// repertoire_builder.js:2269
btnAdd.addEventListener('click', (e) => { e.stopPropagation(); this.saveMove(fen, m, false); });
// repertoire_builder.js:2277
btnDel.addEventListener('click', (e) => { e.stopPropagation(); this.removeBranch(fen, m.uci); });
```

and each success fans out into three more awaited requests:

```js
// repertoire_builder.js:2324
if (data.status === 'success') {
    if (window.chessAudio) window.chessAudio.playSuccess();
    await this.loadStudies();                          // GET /api/repertoire/studies
    await this.refreshTree();                          // GET /api/repertoire/tree  (server-side gap analysis!)
    await this.loadCandidateMoves(parentFen, isUserMove);
}
```

Consequences of a double click (or of clicking "+ Add Branch" on two rows 50 ms apart, which is easy since the rows
are only re-rendered after the *first* response arrives):

* two concurrent `POST /api/repertoire/move` for the same `parent_fen`; for `is_user_move: true` the backend
  replaces the row (documented invariant), for `is_user_move: false` the outcome depends entirely on server-side
  `INSERT OR REPLACE` semantics;
* two overlapping `refreshTree()` calls → `this.treeData` is assigned last-writer-wins at line 900
  (`this.treeData = await res.json();`) with no ordering guard, so `findMoveInTree()` can read a stale tree;
* `removeBranch()` (2335-2357) has the same shape and additionally calls `loadCandidateMoves(parentFen, ...)` for the
  *parent* position while the user may already have navigated — the FEN guard at 2112 saves the table, but the
  button's own row state is not reverted on failure, so a failed delete leaves a "✕" that appears to do nothing;
* `toggleCurrentMoveImportant()` (1566) sends `is_important: newImportant` computed from the *current* tree state, so
  two rapid clicks both compute the same `newImportant` (not a toggle-flip) — correct by luck, but the button label is
  only re-rendered from `this.treeData` after `await this.refreshTree()`, so the click appears to do nothing for the
  duration of the request, inviting a third click.

Nothing surfaces a failure: every one of these handlers ends in `catch (e) { console.error(...) }` with no
`window.showToast`, so a rejected write is invisible to the user.

## Root cause
No request lifecycle management on mutating UI actions: no `disabled` latch, no in-flight set, no
`AbortController`, and no shared sequencing for the `treeData` refresh that the whole UI reads from.

## Exploit / escalation
1. Because `refreshTree()` triggers the backend's `get_game_gaps()` analysis (`backend/repertoire.py:1287`), each
   redundant refresh is expensive server-side — a double-click doubles the gap-analysis work and can visibly stall
   other requests.
2. `importToRepertoire()` / `importAllLinesToRepertoire()` have the identical shape (no latch on "📥 Line → White"
   per card, `renderTreeLines()` listeners at 2919-2932) and additionally call the un-sequenced overlap checks
   (CODE3-06) after the write.
3. `deleteCurrentMoveAndLine()` (1601) `confirm()`s and then calls `this.stepBack()`; if two confirms resolve out of
   order the history can end up one step behind the board.
