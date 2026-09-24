# CODE3-03 — `#blunderBannerAlert` does not exist in index.html: the whole blunder/gap review feature (~160 lines) is unreachable
- **Severity:** high
- **Type:** spec-violation
- **File:** frontend/repertoire_builder.js
- **Line(s):** 1834-1841, 2029-2080, 2359-2441 (`addressGameGap`), 2443-2482 (`addressGap`)
- **Component:** Repertoire Builder — blunder banner & gap review
- **Repro script:** `audit/scripts/CODE3-03_dead_selectors.js`
- **Detected by:** CODE-3

## Expected
`AGENTS.md` §4.B/§4.D describe a blunder-review flow: after a game is loaded, the blunder banner is shown, the user can
"Adopt <move> in Repertoire", and `#2` is promoted to "the new #1 BIGGEST BLUNDER"; the "Show Patched (N)" toggle
drives `include_patched=true` on `/api/repertoire/tree`.

## Actual
Every one of those code paths hangs off a container that does not exist in the DOM.

```js
// repertoire_builder.js:1834
renderBlunderBanner(curr) {           // note: `curr` is never used
    if (this.activeReviewGame) {
        this.renderGameReviewBanner(this.activeReviewGame);
    } else {
        const banner = document.getElementById('blunderBannerAlert');
        if (banner) banner.style.display = 'none';   // always null -> no-op
    }
}
```

```js
// repertoire_builder.js:2029
if (wasCorrectBlunderSolution && solvedBlunder) {
    const banner = document.getElementById('blunderBannerAlert');
    if (banner) {                                 // always null -> whole block skipped
        banner.innerHTML = `... <button id="btnAdoptSolvedMove">...`;
        ...
        btnAdopt.addEventListener('click', async () => { ... this.addressGameGap(nextGap); });
    }
}
```

Selector audit (script `audit/scripts/CODE3-03_dead_selectors.js` cross-checks every literal
`getElementById` against `id="..."` in `index.html`):

```
MISSING: blunderBannerAlert
```

Consequences:
* The "+ Adopt N in Repertoire" button is never rendered → the interactive blunder-solution flow never happens.
* `addressGameGap()` (2359-2441, 83 lines) is reachable **only** from that dead button handler and from `addressGap()`.
* `addressGap()` (2443-2482, 40 lines) has **zero** callers anywhere in `frontend/`:
  `grep -n "addressGap" *.js` → `repertoire_builder.js:2443` (definition), `2445` (internal call only).
* Therefore `this.treeData.game_gaps` is never rendered (its only consumer is line 2065 inside the dead block) and
  `this.treeData.gaps` (top-20 unprepared opponent moves, returned by the backend on every `/api/repertoire/tree`
  call) is never rendered either — `renderCoverageAndGaps()` (907-946) renders only `coverage_pct` and a games count
  despite its name.
* `this.showPatchedGaps` is initialised to `false` at lines 179 and 861 and **never set to `true`** anywhere, so
  `include_patched=false` is always sent (line 899) and the documented "Show Patched (N)" toggle does not exist.
* `this.activeReviewGap` is therefore permanently `null`; the `Reset` button's
  `if (this.activeReviewGame || this.activeReviewGap)` (line 295) can only ever be true via the review path.

Line 1256 is direct evidence the drift is test-driven rather than product-driven:

```js
this.activeReviewGap = null; // keeps test requirement satisfied
```

`tests/test_user_workflows.py:541` only asserts that the *string* `this.activeReviewGap = null` appears in the source.

## Root cause
The banner container was deleted from `index.html` (or never added) while the JS that targets it, and the whole
gap-analysis renderer built on top of it, were kept. Every dependent function silently degrades to a no-op because
each DOM lookup is individually `if (x)`-guarded.

## Exploit / escalation
1. `sort`/`title` drift: `frontend/index.html:92` still advertises
   `title="Reset and seed Italian Game, Sicilian Dragon, and King's Indian"`, and `seedDefaults()` (540-551) calls
   `POST /api/repertoire/seed` **without** `?force=true` — the endpoint requires `force=true` (`backend/api.py`), so the
   button silently does nothing and only `console.error`s.
2. Dead sibling selectors from the same drift wave: `repGapThresholdDisplay`, `repOpeningThresholdLabel`,
   `syncCountNewBlunders`, `syncBlunderPill`, `syncCountNewBlunders` (see CODE3-17) — the sync card's "New Blunders"
   pill and the configurable "Opening Cutoff 10/20/30/50/100" dropdown from `AGENTS.md` §4.B.3 are both absent.
