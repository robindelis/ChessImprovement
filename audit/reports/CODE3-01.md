# CODE3-01 — "🗑️ Remove Line" button is permanently invisible (inline `display:none` beats the JS `visibility` toggle)
- **Severity:** high
- **Type:** ui-regression
- **File:** frontend/repertoire_builder.js
- **Line(s):** 336-341, 1496, 1507-1510, 1541-1544
- **Component:** Repertoire Builder — Move Tools panel
- **Repro script:** `audit/scripts/CODE3-01_repro.js`
- **Detected by:** CODE-3

## Expected
`renderMoveToolsPanel()` reveals the "🗑️ Remove Line" button (`visibility: visible`, `disabled: false`) whenever the
current step is a move that exists in the repertoire, per `AGENTS.md` §8.4 (visibility + `disabled`, never `display`).

## Actual
The button never becomes visible. `frontend/index.html:210` hard-codes `display:none` in an **inline style**:

```html
<button id="btnRepDeleteCurrentMove" class="btn-secondary btn-danger" style="font-size:0.75rem; padding:0.25rem 0.55rem; display:none;" title="Remove this move and all its continuing variations from repertoire">
    🗑️ Remove Line
</button>
```

and every JS write to that element only touches `visibility` / `disabled`:

```js
// repertoire_builder.js:1541
if (btnDelete) {
    btnDelete.style.visibility = repMove ? 'visible' : 'hidden';
    btnDelete.disabled = !repMove;
}
```

An inline `display:none` cannot be overridden by a class rule (`#btnRepDeleteCurrentMove { min-width: 110px }`,
`style.css:2783`) nor by `style.visibility`. `element.style.visibility = 'visible'` on a `display:none` element is a
no-op. Verified by reading every reference:

```
$ grep -n "btnRepDeleteCurrentMove" frontend/*.js frontend/index.html
repertoire_builder.js:336   const btnDelCurrentMove = document.getElementById('btnRepDeleteCurrentMove');
repertoire_builder.js:1496  const btnDelete = document.getElementById('btnRepDeleteCurrentMove');
index.html:210              <button id="btnRepDeleteCurrentMove" ... style="... display:none;">
```

No code path ever assigns `btnDelete.style.display`. The click handler bound at line 338
(`deleteCurrentMoveAndLine()`, 1601-1620) is therefore unreachable, i.e. the "remove the current move and all its
continuing variations" capability documented in the button's own `title` is dead in the UI.

## Root cause
HTML/JS drift: the button was hidden with an inline `display:none` and the reveal logic was later rewritten to the
`visibility` + `disabled` pattern required by `AGENTS.md` §8.4, but the inline attribute was never removed.

## Exploit / escalation
Adjacent weaker variants of the same drift pattern, all confirmed in `index.html`:
1. `frontend/index.html:487` — `<button id="btnTrainToggleArrows" ... style="display:none;">` (Trainer "🎯 Arrows");
   if `repertoire_trainer.js` only toggles `visibility`/`disabled` the trainer has the same permanently-hidden button.
2. `frontend/index.html:194` — `<div id="repQuickAddNotice" style="display:none !important;">` is a dead container:
   no JS ever references `repQuickAddNotice` (it was replaced by `window.showToast`).
3. `frontend/index.html:259` — `<span id="lblMyStatsHeader">My Stats</span>` is referenced by no JS at all, and
   `updateSortHeaderUI()` (`repertoire_builder.js:500`) overwrites the whole `<th>` with `innerHTML`, destroying it on
   the first render.
