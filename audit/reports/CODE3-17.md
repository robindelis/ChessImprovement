# CODE3-17 — DEAD cluster: 8 selectors that can never match, one dead DOM node, one dead alias and one permanently-false cache fallback
- **Severity:** low
- **Type:** ui-regression
- **File:** frontend/repertoire_builder.js
- **Line(s):** 252-268, 315-320, 590-603, 632-634, 655-656, 718-721, 913, 1938-1939, 3173-3175
- **Component:** Repertoire Builder — various
- **Repro script:** `audit/scripts/CODE-17_dead_selectors.js`
- **Detected by:** CODE-3

## Expected
Every `getElementById` in the builder resolves against `frontend/index.html`, and every documented control exists.

## Actual
Cross-checking every literal `getElementById('X')` in `repertoire_builder.js` against the `id="..."` attributes in
`index.html` (and against ids created dynamically in JS) yields these **unmatchable** selectors:

| Selector | JS site | Effect |
|---|---|---|
| `repOpeningThreshold` | 267 (`setupOpeningThreshold`) | never bound |
| `repOpeningThresholdSelect` | 268 | never bound |
| `repOpeningThresholdLabel` | 261-262 | never updated |
| `btnResyncGames` | 315, 599, 616-621, 777-780 | full-resync button enable/disable + "⏳ Scanning Games..." is dead |
| `repGapThresholdDisplay` | 913, 928 | threshold echo never rendered |
| `syncCountNewBlunders` | 632, 655, 716, 738 | "new blunders" counter never shown |
| `syncBlunderPill` | 633, 656, 718-721 | the `if (data.new_blunders > 0 && blunderPill)` branch is unreachable |
| `syncBlunderCount` | 634, 657, 720 | ditto |

The opening-cutoff feature documented in `AGENTS.md` §4.B.3 ("configurable via dropdown: 10, 20, 30, 50, 100") is
therefore **entirely absent**: `this.openingGamesThreshold` is initialised to `30` (line 178) and the only other
assignments live inside the two never-bound `change` listeners, so `/api/repertoire/tree?min_games=30` is hard-coded
in practice.

Other dead items in the same file:

```js
// repertoire_builder.js:3173 — alias with zero callers
showNotice(msg, color = '#4ade80') { this.showQuickNotice(msg, color); }
```

```js
// repertoire_builder.js:1938 — the second key is never written anywhere
const cacheKey = `${this.activeColor}_${normParent}`;
const cached = this.candidateCache.get(cacheKey) || this.candidateCache.get(normParent);
// `candidateCache` is only ever written as `${this.activeColor}_${normFen}` (line 2106) or cleared (line 756)
```

so `stepForward()`'s documented fallback — "play the top 2000+ candidate move if available in cache" (comment at
line 1933) — **never fires**: pressing "Next ▶" at the end of a line with no prepared repertoire move does nothing at
all, with no message.

Dead DOM on the JS side:

* `frontend/index.html:194` — `<div id="repQuickAddNotice" style="display:none !important;"></div>`: no JS reference
  anywhere (superseded by `window.showToast`).
* `frontend/index.html:259` — `<span id="lblMyStatsHeader">My Stats</span>`: no JS reference, and
  `updateSortHeaderUI()` line 500 (`el.innerHTML = \`${h.label} <span class="sort-arrow">${arrow}</span>\``) destroys
  the span on the very first render.

Corroborating dead CSS (zero references in any `.js` or in `index.html`, verified by grep):
`#repGapsList` (`style.css:1107`), `.repertoire-sidebar-card .gap-item` (1171), `.gap-san` (1177), `.gap-meta` (1181),
`.badge-critical` (1185), `.badge-patched` (1192), `.breadcrumb-chip.blunder-step` (1030) and
`.scout-overlap-banner` (2560) — the entire stylesheet surface of the removed "Address Critical Gaps" panel and of the
"Show Patched (N)" badge. Combined with CODE3-03 (`#blunderBannerAlert` missing) this is conclusive: the gap/blunder
UI was deleted from the HTML while both its CSS and its JS were kept.

## Root cause
Long-lived copy-paste evolution with no dead-code sweep: features were re-implemented elsewhere (toasts replaced
inline notices; the sync card was redesigned; the opening-cutoff dropdown was dropped from the top bar) but the
original code paths, their DOM hooks and their now-meaningless `if (x)` guards were all retained.

## Exploit / escalation
1. The `if (el)` guards are what make this silent — a user clicking a control that is not there gets no error, and
   the `title` attributes still advertise features that do not exist (e.g. `index.html:92`
   `title="Reset and seed Italian Game, Sicilian Dragon, and King's Indian"` on a button whose endpoint now requires
   `?force=true` — see CODE3-22).
2. `renderBlunderBanner` / the blunder banner are the same class of rot and take the whole feature with them
   (CODE3-03) — worth a single lint rule (`getElementById` targets must exist in `index.html`) to prevent recurrence.
