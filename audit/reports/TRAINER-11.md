# TRAINER-11 — No request sequencing in `startNewDrill()`: a slow `/train/next` response overwrites a newer drill, so the user is served the previous colour's card and every answer is rejected
- **Severity:** medium
- **Type:** race
- **File:** frontend/repertoire_trainer.js
- **Line(s):** 370-513 (`startNewDrill`), 141-155 (`switchColor`), 340-358 (`setOpeningFocus`), 79-83 (`clearOpeningFocus`)
- **Component:** Repertoire trainer — frontend state machine
- **Repro script:** `audit/scripts/atk2_10_frontend_race.mjs` (scenario 2)
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
A newer navigation must always win. `startNewDrill` is `async` and awaits two loads plus the
`/train/next` fetch before it assigns `this.currentDrill` / `this.currentFen`; it must carry a
generation token (or `AbortController`) so a late response from a superseded call is dropped.

## Actual
No token, no abort, no ordering check. `switchColor` (`await this.startNewDrill()`), the study
`<select>` change handler, `trainModeSelect`, `btnTrainNewDrill` and `clearOpeningFocus` all
call it, and any of them can be in flight simultaneously.

Observed with the real `repertoire_trainer.js` driven against the throwaway backend
(`CHESS_DB_PATH=/tmp/atk2_ui.db`, port 39311), first `/train/next` artificially delayed 700 ms
and a colour switch 120 ms later:

```
2) STALE RESPONSE WINS: switchColor while the first /train/next is still in flight
   [fetch] /api/repertoire/train/next?color=white&mode=adaptive resolved +700ms AFTER later requests
   activeColor          : black
   currentDrill.study   : RaceWhite
   currentDrill.expected: e4 e2e4
   board FEN            : rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
   isWaitingForUser     : true
   --> the stale WHITE drill is now served while the user is training BLACK
   user plays 1.e4 -> This position is not yet in your repertoire tree. | is_in_tree = false
   frontend streak = 0  practicedCount = 1  sessionMistakes = 1
```

`activeColor` is `black` (so the POST is sent with `repertoire: "black"`) while the drill,
the breadcrumb, the study badge and the board are all the white `1.e4` card. The user's move is
rejected, the streak is zeroed, `practicedCount` is incremented and the mistake is added to
`sessionMistakes` — for a move that was correct.

## Root cause
Fire-and-forget async UI state with a single mutable `currentDrill`/`currentFen` pair and no
invalidation of superseded requests.

## Exploit / escalation
1. Same race with the study dropdown, the mode dropdown and the "New Drill" button — the
   `trainModeSelect` handler calls `restartSession(false)` *and* `startNewDrill()`, and
   `clearOpeningFocus()` calls `startNewDrill()` again inside the 404 handler of
   `startNewDrill()` (line 425), so a 404 already produces two overlapping drills.
2. The stale drill's `expected_uci` is still in `this.currentDrill`, so `showHint()` will
   happily draw an arrow for a move that is illegal on the current board (its only guard is a
   turn check at line 579).
3. `this.savedPlanArrows` is a *field* rather than part of the drill, so a late drill also
   swaps the arrows belonging to the previous card.
