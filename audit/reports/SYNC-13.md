# SYNC-13 — stored XSS: untrusted game fields from the Lichess stream are interpolated into `innerHTML` in the games list
- **Severity:** medium
- **Type:** security
- **File:** frontend/repertoire_builder.js
- **Line(s):** 992-1015 (game card), 1153 and 1499 (review banner)
- **Component:** recent-games UI
- **Repro script:** `audit/scripts/atk3_ndjson_hostile_repro.py` (STEP F), `audit/scripts/atk3_reverify_matrix.py` (section `SYNC-13`)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
Any value that originates outside the app is inserted with `textContent` (or escaped) before
being written into an HTML string.

## Actual
`/api/games/recent` returns the raw database strings, and the games list builds the whole
card with one `innerHTML` template:

```js
// frontend/repertoire_builder.js:992-1015
item.innerHTML = `
    ...
    ${colorDot} vs ${g.opponent_name}
    ...
    <span ...>${g.speed} • ${g.played_at_formatted}</span>
    ...
    ${g.eco ? `<strong ...>${g.eco}</strong> • ` : ''}${g.opening_name || 'Standard Opening'}
    ...
    <a href="${g.game_url}" target="_blank" rel="noopener noreferrer" ...>↗</a>
`;
```
Same pattern at `:1153` (`${colorDot} vs ${g.opponent_name}`) and `:1499`
(`Reviewing vs ${game.opponent_name}`).

The backend stores whatever the stream contained — `games.id`, `players.*.user.name` and
`opening.name` — with no validation at all:

```
  SYNC-13 untrusted game fields reach innerHTML
  API hands the frontend: {"id": "<img src=x onerror=alert(1)>",
                           "opponent_name": "<svg onload=alert(3)>",
                           "opening_name": "<img src=x onerror=alert(2)>",
                           "eco": "<b>x</b>",
                           "game_url": "https://lichess.org/<img src=x onerror=alert(1)>"}
  [LIVE ] SYNC-13
```

`g.game_url` is additionally injected into an `href`, so `javascript:`-style payloads are
possible there too (the `https://lichess.org/` prefix limits it, but `id` is fully
attacker-controlled in the data model).

The same class of sink exists for the explorer table (`repertoire_builder.js:2233`), where
`m.san`, `m.popularity_pct` and the win-rate percentages from the untrusted explorer payload
are interpolated into `tr.innerHTML`, including inside a `style="width:${...}%"` attribute.

## Root cause
No output encoding anywhere between the SQLite text columns and `innerHTML`. The API layer
is a pass-through (`/api/games/recent` builds `opponent_name`/`opening_name` straight from
the row) and the frontend uses string templates instead of DOM APIs.

## Exploit / escalation
1. **Reachable data sources:** (a) the Lichess NDJSON stream, (b) `POST /api/repertoire/move`
   with an arbitrary `study_name`/`comment`, (c) `POST /api/scout/*` payloads,
   (d) the explorer JSON. None of them is HTML-escaped on the way in or out.
2. **Chained with CORS:** `app.add_middleware(CORSMiddleware, allow_origins=["*"],
   allow_credentials=True)` means any web page the user visits can POST to
   `http://127.0.0.1:38281/api/repertoire/move` and then read back `/api/repertoire/tree`,
   so a *remote* page can plant a payload that executes in the desktop app.
3. **Amplified by the summary banner:** the review banner at `:1499` re-renders
   `opponent_name` on every board navigation, so a single poisoned row fires repeatedly.
4. **Fix direction:** build these fragments with `document.createElement` + `textContent`
   (as the code already does for `gamesList`/`tr` themselves), or at minimum add a single
   `esc()` helper and route every interpolation through it.
