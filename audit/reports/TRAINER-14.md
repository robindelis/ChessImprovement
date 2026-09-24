# TRAINER-14 — Malformed `arrows` metadata kills the whole drill: the exception is swallowed and the trainer never enables input
- **Severity:** medium
- **Type:** crash
- **File:** frontend/board.js
- **Line(s):** 588-592 (`_getSquareCenterPercent`), 134-137 (`setArrows`), 545-547 (`_renderArrows`); backend/repertoire.py 855-857 (`update_move_metadata` stores `arrows` unvalidated)
- **Component:** Repertoire trainer — arrow rendering
- **Repro script:** `audit/scripts/atk2_10_frontend_race.mjs` (scenario 4)
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
`POST /api/repertoire/move_meta` should validate the `arrows` payload (array of
`{from,to,color}` with real squares) exactly as it validates `comment` length, and the
renderer should be defensive. A bad annotation must not make a drill unanswerable.

## Actual
`update_move_metadata` stores whatever string it is given:
```python
if arrows is not None:
    updates.append("arrows = ?")
    params.append(arrows.strip())          # no json.loads, no shape check
```
`setArrows` only checks `Array.isArray` and then `_renderArrows` dereferences `arr.from`
without a guard (board.js:570 → 588-592):
```js
_getSquareCenterPercent(sq) {
    const file = sq.charCodeAt(0) - 97;    // TypeError when sq is undefined
```

In `startNewDrill` the call sits inside the big `try { … } catch (e) { console.error(…) }`,
and the two statements that make the drill usable — `this.isWaitingForUser = true` and
`this.updateHintButtonState(true)` — come **after** it, so they never run.

Observed:

```
4) malformed `arrows` JSON kills the drill (board.js:589 has no guard)
   Error starting repertoire drill: TypeError: Cannot read properties of undefined (reading 'charCodeAt')
     at Board._getSquareCenterPercent (board.js:589)
     at Board._renderArrows (board.js:547)
     at Board.setArrows (board.js:136)
     at RepertoireTrainer.startNewDrill (repertoire_trainer.js:506)
   currentDrill        : e4
   isWaitingForUser    : false   <-- input never enabled
   hint button disabled: true
   arrows that were stored: [{"note":"e2-e4"}]
   the exception is swallowed by the try/catch in startNewDrill() (line 510)
```

The board shows the position, the breadcrumb and study name are correct, and the card is
permanently unanswerable — the only symptom is one line in the devtools console. The user
must press "New Drill", which re-serves the same poisoned card.

## Root cause
No server-side validation of `arrows`, no shape guard in the renderer, and a wide `try/catch`
that swallows render errors while the state it is responsible for enabling is inside the same
block.

## Exploit / escalation
1. Any non-empty JSON array whose items lack `from`/`to` works: `'[{}]'`, `'[{"note":"x"}]'`,
   `'[null]'` (the latter also crashes `addArrow` in the wrong-answer path, line 857).
2. A *string* that parses to an array of numbers (`'5'` parses to `5`, so `.length` is
   `undefined` and it is skipped) is harmless — the failure needs a real array, which the
   builder produces from its own arrow-drawing UI, so a partially-drawn/erased arrow set is
   the realistic trigger.
3. The same swallow-and-continue pattern hides `JSON.parse` failures of `data.arrows` on the
   continuation path (line 764-770), so a corrupted continuation silently drops the arrows
   instead of reporting it.
