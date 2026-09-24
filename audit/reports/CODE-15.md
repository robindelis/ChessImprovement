# CODE-15 — Seven `getElementById` selectors in `scout.js` can never match `index.html`
- **Severity:** medium
- **Type:** ui-regression
- **File:** frontend/scout.js
- **Line(s):** 352-354, 578-584, 1397, 1405-1408, 1466-1468, 1595, 1604-1608
- **Component:** OpeningScout (dead selectors)
- **Repro script:** `audit/scripts/CODE4_dead_selector_scan.sh`
- **Detected by:** CODE-4

## Expected
Every DOM id referenced from JS must exist in `index.html`; every id in
`index.html` should either be referenced or removed. Drift in either direction
is a latent defect (silent no-ops today, broken selectors after an HTML edit
tomorrow).

## Actual
Mechanical cross-check of all 61 `getElementById('…')` calls in
`frontend/scout.js` against the 130 `id="…"` attributes in
`frontend/index.html`:

```
frontend/scout.js MISSING: ['btnImportBlack', 'btnImportWhite',
  'scoutActiveLineOverlapBanner', 'scoutCoreIdea', 'scoutImportAlert',
  'scoutKeyPlans', 'scoutTypicalMistakes']
```

Each is a real dead code path:

**(a) `btnImportWhite` / `btnImportBlack` — the "Bottom Banner" is gone**

```js
// frontend/scout.js:352-354
// Repertoire whole-line import buttons (Bottom Banner)
document.getElementById('btnImportWhite')?.addEventListener('click', () => this.importToRepertoire('white'));
document.getElementById('btnImportBlack')?.addEventListener('click', () => this.importToRepertoire('black'));
```

Both ids are absent from `index.html`; the surviving equivalent buttons are
`#btnScoutAddMoveWhite` / `#btnScoutAddMoveBlack` (`index.html:710`, `713`),
already wired at `scout.js:346-347`. Two lines of dead wiring plus a stale
comment.

**(b) `scoutCoreIdea` / `scoutKeyPlans` / `scoutTypicalMistakes` — the whole
"strategic ideas" panel was removed from the HTML**

```js
// frontend/scout.js:577-584
// Strategic ideas
const ideas = study.strategic_ideas || {};
const coreEl = document.getElementById('scoutCoreIdea');
if (coreEl) coreEl.textContent = ideas.core_idea || 'Study harmonious piece development and center control.';
const keyEl = document.getElementById('scoutKeyPlans');
if (keyEl) keyEl.textContent = ideas.key_plans || 'Complete piece development, safeguard king, and calculate tactical threats.';
const mistEl = document.getElementById('scoutTypicalMistakes');
if (mistEl) mistEl.textContent = ideas.typical_mistakes || 'Premature pawn pushes, moving pieces multiple times, leaving king uncastled.';
```

`AGENTS.md` §4E still advertises "opening themes, core ideas, key plans, and
common tactical mistakes" as Scout features; the HTML no longer has the
container, so the backend's `strategic_ideas`
(`backend/scout.py:239`, `get_strategic_ideas(eco, opening_name)`) is computed
on every analyse and then thrown away by the client.

**(c) `scoutActiveLineOverlapBanner` — a second, obsolete strip**

```js
// frontend/scout.js:1396-1397, 1405-1408, 1466-1468
const strip = document.getElementById('scoutActiveLineOverlap');
const banner = document.getElementById('scoutActiveLineOverlapBanner');
...
if (banner) { banner.style.display = 'none'; banner.innerHTML = ''; }
...
if (banner) { banner.style.display = 'none'; }
```

The live element is `#scoutActiveLineOverlap` (`index.html:729`); the "banner"
variant is referenced three times and never exists. `style.css:2563` still
carries a `.scout-overlap-banner` rule for it.

**(d) `scoutImportAlert` — an in-flow notice that violates §8.1 *and* does not
exist**

```js
// frontend/scout.js:1595, 1602-1610
const alertEl = document.getElementById('scoutImportAlert');
...
if (!movesToImport || movesToImport.length === 0) {
    const msg = 'No moves played to import yet. Play moves on the board first!';
    if (alertEl) {
        alertEl.style.display = 'block';
        alertEl.style.color = '#ef4444';
        alertEl.textContent = msg;
    }
    this._showQuickAddNotice(msg, '#ef4444');
    return;
}
```

This is dead *and* it is a textbook §8.1 violation (in-flow `display:block`
notice). The compliant `_showQuickAddNotice` call on the next line is the only
reason the user still gets feedback.

## Root cause
HTML and JS were edited independently over several rounds. `AGENTS.md` §4E was
not updated when the corresponding HTML was deleted, so the docs now describe
UI that no longer exists.

## Exploit / escalation
1. **Docs drift.** `AGENTS.md` §4E and the Scout panel subtitle
   (`index.html:553-555`, "…and commentary") promise core-ideas / key-plans /
   typical-mistakes content that is never rendered — a spec violation that a
   reader of the docs would report as a missing feature.
2. **Dead CSS.** `style.css:2563` `.scout-overlap-banner` has no HTML consumer.
   Conversely `scout-badge-youtube` / `scout-badge-guide`
   (emitted at `scout.js:464`) and `feedback-box mistake` (set at
   `repertoire_trainer.js:851`) have **no CSS rule at all**:
   ```
   $ grep -c "scout-badge-youtube\|scout-badge-guide\|\.feedback-box\.mistake" frontend/style.css
   0
   ```
   so the YouTube/Master-Guide source badges render as unstyled inline text and
   the blunder feedback loses its background/border/colour (see CODE-22).
3. Adjacent weaker variant: the reverse direction also holds — 15 ids in
   `index.html` are referenced by **no** JS file (see CODE-10 for the
   high-value subset: `scoutCurrentTimeDisplay`, `scoutLiveIndicator`,
   `scoutResultsCount`, `scoutQuickAddNotice`).
