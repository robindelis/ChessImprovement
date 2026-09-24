# SCOUT-08 — Unrecognised input silently becomes a live YouTube search (and returns an unrelated video study)
- **Severity:** medium
- **Type:** wrong-result
- **File:** backend/scout.py
- **Line(s):** 170-193 (`analyze_source`), 186 (`elif "1." in source`)
- **Component:** `POST /api/scout/analyze`
- **Repro script:** `audit/scripts/atk4_08_pgn_robustness_repro.py` (yt_dlp faked, fallback hit tagged `ZZMARKERZZ`)
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
Input that is not a YouTube id / Lichess study / ECO guide / PGN should be rejected with a
4xx. It must never be reinterpreted as a *search query* and never cause an outbound crawl.

## Actual
The source dispatcher uses `elif "1." in source:` as the "is this a PGN?" test, and the final
`else` performs a **live YouTube search** and analyses the top hit:

```python
elif "1." in source:
    return self._analyze_raw_pgn(source)
else:
    # Try searching YouTube for the string directly as a video query
    search_res = self.search_online_content(source, limit=1)
    if search_res and search_res[0].get('source_type') == 'youtube':
        return self._analyze_youtube_video(search_res[0]['id'])
    raise ValueError(...)
```

Observed (fake `yt_dlp` whose single search result is tagged `ZZMARKERZZ`):

```
  empty string                   0B HTTP200 vars=0 tl=0 title='ZZMARKERZZ fallback video' <<< FELL THROUGH TO YOUTUBE SEARCH
  headers only                  24B HTTP200 vars=0 tl=0 title='ZZMARKERZZ fallback video' <<< FELL THROUGH TO YOUTUBE SEARCH
  not pgn at all                50B HTTP200 vars=0 tl=0 title='ZZMARKERZZ fallback video' <<< FELL THROUGH TO YOUTUBE SEARCH
  json blob                     23B HTTP200 vars=0 tl=0 title='ZZMARKERZZ fallback video' <<< FELL THROUGH TO YOUTUBE SEARCH
  moves without numbers        282B HTTP200 vars=0 tl=0 title='ZZMARKERZZ fallback video' <<< FELL THROUGH TO YOUTUBE SEARCH
  setup fen midgame             96B HTTP200 vars=0 tl=0 title='ZZMARKERZZ fallback video' <<< FELL THROUGH TO YOUTUBE SEARCH
  repeated headers only      240000B HTTP200 vars=0 tl=0 title='ZZMARKERZZ fallback video' <<< FELL THROUGH TO YOUTUBE SEARCH
  nul-heavy                  100000B HTTP200 vars=0 tl=0 title='ZZMARKERZZ fallback video' <<< FELL THROUGH TO YOUTUBE SEARCH
```

8 of 23 hostile inputs are silently answered with **a completely unrelated video study** and
HTTP 200. `ValueError` is only raised when the search itself comes back empty.

**Disclosure:** the first run of this probe did not install the fake and therefore executed
real `ytsearch1:` lookups for a handful of these inputs; the observed titles were real
YouTube videos ("5 BEST Chess Openings for 2026 (for White)", "Learn to CRUSH with 1. e4 |
10-Minute Ch…"), taking 2.7–3.0 s each. That accidental run is itself the proof that the path
performs a live crawl; the committed repro now mocks `yt_dlp` and no crawl occurs.

## Root cause
A content-type sniff (`"1." in source`) instead of proper detection, plus a "helpful" search
fallback that converts *any* unrecognised string into an outbound network crawl.

## Exploit / escalation
1. **Amplification / abuse:** `POST /api/scout/analyze` with `{"source": "<any text>"}` forces
   the server to run a `ytsearch` crawl and then a full `extract_info` on the winning video.
   There is no rate limit, no timeout (`ydl_opts` in both `search_online_content` and
   `_analyze_youtube_video` sets only `quiet/skip_download/no_warnings` — no
   `socket_timeout`), so a handful of concurrent requests can pin worker threads on outbound
   sockets indefinitely (see SCOUT-10).
2. **A FEN-starting PGN can never be imported** (`[SetUp "1"] [FEN …] 4. O-O *` has no
   literal `1.`), so a legitimate and common PGN flavour is unreachable.
3. **A move list without move numbers** (`e4 e5 Nf3 Nc6 …`) — the format a video description
   or a book uses — is treated as a search query.
4. 240 KB of headers or 100 KB of NULs is enough to reach the fallback; there is no size cap
   on `ScoutAnalyzePayload.source` (see SCOUT-10).
