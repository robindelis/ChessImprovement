# SCOUT-10 — No timeouts on `yt_dlp`, and no size caps on any Scout request
- **Severity:** medium
- **Type:** crash
- **File:** backend/scout.py, backend/api.py
- **Line(s):** scout.py:106-112 and 197-201 (`ydl_opts`); api.py:92-110 (pydantic models)
- **Component:** `/api/scout/search`, `/api/scout/analyze`, `/api/scout/check_line`, `/api/scout/check_tree`
- **Repro script:** `audit/scripts/atk4_10_resource_limits_repro.py`
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
Any outbound call has a timeout; any request body has a size cap; a single request must not be
able to pin a worker for minutes.

## Actual

**(a) `yt_dlp` is invoked with no `socket_timeout`.**

```python
ydl_opts = {'quiet': True, 'skip_download': True, 'no_warnings': True}     # _analyze_youtube_video
ydl_opts = {'quiet': True, 'extract_flat': True, 'skip_download': True,
            'default_search': 'ytsearch', 'no_warnings': True}             # search_online_content
```

Observed with a fake that models a stalled upstream:

```
ydl_opts passed to YoutubeDL: {'quiet': True, 'skip_download': True, 'no_warnings': True}
request still blocked after 8s?  True  (thread never returned)
after releasing the fake stall: returned
```

The request thread is blocked for as long as YouTube stalls. These are **synchronous `def`
endpoints**, so they run on the threadpool; a handful of concurrent stalls exhausts it and the
whole API (builder, trainer, scout) stops responding.

**(b) No size cap on any Scout payload.** `ScoutAnalyzePayload.source: str`,
`ScoutImportPayload.moves: List[Dict[str, Any]]`, `ScoutCheckLinePayload.moves`,
`ScoutCheckTreePayload.lines` all have no `max_length` / `max_items`.

```
POST /api/scout/check_line  (50 000 identical moves, 3.4 MB body)    1.387s  -> 200, total_plies = 50000
POST /api/scout/check_tree  (20 lines x 5 000 moves = 100 000 slots)  2.407s  -> 200, 20 results
POST /api/scout/check_tree  (200 lines x 50 000 moves = 1e7 slots)  262.903s  -> 200
GET  /api/scout/search?query=<60 KB of text>                         0.010s  -> 200
```

A single unauthenticated request burned **4m23s of CPU** in the harness. The 60 KB query is
forwarded verbatim as `ytsearch30:AAAA…`.

**(c) Every overlap check re-reads the entire `repertoire_moves` table.**

```python
cursor.execute("SELECT parent_fen, fen, move_uci, move_san, is_user_move, opening_name, eco
               FROM repertoire_moves WHERE repertoire = ?", (repertoire,))
```

With 20 000 rows loaded into the temp DB:

```
repertoire rows now: 20000
POST /api/scout/check_line (1 ply)   0.049s  (x3, identical)
POST /api/scout/check_tree (1 line)  0.085s
```

The browser issues **2 × `check_line` + 1 × `check_tree` per board move** (120 ms debounce,
`scout.js:1385-1522`), and `check_tree` posts the *entire* session tree each time, so the cost
per move grows with the tree. No caching, no memoisation, no index use.

## Root cause
Missing `socket_timeout` in both `ydl_opts` dicts; unconstrained pydantic models; and an
un-indexed, un-cached full-table read inside the hot UI polling path.

## Exploit / escalation
1. `for _ in range(50): client.post("/api/scout/analyze", json={"source": "junk"})` — every
   request falls through to a live YouTube crawl (SCOUT-08) and, combined with (a), can be made
   to hang indefinitely.
2. A single ~35 MB `check_tree` body is a minutes-long CPU burn from one request.
3. `check_line` with 50 000 identical moves also inflates the *response* (`matched_sans` /
   `new_sans` lists) — response size is unbounded too.
4. Because `_evaluate_line_against_index` iterates `lines` with no cap, a 200-line session tree
   with 1000 plies each (reachable by playing 1000 moves) is re-posted on every move.
