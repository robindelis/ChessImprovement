# CODE3-16 — board.js leaks: dead `_onSquareClick`, per-instance `window` pointer listeners with no `destroy()`, and an uncleared right-click anchor
- **Severity:** low
- **Type:** leak
- **File:** frontend/board.js
- **Line(s):** 21-24, 63-65, 191-199, 278-301, 339-360
- **Component:** InteractiveChessboard
- **Repro script:** `audit/scripts/CODE-16_board_listeners.js`
- **Detected by:** CODE-3

## Expected
A component that attaches global listeners exposes a teardown, and unused private methods are removed.

## Actual
**1. `_onSquareClick` is dead code (22 lines).** `_initDOM()` registers only `contextmenu` and `pointerdown`
(line 63-64); the click path is handled inside `_onPointerDown` (lines 208-216). No caller exists:

```
$ grep -n "_onSquareClick" frontend/*.js
board.js:339:    _onSquareClick(sq) {
```

**2. Two `window` listeners per instance, never removed.** They are bound in the constructor and there is no
`destroy()`/`dispose()` method on the class:

```js
// board.js:21
this._boundPointerMove = (e) => this._onPointerMove(e);
this._boundPointerUp = (e) => this._onPointerUp(e);
window.addEventListener('pointermove', this._boundPointerMove);
window.addEventListener('pointerup', this._boundPointerUp);
```

So every mousemove on the page runs `_onPointerMove` for **all three** live boards. It early-returns cheaply when
`_dragState` is null, so the cost is bounded — but the listeners permanently retain the board instances (including
their `chess` models, square maps and SVG trees) from `window`, and `_initDOM()` — called on every `setOrientation()`,
i.e. on every colour switch, every `loadRepertoire()` and every Electron tab switch to the builder (`app.js:63`) —
throws away and rebuilds the whole 64-square DOM without detaching anything or calling `stopAnimations()` first (see
CODE3-13).

**3. `_rightClickStartSquare` is not cleared on the "no arrow drawn" paths.** The pointerup handler returns early
without resetting it when the pointer is released outside any `.square`:

```js
// board.js:280
if (e.button === 2 && this._rightClickStartSquare) {
    const targetEl = document.elementFromPoint(e.clientX, e.clientY)?.closest('.square');
    const targetSq = targetEl ? targetEl.dataset.square : null;
    if (targetSq && targetSq !== this._rightClickStartSquare) { ...draw/toggle arrow... }
    this._rightClickStartSquare = null;
    return;
}
```

When `targetSq === null` the reset is skipped **and** the early `return` bypasses the drag-ghost cleanup
(`this._dragFloatingEl.remove()`, lines 306-309) and the `_dragState` reset, so a `.floating-drag-piece` can survive in
`document.body` at `z-index: 9999` with the source piece left at `opacity: 0.2`.

**4. Also unreachable-by-drift:** `_rightClickStartSquare` is initialised *after* the listeners are attached
(line 65), so the field is `undefined` for any event arriving during `_initDOM()`.

## Root cause
No lifecycle method for a component that binds global listeners, plus a state machine (`_dragState`,
`_rightClickStartSquare`) with more than two exit paths and no single cleanup routine.

## Exploit / escalation
1. Rapidly alternate "White Repertoire"/"Black Repertoire": each switch rebuilds the board DOM through `_initDOM()`
   while `activeAnimations` may still reference detached elements and the two `window` listeners keep the instance
   (and therefore the old square map) alive.
2. Right-drag a piece and release the mouse outside the window with the right button: the floating ghost and the
   0.2-opacity source piece survive until the next `pointerup` on `window`, i.e. the board can render permanently
   with a semi-transparent piece and a stray ghost image on top of the page.
