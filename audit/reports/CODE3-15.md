# CODE3-15 — Global SVG marker ids collide across the three live board instances, so arrows borrow another board's arrowhead colour
- **Severity:** low
- **Type:** wrong-result
- **File:** frontend/board.js
- **Line(s):** 545-586 (`_renderArrows`), 553-556 (`markerId = \`arrow-marker-${i}\``)
- **Component:** InteractiveChessboard — arrow overlay
- **Repro script:** `audit/scripts/CODE-15_marker_id_collision.js`
- **Detected by:** CODE-3

## Expected
Each board instance's arrow overlay resolves its own arrowhead markers, independently of the other boards.

## Actual
Marker ids are derived only from the arrow's index within that board, with no per-instance prefix:

```js
// board.js:553
this.arrows.forEach((arr, i) => {
    const markerId = `arrow-marker-${i}`;
    const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
    marker.setAttribute('id', markerId);
    ...
    line.setAttribute('marker-end', `url(#${markerId})`);
```

Three `InteractiveChessboard` instances exist simultaneously for the whole session — all created eagerly at load
time and kept in the DOM (only their tab's `section` is hidden):

```
$ grep -n "new InteractiveChessboard" frontend/*.js
repertoire_builder.js:218   this.board = new InteractiveChessboard(boardEl, {...})   // #repertoireBoard
repertoire_trainer.js:35    this.board = new InteractiveChessboard(boardEl, {...})   // #trainerBoard
scout.js:198                this.board = new InteractiveChessboard(boardEl, {...})   // #scoutBoard
```

`#repertoireBoard` appears first in `index.html` (158), then `#trainerBoard` (482), then `#scoutBoard` (692).
SVG `url(#id)` references are resolved document-wide, first match wins. So as soon as the Repertoire board and the
Trainer board both have arrows (the trainer draws plan arrows via `addArrow`, `repertoire_trainer.js:599, 857, 870`),
the Trainer's `arrow-marker-0` reference resolves to the Repertoire board's marker: a red "shift-drag" arrow drawn in
the Trainer can be rendered with the green arrowhead defined by the builder's first arrow (and vice versa for every
index). `_renderArrows()` is also called on every `render()`, i.e. many times per navigation, so the winner changes
with whichever board last re-rendered.

## Root cause
Element ids that must be unique within a document are generated from a per-instance counter with no instance
identity. The class already has identity available (it is constructed per board) but the SVG overlay lives outside any
namespacing scheme.

## Exploit / escalation
1. Draw a shift-drag (red) arrow on the Repertoire board at index 0, then trigger the trainer's "Show Hint" arrows
   (green at index 0) — the trainer's arrowhead renders red. Confusing rather than dangerous, but it makes the
   colour coding of the arrow palette meaningless.
2. `document.querySelector('#arrow-marker-0')` from any code (or an attacker-controlled extension/preload) resolves to
   the builder's marker, i.e. the ids are a (harmless) DOM-collision surface.
3. Same class of issue: `img.dragstart` listeners are re-attached to a freshly created `<img>` on every one of the
   32 piece slots on every `render()` (board.js:168) — harmless churn, but ~32 needless listener allocations per
   render, several times per second during fast navigation.
