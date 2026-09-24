# TRAINER-13 — The prepared move's own plan arrows are rendered on the board *before* the attempt, drawing the answer; the same render path also violates the zero-CLS rules
- **Severity:** medium
- **Type:** wrong-result
- **File:** frontend/repertoire_trainer.js
- **Line(s):** 491-506 (arrow pre-render), 762-779 (continuation re-render), 473-489 (`display` toggles), 515-527
- **Component:** Repertoire trainer — pre-attempt rendering
- **Repro script:** `audit/scripts/atk2_10_frontend_race.mjs` (scenario 3)
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
Nothing derived from the *answer* may be displayed before the user commits a move, and
conditional controls in a fixed toolbar must toggle `visibility`/`opacity`, never `display`
(`AGENTS.md` §8.4).

## Actual
`GET /api/repertoire/train/next` returns `expected_uci`, `expected_san`, `comment` and
`arrows` for the chosen move (verified on the API):
```
{"expected_uci": "e2e4", "expected_san": "e4", "comment": "king pawn punch",
 "arrows": "[{\"from\": \"e2\", \"to\": \"e4\", \"color\": \"#22c55e\"}]"}
arrows describe the answer move itself: True
```
and `startNewDrill` draws them unconditionally, with no "revealed" state:

```js
this.savedPlanArrows = JSON.parse(data.arrows) …
if (this.savedPlanArrows && this.savedPlanArrows.length) {
    this.board.setArrows(this.savedPlanArrows);      // drawn on arrival
}
```

`showHint()` then draws *exactly the same* arrow (`addArrow(from, to, '#22c55e')` from
`expected_uci`), i.e. the "Hint" button is redundant for any card whose author drew plan
arrows. The same happens on every line continuation (lines 762-779), where `next_arrows` of
the *next* answer are drawn before that move is played.

The same render path also toggles `display`:

```js
badgeEl.style.display  = data.is_important ? 'inline-flex' : 'none';
btnArrows.style.display = (…) ? 'inline-block' : 'none';
noteBox.style.display  = … ? 'block' : 'none';
```

Observed:

```
3) CLS: conditional toolbar controls toggled with display:none / inline-flex
   #trainImportantBadge display after a NON-important drill: "none"
   #trainImportantBadge display after an IMPORTANT drill  : "inline-flex"
   #btnTrainToggleArrows display                          : "inline-block"
   board arrows drawn BEFORE the user attempts anything  : [{"from":"e2","to":"e4","color":"#22c55e"}]
   (AGENTS.md 8.4 forbids display toggling in fixed toolbars)
```

## Root cause
The drill DTO mixes "what to show before the attempt" (breadcrumb, study, comment) with
"the answer" (`expected_uci`, `expected_san`, `arrows`), and the client treats the whole DTO
as displayable. The `is_important` / arrows-button / note-box visibility is implemented with
`display` instead of `visibility`+`disabled` or `opacity`.

## Exploit / escalation
1. **Curriculum leak**: because plan arrows are authored per prepared move, a user who
   annotates their repertoire (the documented workflow — `AGENTS.md` §5 "arrows … shown on
   board and in Trainer") automatically sees the answer for every card they annotated, with
   no hint click. If an arrow is stored for a *plan* square rather than the move, the trainer
   still shows it as if it were the solution.
2. `data.comment` is written with `textContent` (safe) but the note box uses
   `display: block/none`, so a long comment appearing/disappearing between cards shifts the
   board — a direct §8.1/§8.4 violation on every drill transition.
3. `updateBreadcrumb` (line 524) uses `movesEl.innerHTML = '<span …>'` — the only `innerHTML`
   in the trainer, but it means the element's clamping rules from §8.2/§8.3 must be verified
   in CSS; `textContent` is used everywhere else, so the pattern is inconsistent.
