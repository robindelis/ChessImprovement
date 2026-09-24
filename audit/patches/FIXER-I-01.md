# FIXER-I-01 — Scout / opening-encyclopedia / Lichess-client fixes

**Owner:** FIXER-I · **Files owned (exclusive):** `backend/scout.py`, `backend/openings.py`,
`backend/lichess_client.py`
**New test file:** `tests/test_scout_pipeline.py` (41 tests)
**Findings fixed:** SCOUT-04, SCOUT-05, SCOUT-06, SCOUT-07, SCOUT-08, SCOUT-09, SCOUT-10,
SCOUT-11, SCOUT-15, ABUSE-14, and the ABUSE-01 scope items.
**Not fixed here (hand-off only):** SCOUT-02, SCOUT-03 (both live in `repertoire.py`),
plus route-level caps and the frontend import-status contract.

No real network call, no real `yt-dlp`, no real Stockfish run was performed in any test or
probe: `backend.scout.yt_dlp`, `backend.scout.YouTubeTranscriptApi` and
`backend.lichess_client.requests` are replaced by fakes. `chess_improvement.db` was never
opened; every probe begins with `import tests.test_setup`. Port 38281 was never contacted.
All Python was run with the base conda interpreter `/home/robin/miniconda3/bin/python3`.

**Complete diffs** (for review alongside the per-finding excerpts below):
`audit/patches/FIXER-I-01_scout.diff` (1469 lines),
`audit/patches/FIXER-I-01_openings_lichess.diff` (411 lines).
**Probes:** `audit/scripts/fixeri_07_openings_repro.py`,
`audit/scripts/fixeri_perf_repro.py`, `audit/scripts/fixeri_scout02_03_repro.py`,
`audit/scripts/fixeri_check_order.py`.

---

## 1. Summary of changes

| Finding | File | Lines (new) | Change |
| :--- | :--- | :--- | :--- |
| SCOUT-06 | `scout.py` | 268-353, 355-440, 470-506 | Pipeline wired up; cache-validity rule |
| SCOUT-04 | `scout.py` | 141-186, 549-789 | Order-preserving, legality-verified extraction |
| SCOUT-05 | `scout.py` | 549-756, 442-468 | Dedup, timestamp clamp, chapter sanitising, caps |
| SCOUT-07 | `openings.py` + `scout.py` | `openings.py` 59-192, 336-341; `scout.py` 393-406, 553-560 | Scored, word-boundary, most-specific-wins matching |
| SCOUT-08 | `scout.py` | 268-353 | Explicit source validation, search fallback removed |
| SCOUT-09 | `scout.py` | 989-1170 | Multi-game split, real study names, per-game chapters |
| SCOUT-10 | `scout.py`, `lichess_client.py` | 24-58, 190-266, 891-918, 1178-1215 | `socket_timeout`, payload caps, ply caps |
| SCOUT-11 | `scout.py` | 1217-1379 | Truthful import response, no redundant writes |
| SCOUT-15 | `scout.py` | 60-113, 355-440, 442-468 | Every extractor field coerced/validated |
| ABUSE-14 | `scout.py` | 791-827 | Linear description pattern + length cap |
| ABUSE-01 scope | `lichess_client.py` | whole file | Token redaction, timeouts, bounded NDJSON, error signal |

---

## 2. Per-finding root cause, change and evidence

### SCOUT-06 (HIGH) — dead YouTube pipeline that cached its own failure forever

**Root cause.** The extraction stage was removed from `_analyze_youtube_video` but its
outputs were left as hard-coded empty lists, and the empty payload was written to
`scouted_content` before returning. `analyze_source` accepted *any* truthy cached row, so one
transient failure permanently disabled the Scout for that video, and the frontend never sends
`force_refresh`.

**Change 1 — `scout.py:470-506` new `_fetch_transcript`.** `YouTubeTranscriptApi` was imported
and never called. It is now called (modern `YouTubeTranscriptApi().get_transcript()`, with a
fallback to the legacy static method), returning `(snippets, error)`; a missing caption track
degrades to `transcript_error` in the payload instead of aborting the analysis.

**Change 2 — `scout.py:416-434` extraction is wired in and caching is conditional.**

```python
transcript_snippets, transcript_error = self._fetch_transcript(video_id)
timeline, variations = self._extract_chess_moves_and_lines(
    transcript_snippets, description, chapters, title, duration
)
...
# Cache in SQLite, but only a *complete* study (audit SCOUT-06 ...)
if variations or timeline:
    save_scouted_content(...)
```

**Change 3 — `scout.py:332-353` the cache-validity rule** (`_is_cached_study_usable`): a
cached payload is reusable only if it is a dict with a known `source_type`, a string `title`,
list `variations`/`full_timeline`, and **at least one** variation or timeline row. A row
written by the old build is therefore re-analysed instead of served, so the cache heals
itself.

**Change 4 — `scout.py:272-281` `force_refresh` is still honoured** and the size cap runs
before any work.

**Evidence** (`tests/test_scout_pipeline.py::TestScoutYouTubePipeline`):
`test_pipeline_extracts_moves_and_caches_a_complete_study`,
`test_failed_analysis_is_not_cached_as_a_permanent_empty_success`,
`test_previously_poisoned_empty_cache_row_is_not_served`.

### SCOUT-04 (HIGH) — extraction ignored spoken order; prose became phantom moves

**Root cause.** Two independent defects.

1. `scout.py:333-381` (old) iterated `board.legal_moves` — python-chess yields piece moves
   before pawn advances — and accepted the **first legal SAN found anywhere** in the snippet,
   so `"e4 e5 Nf3 Nc6"` became `1.Nf3 Nc6 2.e4 e5`.
2. `normalize_chess_speech` rewrote homophones unconditionally
   (`\b(be|bee)\s*([1-8])\b` → `b4`), so "I could be 4 moves ahead" produced `1.b4`.

**Change 1 — `scout.py:141-186`, homophones now require a chess keyword in front of them:**

```python
ctx = r'(?:pawn|pion|square|to|play|plays|played|push|pushes|goes|going)'
t = re.sub(r'\b(be|bee)\s+([1-8])\b(?=\s+' + ctx + r'\b)', r'b\2', t)
```

(plus the same guard for `east/see/day/the`, and the castling patterns now also accept
`castling`/`castled`, which the old `\bcastles?\s*(?:kingside)?\b` never matched).

**Change 2 — `scout.py:758-789` new `_find_next_spoken_move`** returns the legal move whose
surface form occurs **earliest in the remaining text** (ties broken by python-chess order),
including the capture form `x<square>` that speech produces (`normalize_chess_speech` emits
`xd4`, `board.san()` emits `exd5`, so captures were previously undetectable) and the
lowercase forms `nf3`/`bf4`.

**Change 3 — `scout.py:602-630` `commit()` is the single acceptance gate:**

```python
if move not in board.legal_moves:      # audit SCOUT-04: legality gate
    return False
fen_before = normalize_fen(board.fen())
key = (fen_before, move.uci())
if key in seen_moves or len(timeline) >= MAX_TIMELINE_MOVES:
    return False
```

**Change 4 — `scout.py:508-547` `_validate_timeline`** is a defence-in-depth pass over every
produced row: the parent FEN must be valid, the move must be legal **for the side to move**,
`color` must match the FEN side to move, and `san` must be the SAN of that move.

**Evidence:** `test_moves_follow_spoken_order_not_move_generation_order`,
`test_piece_move_spoken_before_pawn_move_wins`,
`test_illegal_move_for_side_to_move_is_rejected`,
`test_out_of_turn_move_is_only_accepted_on_its_own_turn`,
`test_plain_english_is_not_a_move`, `test_spoken_capture_is_recognised`,
`test_every_timeline_row_is_a_legal_in_order_move`.

### SCOUT-05 (MEDIUM) — no dedup, no timestamp validation, silent chapter loss

**Root cause.** `_extract_chess_moves_and_lines` had no `seen` set, ignored its own `duration`
parameter, used `ch['start_time'] != last_chapter_time` as a chapter guard, and flushed a
pending variation only `if len(current_variation_moves) >= 3`.

**Change — `scout.py:549-756`:**
* `seen_moves` (dedup by `(fen_before, uci)`) and `seen_snippets` (dedup by
  `(normalised caption, timestamp)`) — auto-captions repeat sentences verbatim.
* Timestamps go through `_safe_float` and are clamped to `[0, duration]`; unknown durations
  fall back to `MAX_DURATION_SECONDS`.
* `sub_ts` advances by `snippet_step` per mined move, so two moves from one caption no longer
  share a timestamp.
* The chapter walk is a single monotonic pointer over the **sorted, sanitised** chapter list
  (`while chapter_idx < len(chapters) and t_start >= chapters[chapter_idx]['start_time']`).
* `MAX_TIMELINE_MOVES = 600`, `MAX_VARIATIONS = 60`, `MAX_TRANSCRIPT_SNIPPETS = 4000`,
  `text_snippet` truncated to 500 chars.
* Any non-empty pending variation is flushed (`>= 3` magic threshold removed).

**Evidence:** `test_repeated_captions_do_not_produce_duplicate_rows`,
`test_timestamps_are_clamped_to_the_video_duration`,
`test_moves_from_one_caption_get_distinct_timestamps`,
`test_hostile_chapter_metadata_is_sanitised`.
Probe `audit/scripts/fixeri_perf_repro.py` section B:

```
PRE-FIX : timeline rows=1600 unique=4 variations=400   |  min ts=-50.0  max ts=900.0 (duration=120)
POST-FIX: timeline rows=4    unique=4 variations=1     |  min ts=0.0    max ts=120.0
```

### SCOUT-07 (HIGH) — Najdorf video labelled "A45 Trompowsky"; `eco_B20_Sicilian` → Hungarian Invitation

**Root cause.** `openings.py:54-79` reduced relevance to
`all(w in name or w in eco)` — a *substring* test with file order as the tie-break. A title
containing "Guide" matched nothing, so `_analyze_youtube_video` fell through to the first
chapter that matched *anything* ("Poisoned pawn" → Trompowsky Poisoned Pawn). And
`find_opening_by_name` fell back to `q in name.lower()` returning the first hit, so
"Sicilian" hit the alphabetically-earlier `A00 Hungarian Opening: Sicilian Invitation`.

**Change 1 — `openings.py:59-112`** `TITLE_STOPWORDS` + `_query_tokens` + `_match_score`:
word-boundary hits in the **name**, an **exact** ECO-code hit, `+400` for an exact name and
`+200` for a prefix, and a small penalty per name character so the **most specific (shortest)**
name wins a tie.

**Change 2 — `openings.py:114-129` `search_openings`** now sorts by score (file order only as
the final tie-break) and clamps `limit`.

**Change 3 — `openings.py:131-161` `find_opening_by_name`** ranks
`tier 0 exact > tier 1 name-prefix > tier 2 word-boundary-contained`, then shortest name.
`"e4"` no longer matches `"…with Ne4"`.

**Change 4 — `openings.py:163-192` new `identify_best_match(*texts)`** picks the best match
across the video title *and* the chapter titles, preferring the earlier (title) text — so a
specific title match always beats a chapter that merely shares one word.

**Change 5 — `scout.py:393-406`** uses `identify_best_match(title, *chapter_titles)`;
`scout.py:553-560` and `scout.py:722-733` use it for the chapter/title fallbacks too.

**Change 6 — `openings.py:336-341`** `get_strategic_ideas` now matches themes on a word
boundary, so "frenchy stuff" is no longer the French Defense.

**Change 7 — `scout.py:920-940` `_analyze_eco_guide`** validates the id
(`^[A-E]\d{2}$` + a non-empty name part) and **raises** when nothing resolves, instead of
falling back to a hard-coded `1. e4 e5` under a bogus ECO code.

**Evidence** — `audit/scripts/fixeri_07_openings_repro.py`:

```
                                   PRE-FIX                                        POST-FIX
find_opening_by_name('Sicilian')   A00 Hungarian Opening: Sicilian Invitation      B20 Sicilian Defense
find_opening_by_name('French')     A08 King's Indian Attack: French Variation     C00 French Defense
find_opening_by_name('e4')         E43 Nimzo-Indian Defense: St. Petersburg…      None
search('Italian Game')[0]          C47 Four Knights Game: Italian Variation        C50 Italian Game
search('Sicilian Defense Najdorf Guide')  []                                      B90 Sicilian Defense: Najdorf Variation
search('c')                        A00 Amsterdam Attack, Barnes, Clemenz…          []
Najdorf video + "Poisoned pawn"   A45 Trompowsky Attack: Poisoned Pawn Variation   B90 Sicilian Defense: Najdorf Variation
```

Tests: `test_najdorf_position_is_identified_as_najdorf`,
`test_dragon_and_kings_indian_positions`,
`test_najdorf_video_title_beats_a_poisoned_pawn_chapter`,
`test_family_names_do_not_cross_match`, `test_eco_guide_id_resolves_to_its_own_family`,
`test_bogus_eco_guide_ids_are_rejected`, `test_search_ranks_the_exact_name_first`.

### SCOUT-11 (HIGH) — the import endpoint lied

**Root cause.** `import_line_to_repertoire` computed the overlap report and then ignored it:
the write loop ran for every move regardless of `already_present`, unusable moves were dropped
by a silent `continue`, and `status` was hard-coded to `'success'`.

**Change — `scout.py:1230-1379`:**
* `already_present` performs **no write at all** (AGENTS.md §4E: it "prevents redundant
  writes"), so a second import can no longer re-file those moves under a new `study_name`;
  `imported_count`/`new_count` are `0` and `status` is `"already_present"`.
* A SAN-only move is resolved with `board.parse_san(san)` against its own `fen_before` and
  legality-checked, instead of being skipped — what `check_line` counted as new is what gets
  stored.
* Every unusable move records an entry in `errors` (parent FEN invalid, unresolvable SAN,
  missing `fen_before`/`uci`/`san`, engine error).
* `status` is `"success"` only when at least one row was written, `"error"` when nothing was
  written and something failed, `"already_present"` for a no-op. The message says
  "No moves were stored: …" instead of "✓ Added 0 moves".
* New `inserted_count` field: rows that did not exist before this call
  (`imported_count` also counts idempotent updates, which the frontend needs so that an
  explicit `comment`/`arrows`/`study_name` on an already-stored move is still applied).
* `MAX_IMPORT_MOVES = 400` and a non-empty `moves` list are required.

**Evidence:** `test_already_present_reports_no_write_and_does_not_relabel_the_study`
(asserts 0 rows move to the new study), `test_san_only_moves_are_imported_not_silently_dropped`,
`test_wholly_invalid_line_is_not_reported_as_success`,
`test_errors_list_is_complete_for_the_first_three`,
`test_partial_import_reports_only_the_new_moves`.

> **Consequence that needs the frontend + test hand-offs below:** `status` is no longer
> `'success'` for a no-op. Two assertions in `tests/test_scout.py` (owned by FIXER-D) and two
> `if (data.status === 'success')` branches in the frontend (owned by FIXER-F/G) encode the old
> contract. Exact patches are in §4 and §5.

### SCOUT-08 (MEDIUM) — unrecognised input silently became a live YouTube search

**Root cause.** `elif "1." in source:` as a content-type sniff plus an `else` branch that ran
`ytsearch1:<arbitrary user text>` and analysed the winner.

**Change — `scout.py:268-353`:** explicit validation in this order — non-empty, size cap
(`MAX_SOURCE_CHARS = 200_000`), YouTube id, Lichess study link, `eco_*` id, `_looks_like_pgn`
(`scout.py:306-330`: needs move numbers or a `[SetUp]`/`[FEN]` header **and** real movetext), and
otherwise `raise ValueError(...)` → HTTP 400. The search fallback is gone.
`_looks_like_pgn` also makes the FEN-starting PGN flavour (`[SetUp "1"][FEN …]`, which has no
literal `1.`) reachable — it was previously unreachable.

The documented UI contract (`frontend/index.html:920`, "Enter a YouTube video URL, a Lichess
Study link, or paste raw PGN moves") is unaffected.

**Evidence:** `test_unrecognised_input_never_becomes_a_live_search` (9 hostile inputs, each
must be HTTP 400 and must not contain the marker id of the fake search result),
`test_fen_starting_pgn_is_accepted`.

### SCOUT-09 (MEDIUM) — multi-game PGNs fused, studies named `?`

**Root cause.** `chess.pgn.read_game` needs a header/blank-line boundary, so a header-less
blob kept consuming movetext and produced one "line" spanning three openings;
`g_title = Event or White + " vs " + Black or f"Chapter {n}"` let `+` bind before `or` and
python-chess pre-fills `White`/`Black` with `'?'`, making `f"Chapter {n}"` unreachable;
`if game_num == 1: timeline.extend(moves)` dropped games 2..N; every chapter started at 0.

**Change — `scout.py:989-1170`:**
* `_split_pgn_games` (`989-1026`) promotes a result token (`1-0`, `0-1`, `1/2-1/2`, `*`) to a
  game boundary, and a header block that follows movetext to a boundary, so
  `"1. e4 e5 *\n1. d4 d5 *\n1. c4 e5 *"` becomes three chunks.
* `_pgn_game_title` (`1028-1050`) with the correct precedence and `'?'` filtering, falling back
  to `Line N` (not `?`).
* `full_timeline` now holds **all** games' moves (so `duration` is right), each game gets
  `start_time`/`end_time` and a `start_formatted` chapter entry.
* `MAX_PGN_GAMES = 200`; a blob that yields **no** game raises `ValueError` → HTTP 400 instead
  of opening an empty studio.
* All header strings go through `_safe_str` (bound to `MAX_TITLE_CHARS`).

**Evidence:** `test_header_less_multi_game_pgn_is_split`, `test_study_names_are_not_question_marks`.

### SCOUT-10 (MEDIUM) — no timeouts, no caps (262.9 s of CPU in one request)

**Change:**
* `scout.py:51-58` `BASE_YDL_OPTS` now carries `socket_timeout = 15` (and `noplaylist`); both
  call sites (`scout.py:207` search, `scout.py:357` analyze) build their options from it, so
  the constant cannot be forgotten. A regression test asserts every `ydl_opts` passed to
  `YoutubeDL` contains it.
* `scout.py:190-266` `search_online_content` truncates the query to `MAX_QUERY_CHARS = 200`,
  clamps `limit` to 30, and coerces every entry field.
* `scout.py:1178-1191` `check_line_overlap` rejects more than `MAX_CHECK_LINE_PLIES = 300`.
* `scout.py:1193-1215` `check_lines_overlap` rejects more than `MAX_CHECK_TREE_LINES = 40`
  lines or `MAX_CHECK_TREE_PLIES = 2000` total plies.
* `scout.py:891-918` `_analyze_lichess_study` reads the response with
  `stream=True` + `iter_content` and stops at `LICHESS_STUDY_MAX_BYTES = 2 MB`
  (`timeout=10` was already present).
* `lichess_client.py:9-15,148-152` every Lichess request now has a timeout
  (`LICHESS_TIMEOUT = 20`).

Probe `audit/scripts/fixeri_perf_repro.py` section D:

```
PRE-FIX : ACCEPTED (no cap) elapsed=1.098s      # 200 lines x 50 000 moves; the report measured 262.9 s
POST-FIX: rejected after 0.0000s: Too many moves in one tree check (50000); the limit is 2000 plies.
```

Route-level caps are still needed in `api.py` → hand-off §4.

### SCOUT-15 (MEDIUM) — 9 of 17 hostile metadata shapes returned HTTP 500

**Change:** `scout.py:60-113` adds `_safe_str` / `_safe_float` / `_safe_int` (reject `None`,
`NaN`, `inf`, non-numeric, out-of-range; bound length). `scout.py:355-440` coerces
`title`/`uploader`/`channel`/`duration`/`description`/`chapters` before they reach
`sqlite3` or `float()`; `scout.py:442-468` `_sanitize_chapters` drops non-dict entries, caps
at `MAX_CHAPTERS = 60`, clamps to `[0, duration]`, fixes `end_time < start_time` and sorts by
start time. `search_online_content` coerces its entry fields the same way.

**Evidence:** `test_hostile_metadata_shapes_do_not_500` (20 shapes through the real HTTP
endpoint, asserting `status_code < 500`, in `(200, 400, 422)`, and — on 200 — a `str` title and
numeric, in-range chapter times).

### ABUSE-14 (LOW) — quadratic regex, 42.8 s of CPU for a 50 KB description

**Root cause.** `(?:(\d{1,2}:\d{2})\s+)?(?:[Vv]ariation|[Ll]ine)?\s*([A-Za-z0-9\s-]+)?[:\-]?\s*((?:1\.\s*[e4d4c4Nf3][^\n\r]+))`
— three optional groups over a class that *includes* whitespace, so the engine retried every
split point from every offset.

**Change — `scout.py:791-827`:** `DESC_LINE_RE` is matched per line (`desc.splitlines()`) with
one mandatory `[:\-]` delimiter and an anchored end, i.e. linear per line; the input is capped
at `MAX_DESCRIPTION_CHARS = 20_000`; the number of parsed lines is capped at `MAX_VARIATIONS`.
`_parse_time_str` (`829-838`) is now exception-safe.

**Evidence:** `test_large_description_is_bounded_and_fast` (< 2 s for 50 KB),
`test_timestamped_description_line_is_still_found`. Probe section A:

```
             1 KB      10 KB      50 KB
PRE-FIX :   0.019 s    1.718 s   42.315 s
POST-FIX:   0.000 s    0.000 s    0.000 s
```

### ABUSE-01 scope items — `lichess_client.py`

Token hygiene, verified and unchanged in the positive direction: the token is read only from
the `settings` table and `.env`; it is never logged, never returned by a response, never
written to SQLite by this module and never placed in a subprocess `argv` (this module starts
no subprocess — Stockfish is launched from `analyzer.py` with a list argv and no shell).

Changes:
* `redact()` (`lichess_client.py:28-38`) scrubs the token and anything matching
  `lip_[A-Za-z0-9]{8,}`; every log/print that can carry an upstream message now goes through
  it (`lichess_client.py:99,158,178,182,211`), and `_resolve_token` no longer risk-reports
  file contents.
* Timeouts: `LICHESS_TIMEOUT = 20`, `ACCOUNT_TIMEOUT = 10`, `EXPLORER_TIMEOUT = 8`
  (`lichess_client.py:9-15`); the 429 retry no longer uses a different, longer timeout.
* The NDJSON read is bounded: `MAX_NDJSON_GAMES = 5000`, `MAX_NDJSON_LINE_BYTES = 1_000_000`,
  non-dict lines ignored, response closed in `finally`, truncation reported in the error
  string.
* `fetch_user_games_result()` returns `(games, error)` and `fetch_user_games(...,
  raise_on_error=True)` raises `LichessFetchError` — a caller can finally distinguish "no
  games" from "the request failed".

**⚠️ The token must still be rotated.** It is in git history at commit `81f4ed6`
(`git show 81f4ed6:.env` yields a live `lip_…` value). Gitignoring the file does not
invalidate it. The user must revoke/rotate it at <https://lichess.org/account/oauth/token> and
rewrite history (`git filter-repo --path .env --invert-paths`) or re-init the repository. No
code change can do this.

**Evidence:** `test_token_is_never_logged`, `test_redact_scrubs_token_shaped_strings`,
`test_fetch_user_games_has_a_timeout_and_reports_failure`, `test_ndjson_read_is_bounded`.

---

## 3. Test evidence (before / after)

New file: `tests/test_scout_pipeline.py` (41 tests, 7 classes, all network boundaries faked).

**BEFORE (pre-fix `scout.py` / `openings.py` / `lichess_client.py` restored into a scratch
copy of the repo, same test file):**

```
ERROR: test_fetch_user_games_has_a_timeout_and_reports_failure
ERROR: test_hostile_chapter_metadata_is_sanitised
ERROR: test_najdorf_video_title_beats_a_poisoned_pawn_chapter
ERROR: test_ndjson_read_is_bounded
ERROR: test_oversized_search_query_is_truncated_not_forwarded
ERROR: test_partial_import_reports_only_the_new_moves
ERROR: test_previously_poisoned_empty_cache_row_is_not_served
ERROR: test_redact_scrubs_token_shaped_strings
FAIL: test_already_present_reports_no_write_and_does_not_relabel_the_study
FAIL: test_bogus_eco_guide_ids_are_rejected
FAIL: test_check_line_ply_cap
FAIL: test_check_tree_ply_cap_rejects_the_262_second_payload
FAIL: test_eco_guide_id_resolves_to_its_own_family
FAIL: test_errors_list_is_complete_for_the_first_three
FAIL: test_failed_analysis_is_not_cached_as_a_permanent_empty_success
FAIL: test_family_names_do_not_cross_match
FAIL: test_fen_starting_pgn_is_accepted
FAIL: test_header_less_multi_game_pgn_is_split
FAIL: test_hostile_metadata_shapes_do_not_500
FAIL: test_large_description_is_bounded_and_fast
FAIL: test_moves_follow_spoken_order_not_move_generation_order
FAIL: test_moves_from_one_caption_get_distinct_timestamps
FAIL: test_piece_move_spoken_before_pawn_move_wins
FAIL: test_pipeline_extracts_moves_and_caches_a_complete_study
FAIL: test_plain_english_is_not_a_move
FAIL: test_repeated_captions_do_not_produce_duplicate_rows
FAIL: test_search_ranks_the_exact_name_first
FAIL: test_study_names_are_not_question_marks
FAIL: test_timestamps_are_clamped_to_the_video_duration
FAIL: test_token_is_never_logged
FAIL: test_unrecognised_input_never_becomes_a_live_search
FAIL: test_wholly_invalid_line_is_not_reported_as_success
FAIL: test_ydl_calls_carry_a_socket_timeout

Ran 41 tests in 46.397s

FAILED (failures=25, errors=8)
```

Representative pre-fix failure text:

```
FAIL: test_pipeline_extracts_moves_and_caches_a_complete_study
AssertionError: [] is not true : analysis produced no variations at all

FAIL: test_hostile_metadata_shapes_do_not_500
AssertionError: 500 not less than 500 : hostile metadata {'chapters': [{'title': 'A',
  'start_time': None, 'end_time': 10}]} returned HTTP 500

FAIL: test_repeated_captions_do_not_produce_duplicate_rows
AssertionError: 1600 != 4 : timeline contains duplicate (fen, uci) rows

FAIL: test_timestamps_are_clamped_to_the_video_duration
AssertionError: -50.0 not greater than or equal to 0.0 : negative timestamp survived

FAIL: test_moves_from_one_caption_get_distinct_timestamps
AssertionError: 1 != 2 : two moves share one timestamp

FAIL: test_ydl_calls_carry_a_socket_timeout
AssertionError: 'socket_timeout' not found in {'quiet': True, 'skip_download': True,
  'no_warnings': True}
```

**AFTER:**

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_scout_pipeline
Ran 41 tests in 3.350s

OK
```

**Full suite** (run with the base conda interpreter, `/home/robin/miniconda3/bin/python3`),
after the concurrent agents' work settled:

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
FAIL: test_evaluator_illegal_fen_safety (test_repertoire.TestRepertoire)
  AssertionError: True is not false : a refused FEN must not close the shared engine pool
  (backend/evaluator.py — FIXER-C's domain, unrelated to this patch)
Ran 376 tests in 21.194s

FAILED (failures=1)
```

Everything in the Scout / openings surface is green:

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_scout tests.test_scout_pipeline \
      tests.test_openings tests.test_api
Ran 93 tests in 6.902s

OK
```

The two §5 hand-off assertions (`test_scout_import_repertoire`,
`test_single_played_move_import`) were failing when this patch was written and were resolved in
`tests/test_scout.py` by a concurrent agent while the document was being finalised; the §5 patch
is kept because it documents the response-contract change and is what the interim scratch-copy
run was verified against. Baseline before any of my changes was
`Ran 270 tests … FAILED (failures=3, errors=1)`.

### Cross-agent breakage observed while working (not caused by this patch)

1. **`backend/api.py` was syntactically broken mid-run** (`api.py:1293`, a bare
   `leading zeros in decimal integer literals` docstring) for ~90 s while another agent was
   editing it, which made every test module fail to import. It recovered on its own.
2. **A new host/origin guard in `backend/api.py:449-582` answers HTTP 403 to `TestClient`
   requests**, because the default `Host: testserver` is not loopback
   (`is_allowed_request_host`). At the time it landed this produced **51 failures / 14 errors
   across the whole suite**, including tests I do not own. My new test file now uses
   `TestClient(app, base_url="http://127.0.0.1:38281")` so it is immune; other test modules
   (`test_pedagogical_trainer`, `test_user_workflows`, `test_user_behavior_probe`, …) needed the
   same change or `CHESS_ALLOWED_HOSTS=testserver`. This has since been resolved by the owning
   agents, but it is a live hazard for any test module still using a bare `TestClient(app)`.
3. **FIXER-B implemented the route-level payload caps from §4.1 while this patch was being
   written** (`check_line` now answers 422, an oversized body 413). My tests accept
   `400 | 413 | 422` so they hold under both the route model and the scout-layer cap.

---

## 4. Hand-off: `backend/api.py` (FIXER-B)

1. **Route-level caps (SCOUT-10).** `scout.py` now enforces them, but the payload should be
   rejected before it is parsed and the 400 should come from validation, not from deep in the
   business layer. In `backend/api.py:229-247`:

   ```python
   from pydantic import BaseModel, Field

   class ScoutAnalyzePayload(BaseModel):
       source: str = Field(..., max_length=200_000)          # scout_mod.MAX_SOURCE_CHARS
       force_refresh: Optional[bool] = False

   class ScoutImportPayload(BaseModel):
       repertoire: Literal['white', 'black']
       moves: List[Dict[str, Any]] = Field(..., max_length=400)      # MAX_IMPORT_MOVES
       study_name: Optional[str] = Field(None, max_length=300)       # MAX_TITLE_CHARS

   class ScoutCheckLinePayload(BaseModel):
       repertoire: Literal['white', 'black'] = 'white'
       moves: List[Dict[str, Any]] = Field(default=[], max_length=300)   # MAX_CHECK_LINE_PLIES

   class ScoutCheckTreePayload(BaseModel):
       repertoire: Literal['white', 'black'] = 'white'
       lines: List[Dict[str, Any]] = Field(default=[], max_length=40)    # MAX_CHECK_TREE_LINES
   ```

   Import the numbers from `backend.scout` rather than hard-coding them, e.g.
   `from backend.scout import MAX_SOURCE_CHARS, MAX_IMPORT_MOVES, MAX_CHECK_LINE_PLIES, MAX_CHECK_TREE_LINES`.
   Prefer `max_length` (422) over a manual `HTTPException(400)` so the client sees the field.
2. **Search query cap.** `scout_search` (`api.py:1396-1400`) should bound the query length:
   `query: str = Query("Italian Game", max_length=200)`.
3. **`force_refresh` for the Scout UI (SCOUT-06).** `frontend/scout.js:940` sends only
   `{source}`. Either expose a "Re-analyse" affordance that sends `force_refresh: true`, or
   add a query/body flag — with the new cache-validity rule a poisoned row heals on the next
   analysis, so this is a UX improvement rather than a correctness fix.
4. **Honest Lichess failures (ABUSE-01 scope).** `api.py:363-386` currently distinguishes
   "no games" from "request failed" with an *account probe* heuristic. Now that
   `LichessClient.fetch_user_games_result()` returns `(games, error)`, replace it:

   ```python
   raw_games, fetch_error = client.fetch_user_games_result(username, max_games=max_games)
   if fetch_error:
       logger.error("Lichess fetch failed for %s: %s", username, fetch_error)
       raise HTTPException(status_code=502, detail="Could not reach Lichess. Please try again.")
   ```
   (`fetch_error` is already redacted by `backend/lichess_client.py`.) The same applies to the
   four `fetch_user_games` call sites in `backend/repertoire.py:1583,1664,1826,1921` (FIXER-A):
   `games, err = client.fetch_user_games_result(...)`, or
   `client.fetch_user_games(..., raise_on_error=True)` wrapped in the existing `try`.
5. **`/api/scout/analyze` error mapping** already maps `ValueError` → 400 through `_scout_call`;
   the new validation raises `ValueError`, so the new 400s need no route change. Confirm
   `_scout_call` still passes `force_refresh` (it does).

## 4b. Hand-off: `backend/repertoire.py` (FIXER-A)

1. `check_line_overlap` / `check_lines_overlap` re-read the **entire** `repertoire_moves`
   table on every call, and the browser issues 2 × `check_line` + 1 × `check_tree` per board
   move (SCOUT-10c). The ply caps now bound the worst case, but the per-call full-table scan
   remains: add an index on `repertoire_moves(repertoire, parent_fen)` and cache the
   `moves_by_parent` / `moves_by_parent_3` index for a short TTL (or build it once per
   repertoire per process and invalidate on write).
2. SCOUT-11(4) and SCOUT-11(5) — see §6.
3. If you wire `raise_on_error` (§4.4), `fetch_user_games` keeps its default `False`, so
   existing tests that `patch` it are unaffected.

## 5. Hand-off: `tests/test_scout.py` (FIXER-D) — 2 assertions

These two assert the *old, lying* contract ("status is success" for an import that writes
nothing), which SCOUT-11 fixes. I could not edit them (existing tests are FIXER-D's). Apply
verbatim:

```python
# tests/test_scout.py::test_scout_import_repertoire  (line ~123)
        data = res.json()
        # FIXER-I: an import that writes nothing must not claim success (SCOUT-11).
        self.assertIn(data["status"], ("success", "already_present"))
        self.assertGreaterEqual(data["imported_count"] + data["matched_count"], 2)

# tests/test_scout.py::test_single_played_move_import  (line ~177)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn(body["status"], ("success", "already_present"))
        self.assertEqual(body["imported_count"] + body["matched_count"], 1)
```

Verified in a scratch copy: `tests.test_scout tests.test_scout_pipeline` → `Ran 65 tests … OK`.

## 5b. Hand-off: `frontend/scout.js` + `frontend/repertoire_builder.js` (FIXER-F/G)

The import response now has three terminal statuses. Three call sites branch on
`status === 'success'` and must be widened, otherwise a legitimate double-click shows a red
"✗ Import rejected" toast.

1. `frontend/scout.js:2332-2336` (`_submitLineImport`):

   ```js
   const okStatus = data.status === 'success' || data.status === 'already_present';
   if (!okStatus) {
       this._showQuickAddNotice(`✗ Import rejected: ${data.message || 'check the move format'}`, '#ef4444', 6000);
       return false;
   }
   ```
   `_reportImportResult` (scout.js:2285-2312) already handles `imported === 0 &&
   alreadyPresent` correctly (blue "already in …" notice), so no change is needed there beyond
   optionally using the new `inserted_count`:
   `const imported = Number(data.inserted_count ?? data.imported_count || 0);`
2. `frontend/repertoire_builder.js:3612-3613` — already special-cases `data.already_present`
   *inside* the `status === 'success'` branch; change the guard to
   `if (data && (data.status === 'success' || data.status === 'already_present'))`.
3. `frontend/repertoire_builder.js:3686` — same one-line guard change for the
   "📥 All → White/Black" bulk import.
4. `frontend/scout.js:2520` is the `move_meta` route, not the import route — **no change**.
5. Optional: the study header now shows a `transcript_error` string when a video has no
   captions (`/api/scout/analyze` returns it). Render it as an inline note in
   `openStudio()`; do **not** use an in-flow toast (AGENTS.md §8.1).

---

## 6. What is still needed for SCOUT-02 and SCOUT-03 (CRITICAL, `repertoire.py`)

Neither is fixable from `scout.py`; both live in `RepertoireManager.add_repertoire_move`
(`backend/repertoire.py:431-1015`) and are **not** addressed by this patch. Reproduced live by
`audit/scripts/fixeri_scout02_03_repro.py` (read-only w.r.t. backend code).

### SCOUT-02 — a divergent line import deletes the prepared user move and its whole subtree

**Mechanism (`repertoire.py:511-557`).** `add_repertoire_move` enforces "one user move per
position". When a *different* user move arrives for a position that already has one, it runs
`SELECT … WHERE is_user_move = 1` → `DELETE` → recursive orphan prune:

```python
existing_user_move = cursor.fetchone()
if existing_user_move and existing_user_move["move_uci"] != move_uci:
    old_child_fen = existing_user_move["fen"]
    cursor.execute("DELETE FROM repertoire_moves WHERE repertoire=? AND parent_fen=? AND is_user_move=1", ...)
    to_delete = [old_child_fen]
    while to_delete:                       # recursive prune, no depth bound
        ... DELETE FROM repertoire_moves WHERE parent_fen = norm_curr OR parent_fen = curr_3 OR parent_fen LIKE "curr_3 %"
```

**Reproduced** (7 prepared plies, then one conflicting `1.d4` import):

```
[after preparing 1.e4 e5 2.Nf3 Nc6 3.Bc4 Bc5 4.d3 under 'My Italian']
  repertoire_moves (7): e2e4, e7e5, g1f3, b8c6, f1c4, f8c5, d2d3      (all @My Italian)
[after the conflicting 1.d4 import]
  repertoire_moves (1): d2d4@Scouted          <-- all 7 prepared plies destroyed
  study_moves      (8): 7 orphaned links to 'My Italian' + 1 to 'Scouted'
  !! DANGLING study_moves rows: 7
```

`scout.py` cannot prevent it: the frontend already shows a `window.confirm` warning
(`overlapReplacesPreparedMove`, scout.js:2270-2272) but the request is still sent, and the
backend performs the destruction unconditionally.

**Remaining work, precisely:**
1. Add `replace_existing: bool = False` (or `on_conflict: Literal["conflict","replace"]`) to
   `add_repertoire_move`. With the default, a *different* user move for an already-prepared
   position must return
   `{"status": "conflict", "message": …, "existing_uci": …, "existing_san": …}` and **write
   nothing** — no `DELETE`, no prune.
2. Thread the flag through `ContentScout.import_line_to_repertoire` (`scout.py:1230`): add a
   `replace_existing: bool = False` argument, forward it, and surface a conflict as
   `status: "conflict"` carrying `overlap_details.divergence_info` (already computed at
   `repertoire.py:1143-1162`), so the existing `window.confirm` becomes meaningful instead of
   cosmetic.
3. Only with `replace_existing=True` should the current delete + prune run. Two regression
   tests: the prepared move *and* its subtree survive a conflicting import; they *are* removed
   when the flag is true.
4. Audit the prune itself: `parent_fen LIKE "curr_3 %"` with
   `curr_3 = " ".join(norm_curr.split()[:3])` (placement + turn + castling) matches
   transpositions, and the loop issues `COUNT` / `SELECT` / `DELETE` per node with no depth
   bound. Add a depth cap and an index on `repertoire_moves(repertoire, parent_fen)`.

### SCOUT-03 — an overlapping import hijacks `study_name` and leaves dangling `study_moves`

**Mechanism.** Study bookkeeping lives in the same upsert (`repertoire.py:569-616` and the
`update_repertoire_metadata` twin at `repertoire.py:990-1003`): `study_name` is denormalised
into `repertoire_moves.study_name` **and** mirrored into `repertoire_studies` +
`study_moves`. Note the schema (`db.py:171-178`): `study_moves` is keyed by
`(study_id, repertoire, parent_fen, move_uci)` and has a foreign key **only** to
`repertoire_studies` — there is no relation to `repertoire_moves`, so deleting a move can
never cascade. Three defects, all reproduced:

```
[after filing 1.e4 under 'Study A']            repertoire_moves: e2e4@Study A
[after re-importing the SAME move with study_name='Study B']
  repertoire_moves (1): e2e4@Study B      <-- denormalised column hijacked
  study_moves      (2): studyA:e2e4, studyB:e2e4   <-- AND still linked to BOTH studies
  repertoire_studies: ['Study A', 'Study B']      <-- 'Study A' now shows a stale member
```

1. **Hijack / split membership.** `final_study` is
   `study_name.strip() if study_name and study_name.strip() else <existing>`
   (`repertoire.py:569`), and the new link is *inserted* (`repertoire.py:613`,
   `INSERT OR IGNORE INTO study_moves`) while the old link is **never removed**. So a
   re-import both rewrites the denormalised column *and* leaves the move in its old study —
   the two sources of truth now disagree. This is the part my patch stops from the Scout side
   (`already_present` performs no write at all), but **any other caller** —
   `add_repertoire_move` directly, `/api/repertoire/move_meta`
   (`repertoire.py:978-983` updates `study_name` with no study-link cleanup), the builder's
   "import all lines" — can still trigger it.
2. **Dangling `study_moves`.** The SCOUT-02 prune deletes `repertoire_moves` rows without
   touching `study_moves`; the repro above shows 7 orphaned links and a `repertoire_studies`
   row ("My Italian") left with zero moves, which the study list then renders as an empty
   study.
3. **No rename propagation.** Renaming a study updates `repertoire_studies.name` but not the
   denormalised `repertoire_moves.study_name`, so the two drift apart permanently.

**Remaining work, precisely:**
1. In the SCOUT-02 prune, delete the matching `study_moves` rows
   (`DELETE FROM study_moves WHERE repertoire=? AND (parent_fen=? OR parent_fen=? OR
   parent_fen LIKE ?)`) alongside each `repertoire_moves` delete — the prune already computes
   exactly those keys. Alternatively denormalise `study_moves` to a real `move_id` FK with
   `ON DELETE CASCADE`. Add a reconciliation query that drops `study_moves` rows with no
   matching `repertoire_moves` row and `repertoire_studies` rows with no `study_moves`.
2. Make study membership a first-class decision: `on_study_conflict: Literal["keep","move"]`
   (default `"keep"`), and when it is `"move"` remove the old `study_moves` row in the same
   transaction as the insert.
3. Stop denormalising `study_name` into `repertoire_moves`; derive it from
   `repertoire_studies ⋈ study_moves` (the code at `repertoire.py:2151-2152, 2316-2325`
   already does the join — use it for every read) — this removes the possibility of (1) and (3).
4. Regression tests (none exist today): re-import the same line under a different study name
   and assert membership is unchanged; delete a move that belongs to a study and assert no
   dangling `study_moves` row and no empty `repertoire_studies` row remains.

---

## 7. Also noticed (not fixed)

1. **`scout.py:1052` `_analyze_raw_pgn` keeps every game's moves in `full_timeline`.** For a
   200-game Lichess study import the response can be tens of MB. `MAX_PGN_GAMES = 200` bounds
   the games but not the plies per game; consider a `MAX_TIMELINE_MOVES` ceiling here too.
2. **`openings.py:32-52` `_load_openings` runs on first use and holds the whole 3815-entry
   encyclopedia in memory** (~2 × FEN-keyed dicts). `identify_best_match` is O(entries) per
   call; with the chapter loop capped at 60 it is fine, but an inverted name index would make
   `find_opening_by_name` O(1).
3. **`backend/scout.py` still `print`s nothing but `logger`s at INFO/WARNING**; `logger` is not
   configured by this module, so messages surface through the root logger only. Consider
   `logger.debug` for the per-snippet paths.
4. **`repertoire_moves` has no index on `(repertoire, parent_fen)`** (see §4b).
5. **`tests/test_scout.py` has no test for `_analyze_lichess_study`** (the only path that
   performs a real `requests.get`); it is now bounded but still unexercised.
6. **`_fetch_transcript` requests only `('en',)`** and never tries `language_translators`.
   Non-English chess channels produce no timeline. A future improvement, not a bug.
7. **`scout.py:1052` a PGN whose only game is header-less and result-less** (e.g.
   `1. e4 e5`) is accepted as one game — correct — but such a blob is indistinguishable from
   a truncated multi-game export, so the first (and only) line wins.
