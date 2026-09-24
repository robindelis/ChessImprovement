# CODE-05 — No request-generation guard: stale `search()` / `loadMasteryProgress()` responses overwrite newer ones
- **Severity:** high
- **Type:** race
- **File:** frontend/scout.js (395-414), frontend/repertoire_trainer.js (212-338, 370-513)
- **Component:** OpeningScout.search / RepertoireTrainer.loadMasteryProgress + loadOpeningsList
- **Repro script:** `audit/scripts/CODE4_race_repro.js`
- **Detected by:** CODE-4

## Expected
Any async read that repaints shared UI must either be cancelled
(`AbortController`) or guarded by a monotonically increasing generation
counter, so a slow earlier response can never overwrite a fast later one.

## Actual
`search()` is a bare fetch → assign → render with no sequencing:

```js
// frontend/scout.js:395-414
async search(query) {
    const loading = document.getElementById('scoutLoadingIndicator');
    const grid = document.getElementById('scoutResultsGrid');
    if (loading) loading.style.display = 'flex';
    if (grid) grid.innerHTML = '';

    try {
        const res = await fetch(`/api/scout/search?query=${encodeURIComponent(query)}&limit=12`);
        const data = await res.json();
        this.cachedResults = data || [];
        this.renderResults();
    } catch (e) { ... }
    finally { if (loading) loading.style.display = 'none'; }
}
```

It is invoked from a `<form>` submit (line 213), from every chip click
(lines 227/232) and once at construction (line 192,
`this.search('Caro-Kann Defense')`). A `/api/scout/search` call performs a live
`yt-dlp` web search server-side (`backend/scout.py:95` `search_online_content`),
so latencies differ by seconds.

The trainer has the same shape, and additionally issues the **same URL twice in
parallel** on every drill start:

```js
// frontend/repertoire_trainer.js:380-383
await Promise.all([
    this.loadOpeningsList(),      // itself fetches /api/repertoire/train/progress
    this.loadMasteryProgress()    // fetches /api/repertoire/train/progress AGAIN
]);
```

```js
// frontend/repertoire_trainer.js:271-274
const [openingsRes, progressRes] = await Promise.all([
    fetch(`/api/repertoire/train/openings?color=${this.activeColor}`),
    fetch(`/api/repertoire/train/progress?color=${this.activeColor}`)
]);
```

Both then write to the same `trainOpeningFilter` <select> and the same mastery
DOM, with `activeColor` captured as a local at call time but no check that
`this.activeColor` is unchanged when the response lands.

## Root cause
No `AbortController`, no `this.searchSeq++` / `this.progressSeq++` guard, and
no `if (colour !== this.activeColor) return;` re-validation. The module-level
`this.cachedResults` and `this.activeColor` are mutated from async callbacks
that were started under a different logical context.

## Exploit / escalation
1. **Wrong results, normal flow.** Page load fires `search('Caro-Kann
   Defense')` (yt-dlp, slow). The user immediately types "Sicilian Najdorf" and
   submits. If the *first* response lands second, `this.cachedResults` holds
   Caro-Kann results while `#scoutSearchInput` reads "Sicilian Najdorf". Every
   subsequent filter click (`renderResults`) re-renders the wrong set, and
   `showTreeTab`/import operate on lines from a different opening with no
   visible indication.
2. **Trainer colour bleed.** `switchColor('white' → 'black')`
   (`repertoire_trainer.js:141-155`) calls `restartSession` + `startNewDrill`.
   If the in-flight *white* `/api/repertoire/train/progress` resolves after the
   *black* one, `loadMasteryProgress` paints White's `mastered_pct` into the
   Black view (`trainMasteredHeadline`, the three segment widths and all nine
   legend counters) while the drill below it is Black — a fully contradictory
   panel.
3. **Load amplification.** `startNewDrill()` is called from
   `showTab('rep-trainer')` (`app.js:69`), from `switchColor`, from the filter
   `change` handler, from the mode `change` handler, from `setOpeningFocus` and
   from the 150/200 ms `autoNextTimeout` after every completed line. Each call
   fires 3 requests (openings + 2× progress) with no cancellation, so a
   fast-moving session leaves a growing tail of in-flight responses all
   competing to paint the same elements.
4. Adjacent weaker variant: `analyzeSource()` (`scout.js:499-523`) has the same
   defect — two quick card clicks produce two `POST /api/scout/analyze`
   responses and the *slower* one wins the `openStudio()` call, so the studio
   shows video A while the user clicked video B.
