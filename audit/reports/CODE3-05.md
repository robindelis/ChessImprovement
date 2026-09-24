# CODE3-05 — `/api/games/recent` list has no request sequencing: a slower earlier response overwrites a newer filtered one
- **Severity:** medium
- **Type:** race
- **File:** frontend/repertoire_builder.js
- **Line(s):** 948-1043 (`loadRecentGames`), 425-443 (filter pills), 448-458 (search debounce)
- **Component:** Repertoire Builder — Recent Games tab
- **Repro script:** `audit/scripts/CODE-05_recent_games_race.js`
- **Detected by:** CODE-3

## Expected
The games list and the games count always reflect the most recent user input (colour filter + search text).

## Actual
`loadRecentGames()` is fired from three independent places — the filter pills, the 250 ms search debounce, and
`switchSidebarTab('games')` / `renderCoverageAndGaps()` — and there is **no `AbortController`, no request token and no
`res.ok` check** anywhere in the function:

```js
// repertoire_builder.js:961
try {
    const res = await fetch(url);
    const data = await res.json();
    const games = data.games || [];
    this.recentGames = games;              // shared mutable slot
    if (gamesCount) { gamesCount.textContent = data.total !== undefined ? data.total : games.length; }
    gamesList.innerHTML = '';
    games.forEach(g => { ... gamesList.appendChild(item); });   // unconditional DOM write
} catch (err) { ... }
```

Concrete repro: type `a`, pause 250 ms (request R1 goes out with `search=a`), then quickly type `b` and pause
(R2 with `search=ab`). If R1 is slower (it is, on any cold `/api/games/recent` query that has to re-derive opening
names via `OpeningBook.identify_game_opening` per row, `backend/api.py:355-368`), R1 resolves last and:

* the list shows the results for `a` while the input box reads `ab`;
* `this.recentGames` holds the stale array, so `isCurrentReview` highlighting (`line 984`) and any subsequent
  `reviewGame(g)` act on the wrong game;
* nothing ties the *response* to the query it was issued for.

A second, deterministic instance of the same class of bug:

```js
// repertoire_builder.js:936  (inside renderCoverageAndGaps, runs on every refreshTree)
fetch('/api/games/recent?limit=1')
    .then(r => r.json())
    .then(data => {
        const gamesCount = document.getElementById('repGamesCount');
        if (gamesCount && data.total !== undefined) { gamesCount.textContent = data.total; }   // UNFILTERED total
    })
    .catch(() => {});
```

`refreshTree()` runs on study change, threshold change, every import, and every tab switch back to the builder
(`app.js:63`). So with the "White" filter active, the tab badge can silently snap back to the all-colours game count
while the list below it still shows only White games. The `.catch(() => {})` also swallows the non-`ok` case: a 500
body leaves the count at whatever it was, with no user-visible signal.

## Root cause
Module-level mutable state (`this.recentGames`, `this.gamesColorFilter`, `this.gamesSearchQuery`) is written from
un-sequenced async callbacks, and the fetch is not abortable — unlike `loadCandidateMoves()` (2096-2099) which at
least aborts the previous request.

## Exploit / escalation
1. Switch to the "Recent Games" tab and immediately type in the search box: `switchSidebarTab` → `loadRecentGames()`
   and the debounced call both in flight; the first to resolve wins, and `gamesList.innerHTML = ''` + re-append from a
   stale `games` array flickers the whole list (visible repaint of up to 40 cards).
2. Because `res.json()` is unguarded, a non-JSON 500 body throws inside the `try`, replacing the entire list with
   "Failed to load games." even though the app is otherwise healthy.
3. Same missing-`res.ok` pattern in `refreshTree()` (899-904): a 500 leaves `this.treeData` at its previous value, so
   `renderCandidateTable` marks the wrong moves as "★ Your Move" / "✓ In Tree" with only a `console.error`
   (see CODE3-22).
