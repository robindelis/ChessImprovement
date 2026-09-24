# CODE3-06 — Overlap checks fire 4 un-aborted POSTs per navigation with no generation counter; stale responses overwrite the overlap strip
- **Severity:** medium
- **Type:** race
- **File:** frontend/repertoire_builder.js
- **Line(s):** 2576-2594 (`updateActiveLineCounts` / `scheduleOverlapCheck`), 2596-2653 (`checkActiveLineOverlap`), 2655-2703 (`checkTreeOverlap`)
- **Component:** Repertoire Builder — repertoire overlap badges
- **Repro script:** `audit/scripts/CODE-06_overlap_race.js`
- **Detected by:** CODE-3

## Expected
`#repActiveLineOverlap` and the per-card badges always describe the line the user is currently on.

## Actual
Every position update schedules the check:

```js
// repertoire_builder.js:2583 (called from updatePosition -> updateActiveLineCounts)
scheduleOverlapCheck() {
    if (this.activeOverlapDebounce) { clearTimeout(this.activeOverlapDebounce); }
    this.activeOverlapDebounce = setTimeout(() => {
        this.checkActiveLineOverlap();
        this.checkTreeOverlap();
    }, 120);
}
```

Each of those two issues **two** `POST /api/repertoire/check_line` / `check_tree` requests (White + Black) — four
requests per navigation, none abortable, none carrying a request identity:

```js
// repertoire_builder.js:2615
const [resW, resB] = await Promise.all([ fetch('.../check_line', {repertoire:'white', ...}),
                                          fetch('.../check_line', {repertoire:'black', ...}) ]);
const dataW = await resW.json();
const dataB = await resB.json();
if (strip) { strip.innerHTML = `... ${getBadge(dataW,'White')} ${getBadge(dataB,'Black')}`; }   // unconditional
```

`checkTreeOverlap()` is worse: it rebuilds `this.treeOverlapMap` wholesale from whatever arrives last
(`this.treeOverlapMap = { white: {}, black: {} }; ... listW.forEach(...)`, lines 2687-2697) and then calls
`_applyTreeBadges(lines)` with `lines` recomputed at *its own* start, indexing by position:

```js
// repertoire_builder.js:2659
const payloadLines = lines.map((l, idx) => ({ id: String(idx), moves: ... }));   // id = DFS index
```

`getAllLines()` (85-113) enumerates leaves in DFS order, so `id: String(idx)` is **positional**. Two different
responses for two different trees (before/after a move) therefore use the *same* ids, and whichever response lands
last wins — e.g. the badges for line #2 can be applied to what is now a different line #2. Nothing verifies that
`this.lineTree` still contains `lines[idx]`.

Trigger: hold the `→` arrow key. Each press is > 120 ms apart, so the debounce never coalesces, and each press emits
4 POSTs whose latency (~50-300 ms, growing with line length) easily exceeds the inter-press interval.

## Root cause
Async callbacks that mutate shared DOM/module state without an `AbortController` and without a monotonically
increasing request token compared before the write. Contrast `loadCandidateMoves()` (2096-2114) and
`startEvalStream()` (1733, 1793), which do implement abort/identity guards — the overlap path does not.

## Exploit / escalation
1. Rapid `→`/`←` (or clicking breadcrumb chips quickly) → the strip shows `✓ White (6/6)` for a line you have already
   navigated away from, and the "📥 Line → White" button text on a card can say `✓ In White (4p)` for a line that is
   not in the repertoire → the subsequent import then silently no-ops.
2. `importToRepertoire()` (3042-3043) and `importAllLinesToRepertoire()` (3101-3102) call
   `this.checkActiveLineOverlap(); this.checkTreeOverlap();` **without awaiting and without the 120 ms debounce**,
   adding two more un-sequenced pairs on top of any already in flight.
3. `_applyTreeBadges()` (2705) mutates card DOM that `renderTreeLines()` may have just replaced via `innerHTML`
   (2804) — the `cards.forEach` then operates on detached nodes, so badges silently fail to appear.
