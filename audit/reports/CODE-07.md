# CODE-07 — Trainer: uncleared `setTimeout`s mutate the board and drill of the *next* position
- **Severity:** high
- **Type:** race
- **File:** frontend/repertoire_trainer.js
- **Line(s):** 701-804 (opponent reply, 550 ms), 863-872 (mistake reset, 800 ms)
- **Component:** RepertoireTrainer.onUserAttempt
- **Repro script:** `audit/scripts/CODE4_trainer_timer_repro.js`
- **Detected by:** CODE-4

## Expected
Every deferred continuation of a drill must be cancelled (or tagged with the
drill id it belongs to) whenever the drill is replaced by
`startNewDrill()` / `retryCurrentPosition()` / `switchColor()`.

## Actual
`autoNextTimeout` is the *only* tracked timer, and it is cleared in three
places. The two timers that actually touch the board and mutate
`this.currentDrill` are anonymous and untracked:

```js
// frontend/repertoire_trainer.js:697-712
if (data.opponent_reply) {
    this.isWaitingForUser = false;
    this.updateHintButtonState(false);

    setTimeout(() => {                       // <-- NOT stored, NOT clearable
        const opp = data.opponent_reply;
        const oppFrom = opp.uci.slice(0, 2);
        const oppTo = opp.uci.slice(2, 4);
        const oppPromo = opp.uci.length === 5 ? opp.uci[4] : undefined;

        this.board.playMove(oppFrom, oppTo, oppPromo);
        this.currentFen = this.board.getFEN();   // <-- overwrites the NEW drill

        if (this.currentDrill) {
            this.currentDrill.last_move = { from: oppFrom, to: oppTo, san: opp.san };
        }
        ...
    }, 550);
}
```

```js
// frontend/repertoire_trainer.js:863-872
setTimeout(() => {                           // <-- NOT stored, NOT clearable
    this.board.loadFEN(this.currentFen);
    if (this.currentDrill && this.currentDrill.last_move) {
        this.board.setLastMove(this.currentDrill.last_move.from, this.currentDrill.last_move.to);
    }
    if (data.expected_uci) {
        this.board.clearArrows();
        this.board.addArrow(data.expected_uci.slice(0, 2), data.expected_uci.slice(2, 4), '#22c55e');
    }
}, 800);
```

`startNewDrill()` — the single entry point used by "New Position / Line",
`switchColor`, the Focus `<select>` `change` handler, the Mode `<select>`
`change` handler, `setOpeningFocus`, `clearOpeningFocus`,
`showTab('rep-trainer')` and the session-complete modal — clears only
`this.autoNextTimeout`:

```js
// frontend/repertoire_trainer.js:386-387
clearTimeout(this.autoNextTimeout);
this.hasAnsweredCurrentPosition = false;
```

It also resets `isWaitingForUser` / `currentDrill` (lines 376-377) but cannot
recall the in-flight callbacks.

## Root cause
Anonymous `setTimeout` handles used as "the next step of the current drill",
with no ownership token. The callbacks read mutable instance state
(`this.board`, `this.currentFen`, `this.currentDrill`) that has since been
replaced by a newer drill.

## Exploit / escalation
1. **Board plays a phantom move.** Answer correctly, then click
   "New Position / Line" (or flip the colour) within 550 ms. `startNewDrill`
   loads the new drill's FEN; then the stale callback fires and executes
   `this.board.playMove(oppFrom, oppTo, oppPromo)` — playing the *previous*
   line's opponent move on the *new* position. `chess.js` will usually reject
   it (`this.chess.move()` returns `null` and `playMove` bails, leaving the
   board unchanged) — **but for a capture/quiet move that happens to be legal in
   the new position it silently succeeds**, and `this.currentFen =
   this.board.getFEN()` then records a FEN that is not any drill's position.
2. **Drill metadata corruption.** Even when the move is rejected, the callback
   still runs `this.currentDrill.last_move = {from, to, san}` on the *new*
   drill and, if `data.next_expected_uci` is set, overwrites
   `this.currentDrill.expected_uci` / `expected_san` / `path_formatted`
   (lines 715-733). `showHint()` then draws an arrow for a move that does not
   belong to the position on screen, and `retryCurrentPosition()` resets the
   board to the wrong FEN.
3. **Stray arrow after a mistake.** Blunder a move, then hit "New Position /
   Line" inside 800 ms: the stale callback calls
   `this.board.loadFEN(this.currentFen)` — but `this.currentFen` has already
   been overwritten by the new drill's `data.parent_fen`
   (`repertoire_trainer.js:450`) — and then paints the *old* drill's
   `expected_uci` arrow on it, teaching the user a move for a position it does
   not belong to.
4. Adjacent weaker variant: the 550 ms window is also open while the session
   modal is up (`onSessionComplete` fires before the opponent reply plays), so
   "Start Next 20-Move Session" can be clicked mid-window and inherits the
   same corruption.
