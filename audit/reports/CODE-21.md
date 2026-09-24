# CODE-21 — Four `fetch` calls with no cancellation per board move; overlap results are read after the tree has changed
- **Severity:** medium
- **Type:** race
- **File:** frontend/scout.js
- **Line(s):** 1378, 1385-1393, 1395-1472, 1474-1522, 666-713
- **Component:** OpeningScout overlap checking
- **Repro script:** `audit/scripts/CODE4_race_repro.js`
- **Detected by:** CODE-4

## Expected
A debounce plus an in-flight guard (or `AbortController`) so that rapid board
navigation issues at most one pair of overlap checks and that a response is
only applied to the tree snapshot it was computed for.

## Actual
`scheduleOverlapCheck()` is called from **two** places on **every** board
mutation:

```js
// frontend/scout.js:944-957  (updateActiveLineCounts — called by _onUserBoardMove,
//                             undoUserMove, nextStep, lastStep, resetBoard,
//                             loadTreeLine, firstStep, openStudio)
...
this.scheduleOverlapCheck();

// frontend/scout.js:1378  (renderTreeLines — called by all of the above too)
...
this.scheduleOverlapCheck();
```

and it unconditionally fires **four** requests:

```js
// frontend/scout.js:1385-1393
scheduleOverlapCheck() {
    if (this.activeOverlapDebounce) { clearTimeout(this.activeOverlapDebounce); }
    this.activeOverlapDebounce = setTimeout(() => {
        this.checkActiveLineOverlap();      // 2 x POST /api/scout/check_line
        this.checkTreeOverlap();            // 2 x POST /api/scout/check_tree
    }, 120);
}
```

Neither `checkActiveLineOverlap` (line 1421) nor `checkTreeOverlap` (line 1490)
uses an `AbortController`; `this.activeOverlapDebounce` only cancels the
*pending* timer, never an in-flight request. `checkTreeOverlap` then writes
`this.treeOverlapMap` from whatever snapshot it captured:

```js
// frontend/scout.js:1506-1518
this.treeOverlapMap = { white: {}, black: {} };
if (Array.isArray(listW)) listW.forEach(item => { this.treeOverlapMap.white[item.id] = item; });
if (Array.isArray(listB)) listB.forEach(item => { this.treeOverlapMap.black[item.id] = item; });
this._applyTreeBadges(lines);
```

and `checkActiveLineOverlap` writes the strip plus a button's `display` from a
possibly-stale dataset (`scout.js:1449-1464`).

`importToRepertoire` makes it worse by *not* using the debounce at all — it
calls the two checkers directly and then re-renders, which schedules a third
round:

```js
// frontend/scout.js:1680-1683
// Immediately update badges and active line overlap status
this.checkActiveLineOverlap();
this.checkTreeOverlap();
this.renderTreeLines();          // -> scheduleOverlapCheck() -> 4 more requests
```

## Root cause
Debounce without cancellation, and two independent entry points into the same
pair of expensive endpoints. The server side has no caching either
(`check_line_overlap` re-reads the **entire** `repertoire_moves` table on every
call, `backend/repertoire.py:925-934`).

## Exploit / escalation
1. **Request storm.** Playing a 30-ply line faster than 120 ms/ply issues up
   to 120 `check_line` + 120 `check_tree` requests, each of which does a full
   `SELECT * FROM repertoire_moves` plus a per-line recursive depth walk
   (`get_max_depth`, `backend/repertoire.py:1074-1091`). On a large repertoire
   this saturates the single-threaded FastAPI/uvicorn worker and the eval SSE
   stream (`/api/repertoire/eval_stream`, `scout.js:1930`) starves, so the eval
   bar freezes mid-update.
2. **Out-of-order strip writes.** The two responses are awaited with
   `Promise.all` and then `resW.json()` / `resB.json()` sequentially
   (`scout.js:1434-1435`), so the white response may already be rendered into
   the strip before the black one is even parsed. If the tree changed in
   between, the strip shows a white badge for line N and a black badge for
   line N of a *different* tree.
3. **Toggle storm on `#btnScoutRemoveFromRep`.** Its `display` is driven by the
   stale `relevantData` (`scout.js:1458-1464`), so the button can appear and
   disappear repeatedly as out-of-order responses land — a live §8.4 layout
   shift in the middle of a wrapping flex row (see CODE-14).
4. Adjacent weaker variant: `checkActiveLineOverlap` early-returns on the
   empty-active-line path (`scout.js:1400-1410`) **without** resetting
   `btnScoutRemoveFromRep`, so pressing "⏮ Start" leaves the previous line's
   Remove button visible while the strip reads "Repertoire: Starting Position".
