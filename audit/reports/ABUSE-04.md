# ABUSE-04 — Stored XSS in the Scout results grid: an attacker-named YouTube video executes JS in the Electron renderer, which chains to unauthenticated data destruction
- **Severity:** high
- **Type:** security
- **File:** `frontend/scout.js`
- **Line(s):** 463-483 (sink), 416-424 (`loadRecent()` → `cachedResults`), 655 (`<iframe … src="https://www.youtube.com/embed/${videoId}">`); data path `backend/scout.py:207-271` and `backend/db.py:421-453`
- **Component:** frontend / Scout UI
- **Repro script:** `audit/scripts/atk5_xss_dom.html` (DOM proof, Chromium `--dump-dom`), `audit/scripts/atk5_xss_server.py` (data-path proof, isolated DB on port 39311)
- **Detected by:** ATK-5

## Expected
Any string that originates outside the app (a YouTube video title/uploader name,
a Lichess study title, a PGN `Event`/`White`/`Black` header) must be inserted with
`textContent` / a DOM node, or HTML-escaped, before it is concatenated into an
`innerHTML` template. `frontend/app.js:266-271` already does this correctly for
study names (`opt.textContent = …`), so the codebase already knows the pattern.

## Actual
`frontend/scout.js:463-483` interpolates raw, externally-supplied values into
`innerHTML`:

```js
        grid.innerHTML = filtered.map(item => {
            ...
            const thumb = item.thumbnail || '/assets/pieces/wN.svg';
            const ecoBadge = item.eco ? `<span class="badge-eco">${item.eco}</span>` : '';
            return `
                <div class="card-panel scout-card" data-source-id="${item.id}" data-source-type="${item.source_type}">
                    <div class="scout-card-thumb-wrap">
                        <img src="${thumb}" alt="${item.title}" class="scout-card-thumb" onerror="this.src='/assets/pieces/wN.svg'">
                        ...
                <div class="scout-card-title" title="${item.title}">${item.title}</div>
                <div class="scout-card-channel">${item.channel || 'Chess Guide'}</div>
```

`item.title` / `item.channel` come straight from yt-dlp
(`backend/scout.py:129-133`: `'title': e.get('title') …`, `'channel': e.get('uploader') …`)
and are persisted verbatim by `save_scouted_content` (`backend/db.py:435-449`).
`loadRecent()` (`frontend/scout.js:416-424`) re-reads them from
`GET /api/scout/recent` into the same `cachedResults` array that `renderResults()`
renders — so merely clicking the **"Recent History"** filter pill
(`frontend/index.html:602`) is enough.

### Verified data path
`audit/scripts/atk5_xss_server.py` stores a poisoned row in the isolated temp DB
and serves the real app on `127.0.0.1:39311`:

```
$ curl -s "http://127.0.0.1:39311/api/scout/recent?limit=50" | …
FOUND: {"id": 858, "source_type": "youtube", "source_id": "atk5xss1",
        "title": "<img src=x onerror=\"document.title='XSS-EXECUTED';document.body.setAttribute('data-pwned','1');fetch('/api/repertoire/clear',{method:'POST'})\">",
        "channel": "Totally Legit Chess Channel", …}
```

### Verified execution (real Chromium, verbatim template from `scout.js`)
`audit/scripts/atk5_xss_dom.html` runs the byte-for-byte `renderResults()`
template with that payload; `chrome --headless --dump-dom` output:

```html
<!DOCTYPE html>
<html data-pwned="1"><head><title>XSS-EXECUTED</title></head>
<body>
<div id="scoutResultsGrid">
        <div class="card-panel scout-card" data-source-id="atk5xss1" …>
                    <img src="x" onerror="document.title='XSS-EXECUTED';document.documentElement.setAttribute('data-pwned','1');fetch('/api/repertoire/clear',{method:'POST'})"></div>
```

`data-pwned="1"` on `<html>` and the changed `<title>` prove the injected
`onerror` handler **executed arbitrary JavaScript**, and the payload's
`fetch('/api/repertoire/clear', {method:'POST'})` is exactly the unauthenticated
destructive call from ABUSE-03.

## Root cause
Server-side values that transit untrusted third parties (YouTube metadata,
Lichess study titles, PGN headers) are treated as trusted markup by the renderer.
`backend/scout.py` stores them raw, `GET /api/scout/recent` returns them raw
(JSON encoding is not the problem), and `frontend/scout.js` concatenates them into
an HTML string. There is no sanitiser anywhere in the stack and no CSP (see
ABUSE-14), so an injected `<img onerror>`/`<svg onload>` executes with full
renderer privileges.

## Exploit / escalation
1. **Drive-by, no account needed.** Anyone can publish a YouTube video titled
   `<img src=x onerror="…">`. When the victim clicks **Scout → Recent History**
   (or searches for an opening, which also renders `item.title` /
   `item.channel`), the payload runs with `window.desktopApp` (the Electron
   `contextBridge` from `electron/preload.js`) available.
2. **Full compromise of the local app, because nothing is authenticated.**
   The payload can `fetch('/api/repertoire/clear', {method:'POST'})`,
   `fetch('/api/repertoire/seed?force=true', {method:'POST'})`,
   `POST /api/settings` (rewrite the Lichess username/token), or read
   `/api/games/recent` and exfiltrate to an external host — no CSRF token, no
   auth (ABUSE-03).
3. **Adjacent sink, same data class:** `frontend/repertoire_builder.js:973`,
   `:992`, `:1013`, `:1366` and `:2233` build the game list with
   `gamesList.innerHTML = … ${g.opening_name} … ${g.game_url} …` where
   `opening_name` / `game_url` come from the Lichess API and the `games` table —
   the same unescaped-interpolation pattern with a different data source. A
   Lichess study title containing HTML reaches
   `_analyze_raw_pgn` → `save_scouted_content(title=main_name)` → the same grid.

**Required fix:** build these cards with `document.createElement` +
`textContent` (as `app.js` already does for studies) or escape every
interpolation (`esc = s => String(s).replace(/[&<>"']/g, …)`), and add a strict
CSP as defence in depth.
