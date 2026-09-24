# SCOUT-06 — The whole YouTube scouting pipeline is dead code: analysis always returns an empty, permanently cached timeline
- **Severity:** high
- **Type:** spec-violation
- **File:** backend/scout.py
- **Line(s):** 12-13 (imports), 195-276 (`_analyze_youtube_video`), 278-487 (uncalled extractor), 170-193 (`analyze_source`)
- **Component:** Opening Scout — video analysis
- **Repro script:** `audit/scripts/atk4_06_dead_pipeline_repro.py`
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
`AGENTS.md` §4E: *"Online Scouting: Searches YouTube videos … Embedded Video & Chapters …
Interactive Move Playing"* — a scouted video is supposed to yield extracted moves, a timeline
and a variation list, which the user can then import into the repertoire.

## Actual
`_analyze_youtube_video` builds its payload with **hard-coded empty collections** and returns:

```python
data = {
    ...
    'chapters': chapters,
    'variations': [],            # <-- never populated
    'full_timeline': [],         # <-- never populated
    'transcript_snippets': [],   # <-- never populated
```

The function that is supposed to fill them, `_extract_chess_moves_and_lines`, has **no call
site anywhere in the repository**:

```
$ grep -rn _extract_chess_moves_and_lines --include=*.py --include=*.js .
backend/scout.py:278:    def _extract_chess_moves_and_lines(      <-- only the definition
audit/scripts/...                                            <-- this audit's probes
```

`description = info.get('description', '')` is read at line 212 and then never used, and
`from youtube_transcript_api import YouTubeTranscriptApi` (line 13) is imported and never
called — so no subtitles are ever fetched.

Observed (`yt_dlp` faked, the fake video description contains a complete PGN line):

```
analyze #1 -> 200
   variations       : []
   full_timeline    : []
   transcript_snips : []
   chapters         : 2 -> ['Najdorf introduction', 'Poisoned pawn']
```

The empty payload is then written to `scouted_content` (line 261) and `analyze_source` serves
it from cache for ever:

```
analyze #2 (same id, force_refresh NOT set) -> variations: []   (served from cache)
analyze #3 (force_refresh=True)              -> variations: []
frontend analyzeSource() sends force_refresh?  body: JSON.stringify({ source: source })
```

The frontend never sets `force_refresh`, so once a video has been analysed there is **no way
from the UI** to re-analyse it; the stale payload (including a possibly wrong ECO, see
SCOUT-07) is permanent.

Net effect in the UI: `openStudio()` seeds `this.lineTree` from `study.variations`, which is
empty, so `renderTreeLines()` shows the "No lines played yet" placeholder and the user must
re-type every move by hand — the advertised Scout → repertoire workflow is inoperative for
videos, while the raw-PGN and ECO-guide paths still work (10 moves from a direct PGN in the
same run).

## Root cause
The transcript/description extraction stage was removed from `_analyze_youtube_video` but the
empty fields, the docstring (`"""Downloads metadata and captions … parsing moves, timeline,
and variations."""`) and the `AGENTS.md` documentation were left behind. Nothing asserts that
`variations` is non-empty before caching.

## Exploit / escalation
1. Cache poisoning is trivial and permanent: any `POST /api/scout/analyze` with a YouTube id
   writes a row that is served verbatim afterwards, with no TTL and no invalidation path from
   the UI. A row written by an older/buggy build is never refreshed.
2. The dead `description` variable is attacker-controlled YouTube text that *would* have fed
   `_extract_lines_from_description`; when that is wired back up it becomes a second
   uncontrolled input (see SCOUT-05).
3. `data_json` is stored and re-served without any schema validation on read
   (`db.get_scouted_content` → `json.loads` → `analyze_source` returns `cached['data']`
   verbatim), so any structurally-wrong cached payload flows straight into the DOM.
