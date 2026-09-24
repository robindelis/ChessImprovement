# CODE3-13 — Cancelled board animations drop their `onComplete`, so a rapid navigation leaves the board model and DOM on a stale position
- **Severity:** medium
- **Type:** race
- **File:** frontend/board.js
- **Line(s):** 362-435 (`animateMove`), 424-429 (`oncancel`), 76-84 (`stopAnimations`); consumer at frontend/repertoire_builder.js:1861-1885
- **Component:** InteractiveChessboard / Repertoire Builder navigation
- **Repro script:** `audit/scripts/CODE-13_animation_cancel.js`
- **Detected by:** CODE-3

## Expected
Every `animateMove(from, to, onComplete)` invocation eventually runs `onComplete`, or the caller is otherwise
guaranteed that the board is re-synchronised with its logical position.

## Actual
`onComplete` is invoked from exactly three places in `animateMove` — the early `return`s at 366/372, `onfinish` at
422, and the `catch` at 433. The `oncancel` handler deliberately does **not** call it:

```js
// board.js:417
anim.onfinish = () => { ...; if (onComplete) onComplete(); };
anim.oncancel = () => {
    pieceImg.style.zIndex = '';
    pieceImg.style.pointerEvents = '';
    const idx = this.activeAnimations.indexOf(anim);
    if (idx !== -1) this.activeAnimations.splice(idx, 1);
};                                  // <-- onComplete silently dropped
```

and `jumpToStep()` uses that callback as the *only* mechanism for re-syncing the board:

```js
// repertoire_builder.js:1856
if (this.board && typeof this.board.stopAnimations === 'function') { this.board.stopAnimations(); }
...
this.board.animateMove(from, to, () => { this.board.loadFEN(curr.fen); });
return;                                     // updatePosition() (which calls loadFEN) is skipped
```

So any `stopAnimations()` between the start of that animation and its 180 ms completion cancels the sync:

* `jumpToStep()` itself calls `stopAnimations()` at line 1857 on every subsequent navigation;
* `board.loadFEN()` (86-87) also cancels;
* `setOrientation()` (70-74) rebuilds `this.squares` from scratch, invalidating the element references captured by an
  in-flight animation — and it does *not* call `stopAnimations()` first, so the `activeAnimations` array can retain
  entries whose elements are no longer in the document.

Sequence producing a *durable* wrong board (not just a 180 ms glitch): navigate `→` (animation A starts, model still
at FEN(i-1)), then 160-180 ms later navigate `→` again (outside the `isRapid` 160 ms throttle at 1853, so the
animated path is taken again) → A is cancelled, its `loadFEN` never runs, and the model is still at FEN(i-1) while
`currentStepIndex` is i+1 → animation B then queries `squares[from]` on the stale DOM, animating the wrong piece, and
if `animateMove`'s `if (!fromEl || !toEl)` guard (365) trips, the callback is skipped there too and the board never
re-syncs at all.

Interleaving the user into that window is enough to matter: a click on a square during the desync window is resolved
against `this.chess` (board.js:219-222, 351-354), i.e. the **stale** model, and `onBoardMove` then computes
`newFen = this.board.getFEN()` (line 1976) and pushes a history step whose parent FEN is the displayed position —
producing a history/board divergence that survives until the next `loadFEN`.

## Root cause
Fire-and-forget animation with a callback-only completion contract, combined with a navigation fast-path
(`jumpToStep`'s animated branch) that uses that callback as its *state synchronisation* step rather than rendering
state up-front and using the animation purely as decoration.

## Exploit / escalation
1. Press "Next ▶" twice ~170 ms apart (or use a 5 Hz key-repeat source): the board shows a different position than the
   breadcrumbs/candidate table, and a click landing in that window creates a move on the wrong position.
2. Click "Flip" (`index.html:169`) during an animation: `setOrientation()` → `_initDOM()` → `this.squares` replaced,
   the in-flight animation's `onfinish` then calls `loadFEN` on a detached tree — the arrows/highlights are lost
   (`loadFEN` clears `this.arrows`) and `onComplete` can run twice if the animation had already completed.
3. `animateMove`'s `if (!pieceImg) { if (onComplete) onComplete(); return; }` early-return (370-374) means a
   navigation to a move whose *from* square is empty (possible on the stale DOM described above) re-syncs the board
   without any animation — correct by luck, and the `!fromEl || !toEl` branch at 365-368 is the one that is not.
