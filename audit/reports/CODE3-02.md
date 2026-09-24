# CODE3-02 — Saved board arrows are wiped by `loadFEN` at the end of every animated step-forward, while the counter still says "N arrows"
- **Severity:** high
- **Type:** wrong-result
- **File:** frontend/repertoire_builder.js
- **Line(s):** 1861-1885 (animated branch of `jumpToStep`), 1550-1563 (`renderMoveToolsPanel`)
- **Component:** Repertoire Builder — board arrows / move tools panel
- **Repro script:** `audit/scripts/CODE3-02_repro.js`
- **Detected by:** CODE-3

## Expected
Pressing "Next ▶" (or `→`) advances one ply: the board animates the piece and the repertoire arrows saved for the
destination move are visible afterwards, with `#repArrowsCount` agreeing with the board.

## Actual
`jumpToStep()` takes a hand-rolled "animate instead of re-render" path that **inverts** the order of the two steps
`updatePosition()` performs:

```js
// repertoire_builder.js:1861
if (!isRapid && index === prevIndex + 1) {
    ...
    this.renderMoveToolsPanel(curr);            // <-- loads saved arrows onto the board
    ...
    this.board.animateMove(from, to, () => {
        this.board.loadFEN(curr.fen);           // <-- 180 ms later: WIPES this.arrows
    });
    return;
}
this.updatePosition();
```

`renderMoveToolsPanel()` restores the arrows from the DB:

```js
// repertoire_builder.js:1551
if (repMove && repMove.arrows) {
    const parsed = JSON.parse(repMove.arrows);
    this.board.setArrows(parsed);
    if (arrowsCountEl) arrowsCountEl.textContent = `${parsed.length} arrow${...}`;
}
```

but `InteractiveChessboard.loadFEN()` unconditionally drops them (`frontend/board.js:100-104`):

```js
this.selectedSquare = null;
this.legalMovesFromSelected = [];
this.arrows = [];      // <-- wipes the arrows just restored
this.lastMove = null;
this.render();
```

Result after one single "Next ▶": the SVG arrow overlay is empty while `#repArrowsCount` still reads "2 arrows".
Stepping *backwards* (the `updatePosition()` path, which does `loadFEN` → `renderMoveToolsPanel`) is correct, so the
behaviour is inconsistent between directions and depends on the 160 ms `isRapid` throttle at line 1853 — the same
keypress sometimes shows the arrows and sometimes does not.

## Root cause
The animated fast-path duplicates the body of `updatePosition()` (see CODE3-18) but omits the invariant
"model/FEN first, then arrows". `loadFEN` doubles as "load position" and "reset all per-move UI state", so calling it
as an animation callback destroys the per-move state the fast-path had just restored.

## Exploit / escalation
1. Reproduce on any prepared line: import a line with arrows, click a breadcrumb chip for a move that has arrows, then
   click "Next ▶" once (< 160 ms apart is fine) — the arrows disappear and the counter lies.
2. Arrow *drawing* has the mirrored bug: `onArrowsChange()` (`repertoire_builder.js:1622-1653`) only persists arrows
   `if (repMove)` — i.e. only for moves already in the repertoire. Draw an arrow in a freshly explored position, press
   Next and Back: the arrow is gone and no POST was ever made, so the user silently loses their annotation.
3. Because `loadFEN` does not call `onArrowsChange`, the disappearance is invisible to the persistence layer — the
   arrows are still in the DB, so the bug is purely a display/counter mismatch and is easy to misdiagnose as data loss.
