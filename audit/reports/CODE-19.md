# CODE-19 — Two divergent implementations of the same overlap badge; text mutates under the user and neither matches AGENTS §4E
- **Severity:** medium
- **Type:** spec-violation
- **File:** frontend/scout.js
- **Line(s):** 1201-1244 (`renderTreeLines`), 1542-1591 (`_applyTreeBadges`)
- **Component:** Scout overlap badges
- **Repro script:** `audit/scripts/CODE4_badge_drift_repro.js`
- **Detected by:** CODE-4

## Expected
`AGENTS.md` §4E specifies exactly three badge forms:
`✓ In White`, `⚡ White: 4/8 in Rep (+4 new)`, `★ New Line`.
There must be **one** renderer so the badge cannot change text under the user.

## Actual
The same badge markup is written twice, with different strings.

`renderTreeLines()` (executed on every move):

```js
// frontend/scout.js:1202-1209
if (wData.status === 'already_present') {
    badgesHtml += `<span class="scout-overlap-badge rep-present" title="${wData.message}">✓ White (${wData.matched_plies}/${wData.total_plies})</span>`;
} else if (wData.status === 'partially_present') {
    badgesHtml += `<span class="scout-overlap-badge rep-partial" title="${wData.message}">⚡ White: ${wData.matched_plies}/${wData.total_plies} (+${wData.new_plies} new)</span>`;
} else {
    badgesHtml += `<span class="scout-overlap-badge rep-new" title="${wData.message}">★ White New</span>`;
}
```

`_applyTreeBadges()` (executed 120 ms later by the debounced overlap check, and
again by every post-import refresh):

```js
// frontend/scout.js:1544-1550
if (wData.status === 'already_present') {
    html += `<span class="scout-overlap-badge rep-present" title="${wData.message}">✓ White (${wData.matched_plies}/${wData.total_plies})</span>`;
} else if (wData.status === 'partially_present') {
    html += `<span class="scout-overlap-badge rep-partial" title="${wData.message}">⚡ White: ${wData.matched_plies}/${wData.total_plies} in Rep (+${wData.new_plies} new)</span>`;
} else {
    html += `<span class="scout-overlap-badge rep-new" title="${wData.message}">★ White New</span>`;
}
```

The `partially_present` strings differ:
`⚡ White: 4/8 (+4 new)` vs `⚡ White: 4/8 in Rep (+4 new)`.
`AGENTS.md` §4E mandates the **second** form, so `renderTreeLines` renders the
non-compliant text on every move and `_applyTreeBadges` then rewrites it —
a visible one-word reflow inside a `flex-wrap: wrap` strip
(`scout.js:1271`), i.e. a live §8.2/§8.3-adjacent layout shift.

Neither uses the `★ New Line` wording from §4E (`★ White New` is used instead),
and `already_present` is `✓ White (4/8)` rather than `✓ In White` (§4E's form;
the `getBadge` helper in `checkActiveLineOverlap` at `scout.js:1439-1447` *does*
produce `✓ In White (4/8)`, so there are **three** variants of the same badge
across the file).

The `✓ In White (Np)` button label (lines 1226/1238 vs 1567/1581) has the same
duplication, and `_applyTreeBadges` also *reverts* the button label for the
`new` case where `renderTreeLines` had already customised it — see
`scout.js:1222-1232` vs `1573/1587`.

## Root cause
`renderTreeLines` re-renders the whole panel from scratch and *also* re-derives
the badges from the cached map, while a second, DOM-patching renderer exists
for the async refresh. Neither reads the other's output, and no shared
`overlapBadgeHtml(data, repName)` helper exists.

## Exploit / escalation
1. **Visible text mutation.** Play a move whose line is partially in the
   repertoire. The card shows `⚡ White: 4/8 (+4 new)` immediately, then
   ~120 ms later the text becomes `⚡ White: 4/8 in Rep (+4 new)` as the badge
   strip re-wraps. The user sees the panel twitch on every move.
2. **Tooltip loss.** `renderTreeLines` sets `title="${wData.message}"` — the
   only place the backend's replacement warning
   (`"At move 3, Nc3 replaces your prepared Nf3 for White!"`) is exposed at
   all. `_applyTreeBadges` also sets it, but if `wData` is missing (a response
   that arrived for a stale tree, CODE-06) the `if (btnW && wData)` guard
   leaves the **previous** title in place, so a stale warning keeps hovering on
   a line it no longer describes.
3. Adjacent weaker variant: `checkActiveLineOverlap`'s `getBadge`
   (`scout.js:1439-1447`) is a *third* copy of the same three-way branch,
   producing `✓ In White (4/8)` / `⚡ White: 4/8 (+4 new)` / `★ White: New` for
   the strip under the board — so the strip and the cards can simultaneously
   show different wording for the same line.
