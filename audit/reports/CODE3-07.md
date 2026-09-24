# CODE3-07 — Cross-colour candidate-table contamination: the staleness guard compares only FEN, so the other colour's candidates can be rendered
- **Severity:** medium
- **Type:** race
- **File:** frontend/repertoire_builder.js
- **Line(s):** 840-854 (`switchColor`), 856-895 (`loadRepertoire`), 2083-2127 (`loadCandidateMoves`)
- **Component:** Repertoire Builder — 2000+ explorer table
- **Repro script:** `audit/scripts/CODE-07_colour_race.js`
- **Detected by:** CODE-3

## Expected
After switching White ⇄ Black, the 2000+ table shows candidates for the new colour's repertoire and the "★ Your Move"
/ "✓ In Tree" badges are computed against the new colour's tree.

## Actual
`loadCandidateMoves()` fetches with `&color=${this.activeColor}` and caches under `${this.activeColor}_${normFen}`,
but its staleness guard only compares the FEN:

```js
// repertoire_builder.js:2110
const curr = this.history[this.currentStepIndex];
if (!curr || curr.fen !== targetFen) { return; }
this.currentCandidateData = data;
...
this.renderCandidateTable(fen, isUserTurn);
```

Neither `loadRepertoire()` (856) nor `switchColor()` (840) aborts `this.candidateAbortController`:

```js
// repertoire_builder.js:866  (loadRepertoire)
if (this.board) this.board.clearArrows();
this.board.setOrientation(color);
...
this.history = [{ ply: 0, fen: startFen, san: 'Start', ... }];   // <-- back to the START FEN
```

So: be on White, sit on the start position (ply 0) so a `/api/repertoire/explore` request is in flight, then click
"Black Repertoire". `loadRepertoire('black')` resets `history` to the start FEN and issues its own request. The White
request now passes the guard (`curr.fen === targetFen`, both the start FEN), sets
`this.currentCandidateData = <White data>` and renders it — with the White repertoire's badges, because
`renderCandidateTable` reads `this.treeData`, which is mid-refresh. Depending on which of the two responses lands
last, the user sees the Black tab populated with White-colour data (or vice versa), including a "★ Your Move" star on
a move that is not the Black user move.

The same hole applies to `this.history[this.currentStepIndex] === 0` in general: any two positions with the same FEN
pass the guard, and the start FEN is re-visited on every colour switch, tab switch (`app.js:63`) and
`loadTreeLine()` (2960).

## Root cause
The request identity used for the guard is a *position* identity, not a *request* identity. There is no
`AbortController.abort()` in `loadRepertoire`, and no comparison of the colour (or a generation counter) captured at
request time against the colour at write time.

## Exploit / escalation
1. Rapidly alternate "White Repertoire" / "Black Repertoire" while the explorer is loading → the table can end up
   showing the previous colour's candidate list, and clicking "+ Set as My Move" on it then POSTs
   `repertoire: 'black'` with a move chosen from White data (the write is correct, the *choice* is not).
2. `updateSortHeaderUI()` (483-506) has the same exposure via `this.currentCandidateData?.user_games_total`, so the
   "My Stats (N Games)" header can also show the other colour's total.
3. `stepForward()`'s cache probe (1938-1939) mixes the two key schemes:
   `this.candidateCache.get(cacheKey) || this.candidateCache.get(normParent)` — the second key is never written
   (only `${color}_${normFen}` is, at line 2106), so that fallback is dead (see CODE3-17).
