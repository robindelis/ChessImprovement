# CODE3-09 — `importToRepertoire` / `importAllLinesToRepertoire` call `window.promptStudySelection` without the `typeof` guard used everywhere else → unhandled rejection and a silent no-op
- **Severity:** medium
- **Type:** crash
- **File:** frontend/repertoire_builder.js
- **Line(s):** 822-838 (`onNewStudyClick`), 2988-3052, 3054-3111
- **Component:** Repertoire Builder — repertoire import
- **Repro script:** `audit/scripts/CODE-09_unhandled_rejection.js`
- **Detected by:** CODE-3

## Expected
Every call site that depends on the global study-modal helper degrades gracefully when the helper is missing, exactly
as `onNewStudyClick()` already does.

## Actual
The "+ New" button guards the dependency:

```js
// repertoire_builder.js:823
async onNewStudyClick() {
    if (typeof window.promptStudySelection !== 'function') return;
    const name = await window.promptStudySelection({...});
```

but the two import entry points do not:

```js
// repertoire_builder.js:3003 (also 3064)
studyName = await window.promptStudySelection({
    repertoire: repertoire, defaultName: defName, title: `Save Line to ... Study`
});
```

`importToRepertoire()` is bound to the always-visible primary button (`index.html:184`,
`btnRepAddLine.addEventListener('click', () => this.importToRepertoire(this.activeColor))`, line 462) and to the
per-card "📥 Line → White/Black" buttons; `importAllLinesToRepertoire()` to "📥 Import All Lines" (line 475).

If `window.promptStudySelection` is unavailable (script-order failure, a refactor that moves the helper, or a partial
load — it is defined in `app.js`, the **last** script tag in `index.html:925`, after `repertoire_builder.js:922`),
the click handler is `async` and nobody awaits it, so the `TypeError` becomes an **unhandled promise rejection**: no
toast, no console message the user sees, no navigation, and — because `this.currentStudy` is still `''` and nothing
was written — the click appears to do nothing at all. There is no `try`/`catch` around lines 2998-3010 or
3061-3070.

## Root cause
Inconsistent dependency guarding of a cross-file global, plus a click-handler pattern (`async` arrow passed straight
to `addEventListener`) with no rejection boundary.

## Exploit / escalation
1. Load `index.html` with `app.js` failing to parse (or in a CSP/offline scenario where `repertoire_builder.js` is
   cached but `app.js` 404s after a deploy): every "📥 Add Line → Repertoire" button in the primary action bar becomes
   an invisible no-op — a data-entry feature that appears broken with zero diagnostics.
2. Same unhandled-rejection shape at line 462/475/2923/2930 (`importToRepertoire` is called without `await` from
   `renderTreeLines()`'s per-card listeners), so a throw inside the `fetch` handler is equally invisible.
3. Adjacent: `loadStudies()` (785-815) returns silently on `!res.ok` (line 788) leaving the study `<select>` populated
   with stale options, and `refreshTree()` (897-905) does `await res.json()` with no `res.ok` check — a FastAPI 500
   HTML error page raises `SyntaxError` and leaves `this.treeData` stale with only a `console.error`.
