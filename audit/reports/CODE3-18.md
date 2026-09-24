# CODE3-18 — DUP: five near-identical code clusters in the builder (~250 duplicated lines), including two divergent copies of the same badge renderer
- **Severity:** low
- **Type:** ui-regression
- **File:** frontend/repertoire_builder.js
- **Line(s):** 2705-2772 vs 2811-2854; 2988-3052 vs 3054-3111; 615-783 vs 1045-1251; 1861-1885 vs 1429-1483; 1253-1328 vs 2359-2441
- **Component:** Repertoire Builder
- **Repro script:** `audit/scripts/CODE-18_dup.js`
- **Detected by:** CODE-3

## Expected
One canonical implementation per behaviour; the second occurrence is expressed in terms of the first.

## Actual

**1. Overlap-badge renderer — 2 copies, ~62 lines, and they have already diverged.**
`_applyTreeBadges()` (2705-2772) and the badge block inside `renderTreeLines()` (2811-2854) implement the *same*
`already_present` / `partially_present` / `new` decision tree twice, character for character:

```js
// 2813 (renderTreeLines)
if (wData.status === 'already_present')      badgesHtml += `<span class="scout-overlap-badge rep-present" title="${wData.message}">✓ White (${wData.matched_plies}/${wData.total_plies})</span>`;
else if (wData.status === 'partially_present') badgesHtml += `<span class="scout-overlap-badge rep-partial" title="${wData.message}">⚡ White: ${wData.matched_plies}/${wData.total_plies} (+${wData.new_plies} new)</span>`;
else                                           badgesHtml += `<span class="scout-overlap-badge rep-new" title="${wData.message}">★ White New</span>`;
```

```js
// 2725 (_applyTreeBadges) — identical strings, plus duplicated White/Black blocks and the button-label branch
if (wData.status === 'already_present') { html += `<span ...>✓ White (${wData.matched_plies}/${wData.total_plies})</span>`; }
...
if (wData.status === 'already_present') { btnW.innerHTML = `✓ In White (${wData.matched_plies}p)`; btnW.title = wData.message; }
```

They are already inconsistent: `_applyTreeBadges` *mutates* the existing card's badges container, while
`renderTreeLines` *regenerates* the whole `innerHTML`. `_applyTreeBadges` is therefore only correct when the DOM has
not been rebuilt since the last `checkTreeOverlap` — and `renderTreeLines()` rebuilds it on every navigation, which is
exactly the bug described in CODE3-06.3. Canonical version: one `renderOverlapBadges(data, repName)` returning HTML,
used by both.

Canonical for the rest:
* `importToRepertoire` (2988) is the model; `importAllLinesToRepertoire` (3054) is a clone of it (the only
  differences: the source of `movesToImport` and one message string) — **~48 duplicated lines** (the probe counts
  the identical lines).
* `jumpToStep`'s animated branch (1861-1885) re-implements 9 of the 12 statements of `updatePosition()` (1429-1483) —
  this duplicate is the direct cause of CODE3-02.
* `resyncGames` (615-783, 169 lines) and `syncRecentGames` (1045-1251, 207 lines) share ~85 lines of identical SSE
  scaffolding (buffer/decoder/`data: ` split/`JSON.parse`/progress writes/`AbortError` handling/`finally` button
  restore) — the two `buffer.split('\n\n')` + `startsWith('data: ')` + `new TextDecoder()` triples in the file are
  these two functions plus `startEvalStream`.
* `reviewGame` (1253-1328) and `addressGameGap` (2359-2441) share the ~25-line "replay SAN list into `this.history`"
  loop.
* `onBoardMove` (2001-2020) and `ensureHistoryInLineTree` (2537-2556) build the same 14-field `moveObj` literal twice.
* Cross-file: `window.showToast` is defined **twice** in `frontend/app.js` (line 161 and line 434); the second
  definition silently overrides the first, and the first (~25 lines) is dead.

## Root cause
Copy-paste-first evolution with no shared helper module, plus no lint rule against re-implementing an existing
branch. The zero-build, no-bundler constraint of `AGENTS.md` §6.1 makes an inline helper the natural fix, so the
duplication is not architecturally forced.

## Exploit / escalation
1. The divergence in cluster 1 is already user-visible: after a `checkTreeOverlap()` response arrives for a stale
   tree, cards can display a `★ White New` badge that the current `renderTreeLines()` output would have computed
   differently.
2. Fixing the import-confirmation copy-paste (e.g. adding a "no study name" guard) in one function leaves the other
   unpatched — the two `import*` functions are exactly the pair an attacker/attacker-agent would probe for divergent
   validation.
3. `app.js`'s two `showToast` definitions differ in defaults (`3500` vs `3200` ms) and in markup (`.app-toast-text`
   wrapper vs bare span), so any caller that relied on the first version's DOM gets a different toast.
