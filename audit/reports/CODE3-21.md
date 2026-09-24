# CODE3-21 — Flipping the board does not flip the eval bar's label: after "Flip" the label is drawn dark-on-dark / light-on-white and becomes unreadable
- **Severity:** medium
- **Type:** wrong-result
- **File:** frontend/style.css
- **Line(s):** 300-318 (`.eval-bar-wrapper`, `.flipped`), 327-355 (`.eval-bar-label`); JS frontend/repertoire_builder.js:283-292, 869-873, 1274-1278
- **Component:** Repertoire Builder — eval bar orientation
- **Repro script:** `audit/scripts/CODE-21_eval_flip.js`
- **Detected by:** CODE-3

## Expected
The eval bar is a visual extension of the board: when the board is flipped to Black's perspective, the leading
side's fill and its score label both move, exactly as `setOrientation()` does for the squares.

## Actual
The flip **is** applied to the fill container:

```css
/* style.css:300 */
.eval-bar-wrapper { display: flex; flex-direction: column-reverse; /* Fills from bottom for White */ ... }
/* style.css:316 */
.eval-bar-wrapper.flipped { flex-direction: column; /* Fills from top for Black perspective */ }
```

```js
// repertoire_builder.js:283
btnFlip.addEventListener('click', () => {
    const newOr = this.board.options.orientation === 'white' ? 'black' : 'white';
    this.board.setOrientation(newOr);
    const evalWrapper = document.getElementById('repEvalBarWrapper');
    if (evalWrapper) { evalWrapper.classList.toggle('flipped', newOr === 'black'); }
});
```

but the **label** is positioned with un-flipped, colour-coupled absolute offsets:

```css
/* style.css:341 */
.eval-bar-label.white-lead { bottom: 5px; color: #0f172a; }  /* dark text, meant to sit ON the white fill */
/* style.css:346 */
.eval-bar-label.black-lead { top: 5px;    color: #f8fafc; }  /* light text, meant to sit ON the dark slate */
```

There is **no** `.eval-bar-wrapper.flipped .eval-bar-label…` rule anywhere (`grep -n "flipped" style.css` returns
exactly one rule, line 316). Consequences after pressing "Flip" (`index.html:169`) or after any automatic
orientation set (`loadRepertoire` 869-873, `reviewGame` 1274-1278):

* White ahead → `white-lead` → label pinned to the **bottom** while the white fill now occupies the **top** →
  `#0f172a` (near-black) text on the `#1e293b` dark-slate background → effectively invisible;
* Black ahead → `black-lead` → label pinned to the **top**, over the white fill → `#f8fafc` (near-white) text on
  white → effectively invisible;
* `.eval-bar-label.equal` is `top: 50%` with `transform: translateY(-50%)`, which is orientation-independent, so the
  only readable case after a flip is the drawn position.

So the label is legible exactly when the board is unflipped and unreadable in both decisive cases when it is
flipped. The three `.flipped` toggles in JS show the intent was there; the CSS half of the feature was never written.

## Root cause
The label's placement is coupled to the fill direction through a *colour* class on the label itself
(`white-lead`/`black-lead`) rather than through the wrapper's orientation, so flipping the wrapper cannot move it.

## Exploit / escalation
1. The same label/fill coupling exists for the Scout view: `scout.js` writes `#scoutEvalBarLabel` with the same
   class names and `#scoutEvalBarWrapper` with the same `flipped` toggle, so the identical defect is present twice.
2. `board.setOrientation()` does **not** re-render the eval bar, so flipping immediately after a score arrives shows
   the new orientation with the old label placement until the next SSE frame (up to depth 16) — the illegible state
   persists noticeably longer than one frame.
3. Related orientation inconsistency: `switchColor`/`reviewGame` compute the flip from the *trained* colour while
   `btnFlip` computes it from `this.board.options.orientation`; after `reviewGame` sets the board to
   `game.user_color`, the "White"/"Black" toggle buttons and the eval-bar orientation can disagree about which side
   is at the bottom.
