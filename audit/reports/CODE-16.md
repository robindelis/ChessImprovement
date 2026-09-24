# CODE-16 — All three Scout filter buttons are no-ops (`youtube`/`eco_guide`/`recent` match no tested literal), and `loadRecent()` is unreachable
- **Severity:** medium
- **Type:** wrong-result
- **File:** frontend/scout.js
- **Line(s):** 238-245 (filter binding), 416-437 (`loadRecent`), 439-461 (`renderResults`)
- **Component:** OpeningScout results filters
- **Repro script:** `audit/scripts/CODE4_filter_repro.js`
- **Detected by:** CODE-4

## Expected
Each shelf filter button must filter the grid, and the `data-filter` value in
`index.html` must match the branch tested in `renderResults()`.

## Actual
The HTML declares four filters:

```html
<!-- index.html:599-602 -->
<button type="button" class="scout-filter-btn active" data-filter="all">All Content</button>
<button type="button" class="scout-filter-btn" data-filter="youtube">Videos</button>
<button type="button" class="scout-filter-btn" data-filter="eco_guide">Theory &amp; Studies</button>
<button type="button" class="scout-filter-btn" data-filter="recent">Recent History</button>
```

The click handler stores the raw `data-filter` value verbatim:

```js
// frontend/scout.js:238-245
document.querySelectorAll('.scout-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.scout-filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.activeFilter = btn.dataset.filter;
        this.renderResults();
    });
});
```

but `renderResults()` tests three *different* literals — none of which is a
`data-filter` value in the HTML:

```js
// frontend/scout.js:443-450
let filtered = this.cachedResults;
if (this.activeFilter === 'videos') {                                 // HTML: "youtube"
    filtered = filtered.filter(r => r.source_type === 'youtube');
} else if (this.activeFilter === 'guides') {                          // HTML: "eco_guide"
    filtered = filtered.filter(r => r.source_type === 'eco_guide' || r.source_type === 'lichess_study');
} else if (this.activeFilter === 'favorites') {                       // no such button
    filtered = filtered.filter(r => r.is_favorite);
}
// no branch for "recent"
```

**Every** filter except "All Content" is a no-op:

* `videos` ≠ `youtube` → **Videos** does not filter.
* `guides` ≠ `eco_guide` → **Theory & Studies** does not filter.
* `recent` has no branch → **Recent History** does not filter.

`loadRecent()` — the only code that would populate the recent history, and the
only consumer of `GET /api/scout/recent` (`backend/api.py:1053`) — is never
called:

```js
// frontend/scout.js:416-437
async loadRecent() {
    try {
        const res = await fetch('/api/scout/recent?limit=20');
        ...
    }
}
```

```
$ grep -n "loadRecent\b" frontend/scout.js
416:    async loadRecent() {          <-- definition only
```

## Root cause
The filter vocabulary drifted: `renderResults()` was written against
`'videos'`/`'guides'`/`'favorites'` while `index.html` was later edited to
`'youtube'`/`'eco_guide'`/`'recent'`. Because `activeFilter` is a free-form
string copied straight from `dataset`, the mismatch is silent — there is no
`default:` branch and no validation.

## Exploit / escalation
Reproduced by static extraction (`audit/scripts/CODE4_filter_repro.js`):

```
data-filter values in index.html : ["all","youtube","eco_guide","recent"]
literals tested in renderResults : ["videos","guides","favorites"]
  [ok]                "all" is handled
  [FAIL-REPRODUCED]   "youtube"  falls through to the unfiltered list
  [FAIL-REPRODUCED]   "eco_guide" falls through to the unfiltered list
  [FAIL-REPRODUCED]   "recent"   falls through to the unfiltered list
  [FAIL-REPRODUCED]   unreachable branches (no matching button): ["videos","guides","favorites"]
```

1. **User-visible lie.** Clicking "Videos" moves the `.active` highlight to the
   right pill but leaves the ECO-guide cards on screen. The user concludes the
   filter is broken or that there are no videos.
2. **Dead network contract.** `GET /api/scout/recent` (`backend/api.py:1053`)
   and `list_recent_scouted` are implemented and unit-tested but have **no
   production caller** — the "Recent History" tab can never be populated, so
   the "what did I already scout?" workflow does not exist.
3. **Unreachable favourites filter + orphaned favourite state.**
   `toggleFavorite()` (`scout.js:2028-2045`) and `loadRecent()`'s mapper
   (line 431) both write `is_favorite`, but no live filter reads it, so the
   `#btnScoutFav` bookmark has **no observable effect** anywhere in the UI.
4. Adjacent weaker variant: the `.active` highlight is not reset when
   `openStudio()` / `#btnBackToScoutSearch` (`scout.js:286-299`) toggles back to
   the results view, so the highlighted pill can disagree with the rendered
   content even after the literals are aligned.
