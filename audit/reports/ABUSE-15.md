# ABUSE-15 — Cross-cutting summary: negative results, low-severity findings, and unproven suspicions
- **Severity:** low
- **Type:** security
- **File:** (multiple — see sections)
- **Line(s):** (multiple)
- **Component:** horizontal audit summary
- **Repro script:** `audit/scripts/atk5_*.py`
- **Detected by:** ATK-5

## Expected
Every class of cross-cutting attack listed in my brief (secret handling, global
exception handlers, injection, traversal, SSRF, subprocess safety, resource
exhaustion, concurrency, DoS/complexity, CORS/headers, Electron, production-DB
pollution, fuzzing) is either **exploitable and reported**, or **provably not
exploitable and recorded as a negative result** so it is not re-audited.

## Actual
11 of the 13 attack classes yielded at least one verified, runnable finding
(ABUSE-01 … ABUSE-14). Two classes came back clean: **path traversal / static-file
exposure** and **SQL injection**, plus **SSRF** as a third (see section B). The
remaining low-severity findings and the unproven suspicions are in sections A
and C.

## Root cause
The application is a single-user local tool with no trust boundary assumed: every
input is treated as trusted (no auth, no size caps, no escaping of remote HTML,
no locks on shared singletons), which is simultaneously the cause of nearly every
finding in this report.

## Exploit / escalation
The most damaging chain is **ABUSE-04 → ABUSE-03**: a YouTube video title is
attacker-controlled, is stored raw, is rendered with `innerHTML` (verified
execution in real Chromium), and the resulting script can call the completely
unauthenticated `POST /api/repertoire/clear`. The most reachable remote chain is
**ABUSE-03 alone**: any web page the user visits can read and destroy the local
app's data. See each finding's "Exploit / escalation" section for variants.

---

---

## A. Verified low-severity findings

### ABUSE-15a — `LIKE` wildcards are not escaped in the game search box
- **File:** `backend/api.py:518-522` (`search: Optional[str] = Query(None)`) and
  528-531 (`q = f"%{search.strip()}%"`, `LIKE ?`)
- **Repro:** `audit/scripts/atk5_misc_repro.py` section C
- **Observed:**
  ```
  C) /api/games/recent?search= -> LIKE wildcards are not escaped
    search='%'        -> total=307
    search='%%'       -> total=307
    search='_'        -> total=307
    search='RobiDeli' -> total=304
    search='a%'       -> total=297
  ('%' returns the whole table: the 'search' box is not a filter)
  ```
- **Impact:** the search filter is trivially bypassed (`%` = everything, `_` = any
  single char). No injection (the value *is* parameterised), but combined with
  ABUSE-03 it is a one-line full-archive dump. Fix: escape `%`, `_` and the
  default escape character in `search` and add `ESCAPE '\'`.

### ABUSE-15b — `POST /api/scout/favorite` is a non-idempotent read-modify-write toggle
- **File:** `backend/db.py:485-497` (`toggle_favorite_scouted`)
- **Repro:** `audit/scripts/atk5_concurrency_repro.py` section C
- **Observed:**
  ```
  C) /api/scout/favorite x10 PARALLEL toggle
    reported: [True, True, False, True, False, False, False, False, True, True]
    final is_favorite in DB: 0 (10 toggles from 0 should end at 0)
  ```
  Ten concurrent toggles produced five different answers to the same question.
  Low impact (cosmetic bookmark state) but it is the same lost-update class as
  ABUSE-05 and is trivially reachable by a double-click.
- **Fix:** make it `PATCH`-style (`{"is_favorite": true|false}`) or
  `is_favorite = NOT is_favorite` in a single statement.

### ABUSE-15c — `POST /api/repertoire/seed` returned a bare `null` body (observed, since fixed)
- **File:** was `backend/repertoire.py:1865-1910` (`seed_default_repertoires` fell
  off the end returning `None`)
- **Repro:** `audit/scripts/atk5_cors_noauth_repro.py` section 4
- **Observed (against the tree as I found it):**
  ```
  POST /api/repertoire/seed?force=true (no auth) -> 200 null
  repertoire_moves after force-seed: 72
  ```
  A 200 with body `null` gave the frontend nothing to report. A concurrent
  fixer has since made every return path yield a dict
  (`{"status":"ok","seeded_moves":72,"total_moves":72}` and
  `409 {"status":"skipped", …, "force_required":true}`), so this is **closed**.
- **Note for the orchestrator:** during that in-flight patch I briefly observed a
  live regression — `probe.fetchone()` on a `sqlite3.Connection` at
  `repertoire.py:2093` (`AttributeError` → `500 {"detail":"Seeding failed
  unexpectedly…"}` with **zero** moves seeded). It was fixed within minutes
  (`probe_cursor = probe.cursor()`); flagging it only so the patch author
  re-verifies that path.

---

## B. Negative results — attacks that did **not** work (do not re-audit)

| # | Attack | Result | Evidence |
|---|--------|--------|----------|
| N1 | **Static-mount path traversal / `.env` + `chess_improvement.db` exposure through `app.mount("/", StaticFiles(frontend))`** | **Blocked.** 27 payloads over HTTP plus 9 raw-ASGI scopes (bypassing httpx URL normalisation) all returned `404 {"detail":"Not Found"}`; no `lip_`, no `SQLite format 3`, no `/etc/passwd` content. Starlette's `StaticFiles` resolves and containment-checks the path. | `audit/scripts/atk5_static_exposure_repro.py` sections C+D |
| N2 | **SQL injection in any string parameter** | **No injection found.** Every value reaching SQLite is parameterised; the only f-string SQL in the codebase is (a) a generated `IN (?,?,…)` placeholder list (`db.py:364`, `repertoire.py:1340`), (b) a generated `SET` column list from a hard-coded whitelist (`repertoire.py:864` → `SET is_important = ?, comment = ?, …`), and (c) `ORDER BY`/`LIMIT` never interpolate user text. `' OR 1=1 --`, `'; DROP TABLE repertoire_moves; --`, `%00`, stacked queries, unicode/emoji and bidi-override fuzz strings all returned 422/400/200-with-no-effect. | `audit/scripts/atk5_route_fuzz.py` |
| N3 | **SSRF via user-supplied URLs** | **None reachable.** The only user-influenced fetch is the Lichess study PGN: `scout.py:574` matches `lichess\.org/study/([a-zA-Z0-9]{8})` and interpolates that 8-char alnum id into a hard-coded `https://lichess.org/study/{id}.pgn`; `gopher://`, `file:///etc/passwd`, `http://127.0.0.1:…` and `http://169.254.169.254/` cannot be reached because the scheme+host are literals. The yt-dlp paths are constrained too: `extract_youtube_id` only accepts an 11-char `[A-Za-z0-9_-]` id, and the search path always prefixes `ytsearch{N}:`. | `audit/scripts/atk5_route_fuzz.py`; `backend/scout.py:19-37, 574-585` |
| N4 | **Token disclosure via API responses, error handlers, SSE events, DB or `argv`** | **Clean.** The token never appears in any of 12 GET endpoints, is not echoed by `POST /api/settings`, is not in the `@app.exception_handler(ValueError)` body (`str(exc)`), and is not on any subprocess command line (`chess.engine.popen_uci` uses a list argv, no `shell=True`; `STOCKFISH_PATH` is a module constant). Non-finite floats are properly rejected by pydantic (`allow_inf_nan=False`) and 422/405 bodies contain no internals. | `audit/scripts/atk5_secrets_repro.py` §3-5, `atk5_inf_overflow_repro.py` §A/C |
| N5 | **`raise_server_exceptions=True` vs `False` divergence** | **Only the one known case** (`/api/blunders?limit=10**30`, ABUSE-07). Every other route behaves identically under both settings; there is no route that "only works" because TestClient re-raises. | `audit/scripts/atk5_route_fuzz.py` (last section) |
| N6 | **Concurrent SQLite writers → `database is locked`** | **Not reproduced.** 30 parallel writes across `POST /api/settings`, `POST /api/repertoire/move` and `POST /api/scout/favorite` all returned 200/400 in 0.05 s, because `backend/db.py:19` uses `timeout=30.0` and WAL. The `database is locked` risk remains latent for the *leaked* connections of ABUSE-10, but I could not trigger it. | `audit/scripts/atk5_concurrency_repro.py` §D |
| N7 | **Oversized `fen` query parameter** | **Rejected fast.** `is_valid_fen` returns `False` in 48 ms for a 400 000-char pieces field, and `GET /api/repertoire/explore?fen=…` returns `400 {"detail":"Invalid FEN string"}` in 13 ms at 50 000 chars (uvicorn/httpx cap the URL at ~200 kB). No CPU blow-up. | `audit/scripts/atk5_dos_repro.py` §A |
| N8 | **Electron hardening** | **Sound.** `electron/main.js:391-395` sets `nodeIntegration:false, contextIsolation:true, sandbox:true, webSecurity:true, allowRunningInsecureContent:false`; a concurrent fixer has since added `contents.on('will-navigate', blockForeignNavigation)` (line 413) and `contents.setWindowOpenHandler` (line 422). `preload.js` exposes only `isDesktop/platform/version/minimize/maximize/close/onSyncGames` via `contextBridge` — no `ipcRenderer`, no `require`, no fs. `spawn()` for the Python child uses a list argv with no shell, and `stopPythonBackend()` is wired to `before-quit`, `window-all-closed` and SIGINT/SIGTERM. No `remote`, no `nodeIntegration` fallback. | `electron/main.js`, `electron/preload.js` |
| N9 | **`seed_default_repertoires(force=False)` guard (AGENTS.md §9.4)** | **Holds.** With moves present and `force` omitted/false the call returns `409 {"status":"skipped","message":"Repertoire already contains moves. Seeding skipped to protect user data.", …}` and inserts nothing. The endpoint requires the explicit `?force=true`. | `audit/scripts/atk5_misc_repro.py` §A |
| N10 | **All discovered test modules isolate the DB** | **Yes.** Every one of the 11 modules matched by `unittest discover tests` (104 tests) imports `tests.test_setup`, and no backend module opens a connection at import time (`get_db()` re-reads `CHESS_DB_PATH` on every call), so import order cannot leak. | `tests/test_*.py`, `backend/db.py:14-22` |

---

## C. Unproven suspicions (do NOT treat as confirmed)

1. **`AsyncEngineEvaluator` SSE starvation / engine lock hold.** `stream_eval`
   holds a single `asyncio.Lock` around `engine.analysis(..., Limit(depth=…))` with
   **no time limit** and no semaphore/queue cap, and `depth` is accepted up to 30
   (`api.py:1327`). A client that opens `GET /api/repertoire/eval_stream` and never
   reads the response should be able to hold the engine via TCP back-pressure and
   starve every other eval request. **I could not demonstrate it**: under
   `TestClient` the trivial positions finished at depth 30 in milliseconds, so the
   lock was never held when I probed it, and `TestClient`'s per-request event loop
   also resets the engine between requests. A concurrent fixer has since rewritten
   `backend/evaluator.py` (semaphores at lines 387 and 566), so this may already be
   addressed — needs re-testing against real uvicorn.
2. **SSE error events leak `str(e)`.** `backend/evaluator.py` yields
   `data: {"error": str(e)}` on engine failure, which can carry absolute paths /
   engine internals. I verified the code path exists but could not trigger a real
   engine exception to observe the payload.
3. **`_user_stats_cache` / `_explorer_count_memo` dict mutation from the
   threadpool.** `RepertoireManager` mutates these dicts (`repertoire.py:71-72`,
   `:104-107`) from sync routes with no lock. CPython dict ops are atomic enough
   that I saw no corruption, but the memoised *values* can be stale/torn across
   the 60 s window. Unproven.
4. **Electron orphan-Python-child on quit.** `stopPythonBackend()` schedules a
   `SIGKILL` 1200 ms after `SIGINT`, but `app.quit()` in `before-quit` can tear the
   event loop down before the timer fires. Static reasoning only; I did not launch
   the desktop app (no display / would touch the production DB).
5. **`isServerRunning()` before `loadURL()`** — a rogue local process answering
   `200` on `127.0.0.1:38281/api/dashboard` would cause Electron to load it with
   the preload bridge attached. Local-only, not demonstrated.
6. **Deep body + `/api/scout/check_tree` algorithmic cost** — measured linear
   (2000 lines → 1.34 s), so I did not find super-linear behaviour; but each line
   re-reads and re-indexes the whole `repertoire_moves` table
   (`repertoire.py:925-947`), so the true complexity is
   `O(lines × repertoire_rows)` and I could not build a large enough *legal*
   repertoire in the time budget to demonstrate a blow-up. Reported as part of
   ABUSE-09 rather than as its own finding.

---

## D. One operational note

Two of my own probes were killed with `SIGKILL` while holding Stockfish children
(`audit/scripts/atk5_dos_repro.py` hung in its 200 000-char regex section, and one
`kill -9` sweep in an earlier revision of `atk5_engine_leak_repro.py` was too broad
and terminated engines belonging to other agents' concurrent `unittest` runs). The
orphans left behind were reaped; no production data or process (PID 225248) was
touched. Lesson recorded: scope `pgrep`/`kill` to PIDs recorded before the probe.
