# CODE-14 — §8.2/§8.4 violation: Scout quick-rep toolbar re-flows because button labels change width and buttons are `display`-toggled
- **Severity:** medium
- **Type:** ui-regression
- **File:** frontend/scout.js
- **Line(s):** 1007-1013 and 1052-1058 (`_updateMoveNoteUI`), 1457-1464 (`checkActiveLineOverlap`), 1669-1670 and 1749-1750 (import)
- **Component:** OpeningScout quick-rep toolbar (#scout-quick-rep-bar)
- **Repro script:** `audit/scripts/CODE4_cls_scan.sh`
- **Detected by:** CODE-4

## Expected
Per `AGENTS.md` §8.4, conditional actions in a toolbar must keep a **fixed
`min-width`** and switch `visibility`/`opacity` + `disabled`. Per §8.2 any
dynamic-length text needs `nowrap` + `overflow:hidden` +
`text-overflow:ellipsis` + explicit min/max-width + a dynamic `title`.

## Actual
The toolbar is a wrapping flex row with **no** width constraints on its
children:

```html
<!-- index.html:707-731 -->
<div class="scout-quick-rep-bar">
    <div style="display:flex; align-items:center; gap:0.4rem; flex-wrap:wrap;">
        <span style="font-size:0.74rem; color:var(--text-muted); font-weight:600;">Import Line:</span>
        <button id="btnScoutAddMoveWhite" class="btn-secondary btn-quick-rep" ...>📥 Line &rarr; White Rep</button>
        <button id="btnScoutAddMoveBlack" class="btn-secondary btn-quick-rep" ...>📥 Line &rarr; Black Rep</button>
        <button id="btnScoutMarkImportant" class="btn-secondary btn-quick-rep" ...>⭐ Mark Last Move Important</button>
        <button id="btnScoutRemoveFromRep" class="btn-secondary btn-quick-rep" style="display:none; color:#ef4444; ...">🗑️ Remove from Rep</button>
        <button id="btnScoutViewTree" class="btn-secondary btn-quick-rep" ...>🌳 Lines Tree (<span id="scoutQuickLineCount">0</span>)</button>
        <button id="btnScoutTrainAfterImport" class="btn-primary btn-quick-rep" style="display:none; ...">🏋️ Train in Trainer</button>
    </div>
    <div id="scoutActiveLineOverlap" class="scout-line-overlap-strip" style="display:flex;">...</div>
    ...
```

and the CSS class it relies on has **no `min-width`**:

```css
/* frontend/style.css:2413-2419 */
.btn-quick-rep {
    font-size: 0.75rem;
    padding: 0.22rem 0.5rem;
    background: #1e293b;
    border-color: #334155;
    transition: all 0.15s ease;
}
```

Three independent shift sources:

1. **Label length change** on `#btnScoutMarkImportant` — the text alternates
   between two very different widths:
   ```js
   // frontend/scout.js:1011 / 1057 / 1829
   btnImportant.textContent = '⭐ Mark Last Move Important';   // 28 chars
   btnImportant.textContent = isImportant ? '⭐ Important Move' : '⭐ Mark Last Move Important';
   ```
   Toggling importance changes the button width by roughly 90px, so
   `🌳 Lines Tree` and `🏋️ Train in Trainer` shift, and on a narrow Electron
   window the row **wraps** → a full-row vertical jump.
2. **`display` toggling** of two conditional buttons:
   ```js
   // frontend/scout.js:1457-1464
   const btnRemoveRep = document.getElementById('btnScoutRemoveFromRep');
   if (btnRemoveRep) {
       const rep = (this.orientation === 'black') ? 'black' : 'white';
       const relevantData = (rep === 'black') ? dataB : dataW;
       const inRep = relevantData && (relevantData.status === 'already_present' || relevantData.status === 'partially_present');
       btnRemoveRep.style.display = inRep ? 'inline-flex' : 'none';
   }

   // frontend/scout.js:1669-1670 and 1749-1750
   const btnTrain = document.getElementById('btnScoutTrainAfterImport');
   if (btnTrain) btnTrain.style.display = 'inline-flex';
   ```
   `#btnScoutTrainAfterImport` is never hidden again once shown, and
   `#btnScoutRemoveFromRep` flips on/off as the user walks the tree (it is
   driven by the buggy `orientation` proxy — see CODE-01 — so it can also
   flip spuriously).
3. **Unclamped dynamic text.** `#scoutNoteMoveLabel` and `#scoutTurnBadge`
   receive `Move ${move.move_number} (${move.san}) Note` and
   `White to move`/`Black to move` (`scout.js:1022`, `987`, `976`) with no
   `nowrap`/`ellipsis` in CSS. `_updateBoardLabels` *does* correctly set
   `title` on `#scoutCurrentOpeningTag` (`scout.js:974`, `984`) — but not on
   `#scoutNoteMoveLabel`, whose text also embeds a SAN and a study-derived
   move number.

Note the overlap strip itself **is** compliant: `style.css:2553-2561` gives
`.scout-line-overlap-strip` `min-height: 28px` and the JS always re-sets
`display:flex` with a "Repertoire: Starting Position" placeholder
(`scout.js:1400-1404`) exactly as §8.5 requires — except that
`checkActiveLineOverlap` returns early on the empty-line path **without**
resetting `btnScoutRemoveFromRep`, leaving it in its last state.

## Root cause
§8.4 requires fixed `min-width` + `visibility`/`opacity` for conditional
toolbar actions; this toolbar instead relies on `display` and on intrinsic
label width. The `.btn-quick-rep` rule was never given a `min-width`.

## Exploit / escalation
1. **Normal flow.** Press "⭐ Mark Last Move Important" on a move that is in
   the repertoire: the label shortens, the two buttons to its right slide left
   ~90px, and at the default Electron width the row wraps, dropping
   `🏋️ Train in Trainer` to a second line — a 30–45px jump directly above the
   `#scoutActiveLineOverlap` strip and the note bar.
2. **Toggle storm.** Walking a line in and out of the repertoire
   (`checkActiveLineOverlap` runs on a 120 ms debounce after *every* board
   move, `scout.js:1385-1393`) makes `🗑️ Remove from Rep` blink in and out,
   re-flowing the row on each transition.
3. Adjacent weaker variant: the same un-clamped pattern exists on the line
   cards — `frontend/scout.js:1204-1218` and `1545-1559` build
   `⚡ White: 4/8 (+4 new)` / `⚡ White: 4/8 in Rep (+4 new)` with **different
   lengths** into a `flex-wrap:wrap` badge strip
   (`scout.js:1271`), and the two implementations overwrite each other (see
   CODE-19), so the card visibly re-wraps after every debounce.
