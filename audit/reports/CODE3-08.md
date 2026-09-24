# CODE3-08 — `evalCache` grows without bound, is keyed on the full FEN (transpositions never hit), and `renderEvalBar`'s `depth` parameter is never used
- **Severity:** medium
- **Type:** leak
- **File:** frontend/repertoire_builder.js
- **Line(s):** 185-190, 1699-1729 (`renderEvalBar`), 1738-1749, 1778-1781
- **Component:** Repertoire Builder — Stockfish eval bar
- **Repro script:** `audit/scripts/CODE-08_eval_cache.js`
- **Detected by:** CODE-3

## Expected
The per-position evaluation cache is bounded (or evicted) and hits for transposed positions.

## Actual
The cache is a plain `Map` that is only ever added to, keyed on the **full** FEN string including the halfmove clock
and fullmove number:

```js
// repertoire_builder.js:186
this.evalCache = new Map();
...
// repertoire_builder.js:1780
if (data.score_cp !== undefined) {
    this.renderEvalBar(data.score_cp, data.mate, data.depth);
    this.evalCache.set(fen, data);          // fen is the full 6-field FEN
}
```

The full FEN is used as the request parameter too (`/api/repertoire/eval_stream?fen=...`, line 1756), so
transpositions of the same position reached via a different move order (extremely common when exploring variations)
never hit the cache and never reach the engine's cache either. Meanwhile every distinct fullmove counter creates a new
entry. A long exploration session produces hundreds of entries; nothing evicts them and the only `clear()` in the class
is `this.candidateCache.clear()` (line 756) for a different map.

Secondary defect in the same cluster: `depth` is accepted and threaded through three call sites but never read:

```js
// repertoire_builder.js:1699
renderEvalBar(scoreCp, mate, depth) {          // `depth` unused in the entire body
```

so the `cached.depth >= 14` early-return at line 1742 is the only consumer of depth anywhere, and the user never sees
the search depth even though the backend streams it on every iteration.

## Root cause
Unbounded `Map` used as a cache with no eviction policy, keyed on a non-canonical position identity (the FEN's
halfmove/fullmove fields are irrelevant to the evaluation) and never normalised via `normalizeFen()` even though the
class already has that helper (1425-1427).

## Exploit / escalation
1. Long exploration session → monotonically growing heap; each entry also keeps the whole SSE payload object alive
   (score_cp, mate, depth, plus whatever else the backend attaches).
2. Because transpositions miss, every transposition costs a fresh depth-16 Stockfish run — repeated work that the
   cache was supposed to eliminate.
3. `startEvalStream()` is never aborted when the user leaves the Repertoire tab (`app.js:59-64` calls
   `loadRepertoire()` only for the 'repertoire' tab), so a hidden board keeps an engine stream and keeps writing
   `#repEvalBarFill` / `#repEvalBarLabel` for a board nobody is looking at.
