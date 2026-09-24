# CODE-22 — Scout keyboard handler hijacks the Chapter dropdown and swallows Ctrl/Cmd+Z
- **Severity:** low
- **Type:** ui-regression
- **File:** frontend/scout.js
- **Line(s):** 321-343
- **Component:** OpeningScout keyboard shortcuts
- **Repro script:** `audit/scripts/CODE4_keyboard_repro.js`
- **Detected by:** CODE-4

## Expected
A global shortcut handler must ignore events originating in **any** focusable
form control — including `<select>` — and must not swallow browser/OS
shortcuts such as `Ctrl/Cmd+Z`.

## Actual
```js
// frontend/scout.js:321-343
// Keyboard navigation when scout tab is visible
window.addEventListener('keydown', (e) => {
    const scoutView = document.getElementById('viewScout');
    if (!scoutView || !scoutView.classList.contains('active')) return;
    if (document.activeElement && (document.activeElement.tagName === 'INPUT' || document.activeElement.tagName === 'TEXTAREA')) return;

    if (e.key === 'ArrowLeft' || e.key === 'h') {
        e.preventDefault();
        this.prevStep();
    } else if (e.key === 'ArrowRight' || e.key === 'l') {
        e.preventDefault();
        this.nextStep();
    } else if (e.key === 'Home' || e.key === 'ArrowUp' || e.key === 'k') {
        e.preventDefault();
        this.firstStep();
    } else if (e.key === 'End' || e.key === 'ArrowDown' || e.key === 'j') {
        e.preventDefault();
        this.lastStep();
    } else if (e.key === 'u' || (e.ctrlKey && e.key === 'z')) {
        e.preventDefault();
        this.undoUserMove();
    }
});
```

`SELECT` is missing from the guard. The sibling implementation in the Trainer
gets it right:

```js
// frontend/repertoire_trainer.js:118-121
const tag = e.target.tagName?.toLowerCase();
if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) {
    return;
}
```

Note also that Scout checks `document.activeElement` while the Trainer checks
`e.target` — the former misses events dispatched at a non-focused descendant and
misses `contentEditable` entirely.

## Root cause
The two keyboard handlers were written independently and diverged; neither is
registered on a shared, tab-scoped helper.

## Exploit / escalation
1. **Chapter dropdown is unusable with the keyboard.** Focus
   `#scoutChapterSelect` (`index.html:676`) and press `ArrowDown`: the
   `<select>`'s own value change is cancelled by `e.preventDefault()` and
   instead `this.lastStep()` runs, jumping the board to the end of the line.
   The user cannot change chapters with the arrow keys at all.
2. **`Home`/`End` in the dropdown** likewise jump the board to the first/last
   position instead of moving to the first/last option.
3. **Undo is hijacked.** `Ctrl+Z` / `Cmd+Z` inside the Scout tab is
   unconditionally `preventDefault()`ed, so native text-undo is unavailable in
   the note textarea (the textarea is *excluded* from the handler only while it
   has focus, so `Ctrl+Z` there is fine — but with focus anywhere else in the
   Scout tab, `Cmd+Z` is swallowed and the browser cannot undo).
4. Adjacent weaker variant: `e.key === 'u'` fires on plain `u` with no
   modifier and no `repeat` guard, so holding `u` replays `undoUserMove()` →
   `resetBoard()` → `_showQuickAddNotice(...)` at key-repeat rate, spamming the
   toast container (which has no max-concurrent cap).
