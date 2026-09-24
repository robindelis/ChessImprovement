# DEAD-01 — Dead functions, dead locals, a duplicated `window.showToast`, and a CSS class with no rule
- **Severity:** medium
- **Type:** ui-regression
- **File:** frontend/scout.js, frontend/app.js, frontend/repertoire_trainer.js
- **Line(s):** see per-item list below
- **Component:** assorted dead code
- **Repro script:** `audit/scripts/CODE4_dead_selector_scan.sh`
- **Detected by:** CODE-4

## Expected
No unreferenced functions, no unused locals, one definition per global, and
every class emitted by JS has a matching CSS rule.

## Actual

**(a) `window.showToast` is defined twice in `app.js`; the first is dead.**

```js
// frontend/app.js:160-185  -- DEAD, overwritten by line 434
// Global zero-layout-shift Toast Notification System
window.showToast = function(msg, color = '#4ade80', duration = 3500) {
    ...
    toast.style.borderLeft = `4px solid ${color}`;
    const textSpan = document.createElement('span');
    textSpan.textContent = msg;
    toast.appendChild(textSpan);
    container.appendChild(toast);
    setTimeout(() => {
        toast.classList.add('toast-fadeout');      // <-- class does not exist
        ...
```

```js
// frontend/app.js:433-476  -- the live implementation
// Global Floating Toast Notification System (Zero Layout Shift)
window.showToast = function(msg, color = '#4ade80', duration = 3200) {
    ...
    toast.style.borderColor = color;               // different styling mechanism
    const dot = document.createElement('span');
    dot.className = 'app-toast-dot';
    ...
```

25 dead lines, two different default durations (3500 vs 3200), two different
border techniques, and the dead one references `toast-fadeout`, which has no
CSS rule:

```
$ grep -c "toast-fadeout" frontend/style.css
0
```

**(b) `renderVariations()` is never called.**

```js
// frontend/scout.js:1381-1383
renderVariations(variations) {
    this.renderTreeLines();
}
```

Its `variations` parameter is unused — the method is a vestige of the pre-`SessionLineTree`
design where the backend's `study.variations` drove the panel directly.

**(c) Two alias methods are never called.**

```js
// frontend/scout.js:1774-1780
async addCurrentMoveToRepertoire(repertoire) {
    return this.importToRepertoire(repertoire);
}

async addPlayedLineToRepertoire(repertoire) {
    return this.importToRepertoire(repertoire);
}
```

**(d) `loadRecent()` is unreachable** — see CODE-16.

**(e) The `'favorites'` filter branch is unreachable** — see CODE-16.

**(f) Two dead locals in `streamEvaluation`.**

```js
// frontend/scout.js:1927-1928
const barFill = document.getElementById('scoutEvalBarFill');
const barLabel = document.getElementById('scoutEvalBarLabel');

fetch(`/api/repertoire/eval_stream?fen=${encodeURIComponent(fen)}&depth=16`, { ... })
```

Neither is used in the function body; `_updateEvalBar` re-queries both
(`scout.js:1962-1963`).

**(g) `SessionTreeNode.id` is generated but never read.**

```js
// frontend/scout.js:6
this.id = 'node_' + Math.random().toString(36).substring(2, 9);
```

No code compares, serialises or looks up a node by `id` — which is exactly why
CODE-06 had to fall back to positional keys despite a stable identity being
available.

**(h) A dead assignment in `saveSettings`.**

```js
// frontend/app.js:142
const data = await res.json();   // `data` is never referenced again
```

**(i) `feedback-box mistake` has no CSS rule — the blunder feedback renders
unstyled.**

```js
// frontend/repertoire_trainer.js:849-853
if (feedback) {
    feedback.style.display = 'block';
    feedback.className = 'feedback-box mistake';   // <-- no such rule
    feedback.textContent = `✗ ${data.message}`;
}
```

`style.css` defines `.feedback-box.correct` (513), `.feedback-box.blunder`
(520), `.feedback-box.good_alternative` (527) and
`.feedback-box.suboptimal` (534) — but **not** `.mistake`. The only `mistake`
token in the CSS is `.badge-mistake` (`style.css:288`), an unrelated class.
Result: the "you got this wrong" message appears as a bare, unstyled,
unstyled-bordered block with no red background, no border and no red text — the
one feedback state that most needs visual emphasis is the only one with no
styling.

**(j) `scout-badge-youtube` / `scout-badge-guide` have no CSS rule.**

```js
// frontend/scout.js:464
const sourceBadge = item.source_type === 'youtube' ? '<span class="scout-badge-youtube">YouTube</span>' : '<span class="scout-badge-guide">Master Guide</span>';
```

```
$ grep -c "scout-badge-youtube\|scout-badge-guide\|badge-eco" frontend/style.css
0
```

The "YouTube" / "Master Guide" pills and the ECO pill (`index.html:629`) render
as unstyled inline text.

## Root cause
Successive refactors left shims behind, and two independent toast
implementations were written without either being removed. No linter
(`no-unused-vars` / `no-dupe-keys`) runs in this zero-build project.

## Exploit / escalation
1. **Silent UI regression.** `.feedback-box.mistake` is invisible to grep-based
   CSS audits, so the blunder state will keep rendering unstyled; a reviewer
   looking at `class="feedback-box blunder"` in `style.css` concludes the
   feature is styled.
2. **Toast drift.** Because the first definition is dead, any future edit made
   to it (e.g. adding a max-concurrent-toasts cap) will have **no effect**, and
   the two default durations (3500 / 3200) mean behaviour depends on which
   definition a reader assumes is live.
3. Adjacent weaker variant: `renderVariations(variations)` ignoring its
   parameter means a future caller that passes the backend's `study.variations`
   after mutating them will silently get the session tree instead — a trap for
   the next person to touch this file.
