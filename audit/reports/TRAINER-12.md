# TRAINER-12 — The board stays interactive while `/train/attempt` is in flight and the `h`/`←` shortcut re-arms it, so one user move is graded twice and advances SM-2 twice
- **Severity:** medium
- **Type:** race
- **File:** frontend/repertoire_trainer.js
- **Line(s):** 633-700 (`onUserAttempt`), 606-618 (`retryCurrentPosition`), 117-135 (key handler)
- **Component:** Repertoire trainer — frontend submission guard
- **Repro script:** `audit/scripts/atk2_10_frontend_race.mjs` (scenario 1)
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
A drill must accept exactly one attempt. Input must be disabled *synchronously* when the
attempt is dispatched, and every re-arm path must respect the in-flight state.

## Actual
The entry guard reads `isWaitingForUser`, but that flag is only cleared **after** the awaited
`fetch` resolves:

```js
async onUserAttempt(from, to, promo, moveRes) {
    if (!this.isWaitingForUser || !this.currentFen) return;   // still true here
    …
    const res = await fetch('/api/repertoire/train/attempt', {…});   // <-- window open
    …
    if (data.opponent_reply) { this.isWaitingForUser = false; … }    // cleared only now
```

and `retryCurrentPosition()` — bound to both the Retry button and the `h` / `ArrowLeft`
keyboard shortcut — re-arms input with no state check at all:

```js
retryCurrentPosition() {
    if (!this.currentFen || !this.currentDrill || !this.currentDrill.expected_uci) return;
    this.board.loadFEN(this.currentFen);
    …
    this.isWaitingForUser = true;          // unconditional
    this.updateHintButtonState(true);
}
```

Observed (real trainer class, throwaway backend, one user move `1.e4`):

```
1) DOUBLE SUBMIT: the board stays interactive while /train/attempt is in flight
   drill: e4  isWaitingForUser = true
   -> move played, POST in flight; isWaitingForUser is STILL true
      (onUserAttempt only clears it after the await)
   -> retryCurrentPosition() (bound to the h / ArrowLeft key) re-armed input: true
   POSTs issued for ONE user move: 2
   responses: [ { correct: true, tp: 1, tc: 1 }, { correct: true, tp: 1, tc: 1 } ]
   server progress for white: { m: 0, l: 1, u: 2 }
```

Two POSTs for one move, both answered `correct: true`. The frontend's own counters are
protected by `hasAnsweredCurrentPosition` (line 673), but the **server** counters are not —
and in this run the two concurrent writes even lost one another (`tp` stayed 1), which is
TRAINER-02.

## Root cause
Asynchronous submission with the "busy" flag set on the response path instead of on the
request path, plus an unguarded re-arm function reachable from a global key handler that does
not check `isWaitingForUser` and has no `preventDefault` guard for the pending-feedback state.

## Exploit / escalation
1. No exotic input: a user who plays a move and immediately presses `h` (or `←`) — both are
   advertised shortcuts — double-counts. Two fast consecutive drags do the same.
2. Because the window is open for the whole round-trip, a slow `/train/attempt` (the endpoint
   runs a weighted `random.choices` and three `study_moves` joins per call) widens it
   considerably; on a large repertoire the tree build in `/train/next` competing for the same
   SQLite writer makes it worse.
3. Combined with TRAINER-06 this is a pure interval-inflation primitive: repeat the
   `h`-then-replay sequence to walk `interval_days` to the 3650-day cap.
