# CODE-13 — §8.4 violation: `#trainFocusBanner` and `#trainImportantBadge` are `display`-toggled, not `visibility`-toggled
- **Severity:** high
- **Type:** spec-violation
- **File:** frontend/repertoire_trainer.js
- **Line(s):** 360-368 (`clearOpeningFocus`), 398-410 (`startNewDrill`), 473-477 and 746-750 (`trainImportantBadge`)
- **Component:** RepertoireTrainer focus banner + priority badge
- **Repro script:** `audit/scripts/CODE4_cls_scan.sh`
- **Detected by:** CODE-4

## Expected
`AGENTS.md` §8.4: conditionally available UI in toolbars, panel headers and
button groups must use `visibility: hidden|visible` (or `opacity`) with
`disabled: true|false` and a **fixed** `min-width`, never `display: none` /
`inline-flex`, when the removal would shift neighbours.

## Actual
The focus banner is the **first child of the drill sidebar** — everything below
it in that column moves:

```html
<!-- index.html:496-504 -->
<div class="drill-sidebar">
    <div id="trainFocusBanner" style="display:none; margin-bottom:0.75rem; background:rgba(56,189,248,0.12); ... justify-content:space-between; align-items:center;">
        <span style="font-size:0.8rem; color:#38bdf8; font-weight:600;">
            🎯 Focus: <span id="trainFocusName">Opening Name</span>
        </span>
        <button id="btnClearTrainFocus" class="btn-secondary" ...>Clear Focus</button>
    </div>
    <div class="drill-header"> ... </div>
```

```js
// frontend/repertoire_trainer.js:398-410  (startNewDrill — runs on EVERY drill)
const banner = document.getElementById('trainFocusBanner');
const nameSpan = document.getElementById('trainFocusName');

let url = `/api/repertoire/train/next?color=${this.activeColor}&mode=...`;
if (this.openingFilter) {
    url += `&opening=${encodeURIComponent(this.openingFilter)}`;
    if (banner && nameSpan) {
        banner.style.display = 'flex';     // <-- pushes the whole sidebar
        nameSpan.textContent = this.openingFilter;
    }
} else {
    if (banner) banner.style.display = 'none';
}
```

```js
// frontend/repertoire_trainer.js:360-368  (clearOpeningFocus)
const banner = document.getElementById('trainFocusBanner');
if (banner) banner.style.display = 'none';
```

The priority badge is inside a `flex-wrap: wrap` header:

```html
<!-- index.html:507-512 -->
<div style="display:flex; justify-content:space-between; align-items:flex-start; gap:0.5rem; flex-wrap:wrap;">
    <div>
        <div id="trainOpeningName" class="drill-title">Italian Game: Giuoco Pianissimo</div>
        <div id="trainOpeningEco" class="drill-subtitle">C50 • Prepared Repertoire Line</div>
    </div>
    <span id="trainImportantBadge" style="display:none; ...">⭐ Key Priority Move</span>
</div>
```

```js
// frontend/repertoire_trainer.js:473-477
const badgeEl = document.getElementById('trainImportantBadge');
if (badgeEl) {
    badgeEl.style.display = data.is_important ? 'inline-flex' : 'none';
}
// frontend/repertoire_trainer.js:746-750
badgeEl.style.display = data.next_is_important ? 'inline-flex' : 'none';
```

Neither element has a `min-height`/`min-width`, a CSS transition, or a
`visibility` fallback anywhere in `style.css`.

## Root cause
Direct §8.4 violation. The correct pattern is used **in the same codebase** for
the structurally identical Builder toolbar controls
(`repertoire_builder.js:1502-1508`):
`btnImportant.style.visibility = 'hidden'; btnImportant.disabled = true;`
— proving the rule was known and simply not applied to the Trainer.

## Exploit / escalation
1. **Every drill start re-flows the sidebar.** With a Focus set, each
   `startNewDrill()` that resolves without `openingFilter` — including the
   automatic `startNewDrill()` after a completed line
   (`repertoire_trainer.js:799`, `824`) and the two `all_due_completed` /
   `all_frontier_completed` recursions (lines 439, 446) — collapses the banner
   and lifts the header, prompt, note box, feedback and accordion by
   `≈ 40–45px`, then drops them back. This is a guaranteed CLS source on the
   default path because the banner is only shown when a Focus is set and the
   recursions deliberately clear it.
2. **Header wrap.** `#trainImportantBadge` appearing/disappearing in a
   `flex-wrap: wrap` `space-between` row can push `trainOpeningName` /
   `trainOpeningEco` onto a new line at narrow widths (Electron window resize,
   or the `drill-sidebar` breakpoint), changing the header height by a full
   line.
3. Adjacent weaker variant: `#btnTrainToggleArrows` is toggled
   `display:'none' | 'inline-block'` at `repertoire_trainer.js:502` and `773`
   in a `display:flex; gap:0.4rem` row with no `flex-wrap`
   (`index.html:485-488`) — lower impact because `space-between` pins the
   right-hand group, but still a §8.4 violation, and the button's *label* is
   also never given a `min-width`.
