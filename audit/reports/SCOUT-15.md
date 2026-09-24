# SCOUT-15 — Hostile yt-dlp chapter/metadata shapes crash `/api/scout/analyze` with HTTP 500
- **Severity:** medium
- **Type:** crash
- **File:** backend/scout.py
- **Line(s):** 209 (`duration`), 212-222 (chapter loop), 207-208 (`title`/`channel`), 261-271 (`save_scouted_content`)
- **Component:** `_analyze_youtube_video`
- **Repro script:** `audit/scripts/atk4_15_metadata_crash_repro.py` (yt_dlp faked; no network)
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
Metadata coming from a third-party extractor must be validated/coerced. A malformed chapter
list should degrade to "no chapters", not take the endpoint down.

## Actual
The chapter loop calls `float()` on raw values with no `try`, and `title`/`channel`/`duration`
are passed straight to `sqlite3` binding:

```python
duration = int(info.get('duration') or 0)                       # line 209
chapters.append({
    'title': ch.get('title', 'Chapter'),
    'start_time': float(ch.get('start_time', 0)),              # line 219  <-- no coercion guard
    'end_time':   float(ch.get('end_time', duration)),          # line 220
    'start_formatted': format_seconds(float(ch.get('start_time', 0)))
})
```

17 hostile metadata shapes, all through `POST /api/scout/analyze`:

```
  chapter start_time = None        HTTP500  The opening scout could not complete the request.
  chapter end_time = None          HTTP500  The opening scout could not complete the request.
  chapter start_time = string      HTTP400  could not convert string to float: '3:45'
  chapter start_time = nan         HTTP400  cannot convert float NaN to integer
  chapter start_time = inf         HTTP500
  chapter start_time = bool        HTTP200  first_start='00:01'          (True -> 1.0)
  chapters is a string             HTTP500
  chapters is a dict               HTTP500
  chapter is a string              HTTP500
  5000 chapters                    HTTP200  chapters=5000               (no cap)
  duration is a float string       HTTP400  invalid literal for int() with base 10: '600.5'
  duration is a list               HTTP500
  duration is 10**12               HTTP200  fmt='277777777:46:40'       (absurd UI string)
  title is None                    HTTP200
  title is a list                  HTTP500
  uploader is a dict               HTTP500  (sqlite3.ProgrammingError: type 'dict' is not supported)
```

**9 of 17 shapes return HTTP 500.** The `uploader is a dict` traceback is the clearest:

```
sqlite3.ProgrammingError: Error binding parameter 4: type 'dict' is not supported
  backend/scout.py:261 in _analyze_youtube_video -> save_scouted_content(...)
```

Because the exception is raised *before* `save_scouted_content`, nothing is cached, so every
retry re-crashes — there is no negative caching and no degradation path.

Frontend effect: `analyzeSource()` (scout.js:517-520) throws and shows
`alert("Could not open source: The opening scout could not complete the request.")`; the studio
never opens and the loading spinner is cleared with no clue why.

## Root cause
No schema for third-party metadata; `float()`/`int()` are called on whatever the extractor
returned, and `channel`/`title` are never coerced to `str` before the SQLite bind.

## Exploit / escalation
1. Any YouTube upload whose chapter metadata is malformed (yt-dlp leaves `end_time` unset for
   the last chapter in several extractors, and `None` values appear for private/member-only
   videos) bricks the Scout for that video with no fallback.
2. `chapters` with 5000 entries is accepted and becomes 5000 `<option>` elements plus 5000
   `search_openings()` scans over the 3815-entry encyclopedia on every analysis.
3. `duration` values that survive produce nonsense `duration_formatted` strings
   (`277777777:46:40`) that are rendered in the studio header.
4. Because `_analyze_youtube_video` is also reached from `analyze_source`'s search fallback
   (SCOUT-08), a hostile video title is enough to route arbitrary user input into this crash
   path.
