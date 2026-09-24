# CODE-06 — `treeOverlapMap` is keyed by positional index, so overlap badges land on the wrong lines
- **Severity:** high
- **Type:** race
- **File:** frontend/scout.js
- **Line(s):** 1165-1379 (`renderTreeLines`), 1385-1393 (`scheduleOverlapCheck`), 1474-1592 (`checkTreeOverlap` / `_applyTreeBadges`)
- **Component:** Explored Lines overlap badges
- **Repro script:** `audit/scripts/CODE4_race_repro.js`
- **Detected by:** CODE-4

## Expected
Batch overlap results must be matched to lines by a **stable identity** (the
leaf node id or the UCI sequence), never by array position, and the response
must be discarded if the tree changed while it was in flight.

## Actual
The request is built with positional ids and the response is stored under the
same positional ids:

```js
// frontend/scout.js:1478-1487
const payloadLines = lines.map((l, idx) => ({
    id: String(idx),
    moves: l.moves.map(m => ({ fen_before: m.fen_before, uci: m.uci, san: m.san, ... }))
}));
...
// frontend/scout.js:1506-1516
this.treeOverlapMap = { white: {}, black: {} };
if (Array.isArray(listW)) listW.forEach(item => { this.treeOverlapMap.white[item.id] = item; });
if (Array.isArray(listB)) listB.forEach(item => { this.treeOverlapMap.black[item.id] = item; });

this._applyTreeBadges(lines);
```

and read back by position in **two** independent places:

```js
// frontend/scout.js:1198-1199 (renderTreeLines)
const wData = this.treeOverlapMap?.white?.[String(idx)];
const bData = this.treeOverlapMap?.black?.[String(idx)];

// frontend/scout.js:1529-1531 (_applyTreeBadges)
cards.forEach((card, idx) => {
    const wData = this.treeOverlapMap?.white?.[String(idx)];
    const bData = this.treeOverlapMap?.black?.[String(idx)];
```

`SessionTreeNode` already carries a stable `id`
(`scout.js:6`: `this.id = 'node_' + Math.random()...`) — it is simply never
used for this. There is no generation counter: `scheduleOverlapCheck()` is
debounced 120 ms but a request already in flight is never invalidated, and
`renderTreeLines()` (which re-reads the stale map at line 1198) runs on **every
single board move** via `updateActiveLineCounts()`.

## Root cause
Positional keys across an async boundary, plus a `treeOverlapMap` that is
replaced wholesale (`= {white:{},black:{}}`) by whichever response happens to
land last regardless of which tree snapshot it was computed for.

## Exploit / escalation
1. **Normal flow.** The user plays a move. `renderTreeLines()` re-renders the
   panel from the *previous* map. 120 ms later `checkTreeOverlap()` responds
   for the *new* snapshot and `_applyTreeBadges` mutates the buttons in place.
   If the user plays a second move in that window, the new panel is indexed
   against the older map: line #1's card receives line #2's `✓ In White` badge
   and line #2's `📥 Line → White` button is relabelled `📥 Extend White (+2)`.
2. **Insertion shifts everything.** `getAllLines()` is a DFS whose order
   depends on `children[]` insertion order. Playing a *new* first move
   (e.g. `1.d4` after `1.e4`) inserts a new index 0 and shifts every other
   line by one. The badges for the shifted lines now describe their
   neighbours, and `renderTreeLines`' `data-idx="${idx}"` click handlers
   (`scout.js:1281`, `1286`, `1289`, `1292`, `1296`, `1301`, `1305`) close
   over a **re-computed** `lines[idx]`, so a click can import/delete the wrong
   line.
3. **Silent "already present" suppression.** Because the button text becomes
   `✓ In White (4p)` (`scout.js:1226`, `1567`) purely from the map, a
   mis-indexed entry makes the UI claim a line is already in the repertoire
   when it is not — the user is then told nothing when clicking.
4. Adjacent weaker variant: the identical positional scheme exists in
   `repertoire_builder.js:2656+` / `2775+`, and the *two* files have separate
   `treeOverlapMap`s but hit the **same** endpoints
   (`/api/scout/check_tree` vs `/api/repertoire/check_tree`,
   `backend/api.py:1076` vs `:1109`) — so a Scout-triggered refresh never
   invalidates the Builder's badges and vice versa.
