# CODE3-14 — `board.setArrows()` / `addArrow()` accept unvalidated data; one malformed entry throws inside `render()` and takes the arrow layer (or the caller) down
- **Severity:** medium
- **Type:** crash
- **File:** frontend/board.js
- **Line(s):** 120-137 (`addArrow`, `clearArrows`, `setArrows`), 545-586 (`_renderArrows`), 588-603 (`_getSquareCenterPercent`), 188 (call from `render`)
- **Component:** InteractiveChessboard — arrow overlay
- **Repro script:** `audit/scripts/CODE-14_arrow_validation.js`
- **Detected by:** CODE-3

## Expected
`setArrows()` is a boundary between persisted/serialised data and rendering. It should ignore entries that are not
`{from: <file><rank>, to: <file><rank>}` rather than throwing.

## Actual
```js
// board.js:134
setArrows(arrows) {
    this.arrows = Array.isArray(arrows) ? [...arrows] : [];   // no element validation
    this._renderArrows();
}
```

```js
// board.js:570
const p1 = this._getSquareCenterPercent(arr.from);
const p2 = this._getSquareCenterPercent(arr.to);
...
// board.js:589
_getSquareCenterPercent(sq) {
    const file = sq.charCodeAt(0) - 97;      // TypeError when sq is undefined/null/not a string
    const rank = parseInt(sq[1], 10);
    ...
}
```

There is no bounds check either: a square name outside `a1..h8` produces `col = 8..N`, so
`x = (col + 0.5) * 12.5` yields e.g. `281%` — an arrow drawn far outside the board rather than an error.
`_renderArrows()` is called from `render()` (line 188), so a throw propagates out of `render()` and aborts it
part-way (the piece loop has already run, so the board looks OK but no arrows are finalised and the caller sees an
exception).

`repertoire_builder.js` is the only writer and it happens to wrap the one call site in a `try`:

```js
// repertoire_builder.js:1551
if (repMove && repMove.arrows) {
    try { const parsed = JSON.parse(repMove.arrows); this.board.setArrows(parsed); ... }
    catch (e) { this.board.clearArrows(); ... }
}
```

so a `null`/non-string entry in the `repertoire_moves.arrows` column is survivable there. The other call sites have no
such protection — `repertoire_trainer.js:90, 505, 776` (`setArrows(this.savedPlanArrows)`) and `scout.js:1065, 1075`
(`setArrows(arr)`) — and `addArrow(from, to)` (120-123) is a fully public, completely unvalidated mutator used by the
trainer (`repertoire_trainer.js:599, 857, 870`).

Note also that `loadFEN()` clears `this.arrows` (line 102) *without* notifying `onArrowsChange`, so the trainer's
`addArrow()` calls can be silently discarded by any subsequent `loadFEN` — an arrow the user was told is "on the
board" disappears with no event and no persistence.

## Root cause
Missing input validation/normalisation at the render boundary, plus a public mutator that assumes well-formed
square names. The class already has the knowledge to validate (the `this.squares` map) but does not use it.

## Exploit / escalation
1. Persist `arrows = "[null]"` for a repertoire move (possible via any writer that does not validate, or a hand-edited
   DB / a future importer), then navigate to it: `render()` throws from inside the animation callback chain
   (`animateMove` → `onComplete` → `loadFEN` → `render`), which is an *uncaught* throw inside a Web Animations
   callback — the candidate table for that step is then never populated, because `jumpToStep`'s animated branch
   returns immediately after starting the animation.
2. `setArrows([{from:'e2', to:'j9'}])` (a plausible typo/corrupt value) silently renders nothing useful instead of
   clamping or rejecting.
3. Any `arrows` JSON that is valid JSON but an object rather than an array is handled (`Array.isArray` → `[]`), so
   that variant is safe — the array-with-bad-entries case is the live one.
