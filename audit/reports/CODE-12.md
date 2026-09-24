# CODE-12 — §8.1/§8.2 violation: the Trainer note box is toggled in-flow, shoving the whole drill sidebar on every move
- **Severity:** high
- **Type:** ui-regression
- **File:** frontend/repertoire_trainer.js
- **Line(s):** 480-489 (`startNewDrill`), 752-761 (opponent-reply continuation)
- **Component:** RepertoireTrainer note box / #trainNoteBox
- **Repro script:** `audit/scripts/CODE4_cls_scan.sh`
- **Detected by:** CODE-4

## Expected
Per `AGENTS.md` §8.1, conditionally-present content must not expand or
collapse containers in the document flow. `#trainNoteBox` should reserve a
constant height (or be overlaid/replaced in place with `visibility`), and per
§8.2 its text must be clamped with `nowrap` + `overflow:hidden` +
`text-overflow:ellipsis` + a dynamic `title`.

## Actual
The box is a normal in-flow block immediately **above** `#trainFeedback` and
the "Repertoire Info" accordion in the drill sidebar:

```html
<!-- index.html:526-531 -->
<div id="trainNoteBox" style="display:none; background:rgba(30,41,59,0.85); border-left:3px solid #38bdf8; padding:0.6rem 0.8rem; border-radius:6px; font-size:0.85rem; color:var(--text-primary); margin-top:0.75rem;">
    <div style="font-size:0.72rem; color:#38bdf8; font-weight:700; text-transform:uppercase; margin-bottom:0.25rem;">📝 Move Note / Idea:</div>
    <div id="trainNoteContent" style="line-height:1.45;"></div>
</div>
...
<div id="trainFeedback" class="feedback-box"></div>
```

and it is display-toggled on **every** drill:

```js
// frontend/repertoire_trainer.js:480-489  (startNewDrill)
const noteBox = document.getElementById('trainNoteBox');
const noteContent = document.getElementById('trainNoteContent');
if (noteBox && noteContent) {
    if (data.comment && data.comment.trim()) {
        noteContent.textContent = data.comment;
        noteBox.style.display = 'block';
    } else {
        noteBox.style.display = 'none';
    }
}
```

```js
// frontend/repertoire_trainer.js:752-761  (continuing line, after the opponent reply)
const noteBox = document.getElementById('trainNoteBox');
const noteContent = document.getElementById('trainNoteContent');
if (noteBox && noteContent) {
    if (data.next_comment && data.next_comment.trim()) {
        noteContent.textContent = data.next_comment;
        noteBox.style.display = 'block';
    } else {
        noteBox.style.display = 'none';
    }
}
```

The CSS supplies **no** `min-height`, no reserved space, and no
`visibility`/`opacity` transition for `#trainNoteBox` — the only matching
inline style is the one quoted above. `#trainNoteContent` also has no
ellipsis/`nowrap`/max-width, and no `title` attribute is ever set on it, so a
long note grows the sidebar without bound.

## Root cause
Direct violation of §8.1 ("NEVER insert temporary or dynamic notification
messages … into the document flow where they expand or collapse containers or
push surrounding elements") and §8.2 (unclamped dynamic text). The reference
implementation elsewhere in the same codebase gets this right — the *identical*
concept in the Scout is an always-`display:flex` strip with a declared
`min-height` (`style.css:2553-2561`, `min-height: 28px`).

## Exploit / escalation
1. **Repro in the normal loop.** Drill a line where move 3 has a note and
   move 4 does not. `#trainFeedback` and the accordion jump down by
   `≈ 45–90px` (label + wrapped note + `0.75rem` margin) and back again on the
   next move — a visible vertical jump on *every* answer, which is the single
   most-repeated interaction in the Trainer.
2. **Unbounded growth.** A 500-character note with no `max-height` makes the
   sidebar taller than the viewport; the user must scroll to see the feedback
   box that just told them they were wrong.
3. Adjacent weaker variant: `#trainFocusBanner` is toggled with the same
   `display` pattern as the **first** child of the same sidebar
   (`repertoire_trainer.js:365`, `405`, `409`) — see CODE-13. Two independent
   in-flow toggles stacked in one column.
