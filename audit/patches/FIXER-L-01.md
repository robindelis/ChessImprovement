# FIXER-L-01 — `backend/api.py`: local-origin/CSRF boundary, resource caps, security headers, explorer caching

**Owner:** FIXER-L (exclusive write access to `backend/api.py`)
**Findings fixed:** ABUSE-03, ABUSE-07, ABUSE-08, ABUSE-09, ABUSE-13, SYNC-07, SYNC-08, SYNC-09, SYNC-10, SYNC-14, BUILDER-12, plus hand-offs FIXER-C §6.1/§6.2 and FIXER-F item 16.
**Files touched:** `backend/api.py` (only application file), **new** `tests/test_api_security.py`, `audit/scripts/api_prefix_snapshot_FIXER-L.py`, `audit/scripts/FIXER-L_before_after.py`, `audit/scripts/FIXER-L_ablation.py`, `audit/scripts/FIXER-L_e2e_origin.py`, `audit/evidence/FIXER-L_*.txt`, this report.
**Built on:** `FIXER-B-01.md` — nothing there was undone or contradicted (§9 of this report lists what FIXER-B must know).
**Production data:** never written. Every probe and test imports `tests.test_setup` first, which repoints `CHESS_DB_PATH` at a throwaway temp DB before any backend module is imported, and `FIXER-L_e2e_origin.py` **hard-refuses to run** if `CHESS_DB_PATH` is already set (then forces its own temp path). Port 38281 was never contacted; the end-to-end script uses scratch port **39399**.

> **Disclosure — production `mtime` moved, data did not.** I opened the production DB exactly once, read-only, at 19:43 (`mode=ro` **without** `immutable=1`). That created a `chess_improvement.db-shm` sidecar. The file's `mtime` later advanced to `20:30:18` and the `-wal`/`-shm` files disappeared — that is a **WAL checkpoint** (somebody's read/write or non-`immutable` read-only handle folded the `-wal` into the main file), not a data change. The data is verifiably intact, and every table still matches the counts documented in `AGENTS.md` §9.7:
>
> ```
> games 307   blunders 153   scouted_content 31   explorer_cache 444
> drill_reviews 153   settings 5
> repertoire_moves 0   repertoire_studies 0   study_moves 0
> ```
>
> `repertoire_moves`/`repertoire_studies` were already empty before this session began (the `CODE-05-03` migration incident; `recovered/repertoire_moves_from_git_HEAD.json` holds the pre-incident export). I cannot attribute the 20:30 checkpoint to a specific process — several agents run probes concurrently — but none of my four scripts can reach production, and my own inspection of it was read-only. Recording it here so the orchestrator can correlate it. The correct read handle, per the `AGENTS.md` §9.1 rule added during this session, is `file:…?mode=ro&immutable=1`; I have used that form for every check after the 19:43 read.
**Interpreter:** `/home/robin/miniconda3/bin/python3` (base conda env) only.

---

## 0. Evidence: method, and why the "before" is trustworthy

`git` is not a usable baseline (`backend/api.py` was already far from `HEAD`). Instead the **exact pre-fix module** was snapshotted before the first edit:

* `audit/scripts/api_prefix_snapshot_FIXER-L.py` — a byte copy of `backend/api.py` as FIXER-B left it, with `FRONTEND_DIR` pinned to the real `frontend/` (the snapshot lives in `audit/scripts/`, so the un-pinned `dirname(dirname(__file__))` would silently skip the `StaticFiles` mount and hide the bug under test — the same trap FIXER-B documented). **Evidence only; never imported by the app.**
* `audit/scripts/FIXER-L_before_after.py` — imports the snapshot *and* the patched `backend.api` into one process and runs the **same 25 check functions** from `tests/test_api_security.py` against both.

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-L_before_after.py
BEFORE: 1 passed / 24 failed
AFTER : 25 passed / 0 failed
```

Full log: `audit/evidence/FIXER-L_before_after.txt`.

The single check that passes **both** ways is `check_game_gaps_rows_carry_best_san_and_cpl_loss` — a deliberate **control check**. FIXER-A's `repertoire.py` rewrite (landed at 19:19, before this pass) already projects `best_san, cpl_loss` in the `all_blunders` query, so FIXER-F's item 16 is satisfied upstream. The check is kept as a **regression guard** and was made non-vacuous (it seeds two blunders in one game so `all_blunders` is multi-row and `earlier_mistake` is non-null, and it fails if fewer than three chained rows are exercised).

### Real socket, real browser headers — `audit/scripts/FIXER-L_e2e_origin.py`

Not `TestClient`: this boots `uvicorn backend.api:app` on scratch port 39399 with a temp DB and drives it with `urllib` requests carrying real `Host`/`Origin`/`Sec-Fetch-Site` headers. Full log: `audit/evidence/FIXER-L_e2e_origin.txt`.

---

## 1. ABUSE-03 (HIGH) — the headline: CORS/CSRF boundary

### Root cause

`backend/api.py:185-191` (pre-fix):

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Three independent defects compose into full compromise:

1. **`allow_origins=["*"]` + `allow_credentials=True` is not "allow everything", it is "reflect everything".** Starlette cannot answer `*` when credentials are allowed, so it *echoes the caller's `Origin`* and stamps `Access-Control-Allow-Credentials: true`. Every origin on the internet is granted read access to the user's game library.
2. **No CSRF token and no `Origin`/`Sec-Fetch-Site` check anywhere.** A cross-origin `fetch` (or even a plain, non-CORS `<form>` POST, which cannot set headers) reaches every route.
3. **No authentication at all** — there is not one `Depends`/`Security`/`HTTPBearer` in the codebase — so "the route is on localhost" is the *only* thing standing between a web page and `POST /api/repertoire/clear`.

Measured on the pre-fix module (`audit/evidence/FIXER-L_ablation.txt`, section D):

```
pre-fix  GET /api/dashboard -> 200 ACAO='http://attacker.example' ACAC='true'
patched  GET /api/dashboard -> 403 ACAO=None ACAC=None
```

### Threat model — what the fix must and must not do

This is a **single-user desktop app bound to `127.0.0.1`** whose API has **no authentication**. The adversary is therefore *not* "the internet"; it is **"any other web page open in the user's browser"**, plus DNS rebinding. Two consequences:

* The fix does **not** need a login system — and adding one would be a product decision, not an audit fix.
* But it **does** have to stop a hostile *web origin* from issuing state-changing requests, and it **must not** break the Electron shell (`electron/main.js:435` → `mainWindow.loadURL('http://127.0.0.1:<port>')`) or a plain browser tab on the same local origin.

### Design chosen, and why

A single pure-ASGI guard, `LocalBoundaryMiddleware` (`backend/api.py:512-697`), replacing `CORSMiddleware` entirely. Five layers:

| # | Layer | Where | What it stops |
|---|-------|-------|---------------|
| a | **No CORS reflection, ever** | `:700` (`app.add_middleware(LocalBoundaryMiddleware)`) | `Access-Control-Allow-Origin` is emitted **only** for an allow-listed local origin, and **never** with `Access-Control-Allow-Credentials: true` |
| b | **Strict origin allow-list** | `is_allowed_local_origin` `:435-462` | Any `http` origin on a loopback host, any port, plus the self-origin, plus `CHESS_ALLOWED_ORIGINS`. **`null` (sandboxed iframe / `data:` / `file:`) and every `https`/remote origin are refused** |
| c | **Preflight refusal** | `:665-696` | A cross-origin `fetch` with `Content-Type: application/json` is preflighted; we answer `403`, so the browser **never sends the real request** |
| d | **`Origin` + `Sec-Fetch-Site` enforcement, every method** | `:632-661` | `<form>` / `no-cors` submissions, which cannot set custom headers. Enforced for *all* methods, not just mutating ones, because `GET /api/games/sync_recent_stream` and `GET /api/repertoire/sync_games_stream` really do run Stockfish and write blunders |
| e | **`Host` validation (DNS-rebinding blunting)** | `is_allowed_request_host` `:464-477`, applied `:663-671` | A rebound `evil.test` name. Escape hatches `CHESS_ALLOWED_HOSTS` / `CHESS_ALLOWED_ORIGINS` |

**Why layers (c) and (d) together rather than a required custom header** (the option the brief floats): a cross-origin `fetch` **cannot** set a custom header without a preflight, and we refuse the preflight — so the preflight refusal is strictly *stronger* than the header requirement, and it needs **zero frontend change**. A custom header is also strictly *weaker* against `<form>`/`no-cors` submissions, which cannot set headers at all; only the `Origin` check catches those.

**Why the allow-list is "loopback on any port"**: the app answers on whichever port uvicorn/Electron picked (`electron/main.js:474-482` falls forward to a free port), and the user may type `localhost` where Electron uses `127.0.0.1`. Content served from a loopback port can only come from a process on the user's own machine; if such a process exists, the CSRF boundary is already moot.

**Ordering, and the two scoping decisions on the `Host` rule** — these were corrected during review after the orchestrator correctly flagged that a 403 from the Host rule is indistinguishable from a 403 from the Origin rule:

1. **`Origin` is evaluated first**, so a hostile origin is always the *named* cause in the log. Running the Host check first made every browser probe look like a rebinding probe and hid which defence actually fired.
2. **The Host rule is scoped to *browser-driven* requests** (those carrying `Origin` or `Sec-Fetch-*`). DNS rebinding needs a browser to be useful, and a direct non-browser client is not a CSRF vector at all. This is also what keeps Starlette's own `TestClient` working: it sends `Host: testserver`, which is a name **no browser can resolve**, so allowing it through this scoping carries **no** rebinding risk in production. (An earlier revision applied the Host rule unconditionally and broke 51 unrelated tests — see §11.)
3. **A mutating request with no `Origin` is served** (`:632`). This is the documented non-browser client surface and is locked in by `check_mutating_request_without_origin_is_allowed`.

**Rejected alternatives**

* **Login / API key / `HTTPBearer`** — explicitly out of scope; a product decision (the user would have to type a secret into their own desktop app) and it changes every frontend call site.
* **Required custom header on mutating requests** — effective, but only enforceable if the client sets it, and `frontend/**` + `electron/**` belong to other fixers in this wave; shipping it here would break the UI. Handed off (§10). It is also strictly weaker than (c)+(d), as argued above.
* **Per-install random shared secret in a header** — same blocker, and it reintroduces the auth system the brief rules out.
* **Keep `allow_origins=["*"]` and add an `Origin` check on top** — the middleware would still *reflect* the very header it is meant to reject. Deleting it is the fix.
* **`Origin` allow-list only, no `Sec-Fetch-Site`** — `Sec-Fetch-Site` is a browser-emitted signal independent of `Origin`, and it is what catches the `same-site` rebound case (`evil.test:38281` vs `evil.test:80` is the *same site*, so a naive `Sec-Fetch-Site: cross-site` test would miss it; `same-site` is explicitly refused).

### What remains (documented, not fixable in this file)

* A **non-browser** client (`curl`, a local script, another process on the box) can still call anything. There is no auth, by design; if an attacker can run code on the machine, the CSRF boundary is moot.
* A hypothetical pre-2016 browser that sends neither `Origin` nor `Sec-Fetch-Site` on a cross-site POST is indistinguishable from `curl`.
* `Origin: null` is refused, so a genuinely `file://`-hosted copy of the UI would not work. `electron/main.js` loads over `http`, so this does not affect the shipped shell.

### One parser-differential found and closed during self-review

`is_allowed_local_origin` splits the authority on the first colon, so
`http://127.0.0.1:38281.evil.com` yielded host `127.0.0.1` + a non-numeric
"port" and was **allow-listed**. No browser emits such an `Origin` (it is not a
valid URL), so it was not exploitable — but a header this function accepts while
the next parser in the chain would not is exactly how an allow-list rots.
`backend/api.py:461-465` now refuses any authority whose port is not numeric, and
`check_malformed_origins_are_not_pattern_matched` locks it in (4 malformed
origins refused, 5 well-formed loopback origins still accepted, and the refusal
reaches the wire as a 403).

### Verified — real cross-origin attempt, fail-before / pass-after

`audit/evidence/FIXER-L_e2e_origin.txt` (real socket, real headers, scratch port 39399, temp DB):

```
[ok  ] POST /api/repertoire/clear from http://attacker.example is refused  -> 403 {"detail": "Cross-origin requests are not allowed by this local app."}
[ok  ] no Access-Control-Allow-Origin on the refusal
[ok  ] no Access-Control-Allow-Credentials anywhere
[ok  ] GET /api/games/recent from http://attacker.example is refused  -> 403
[ok  ] cross-origin preflight is refused  -> 403
[ok  ] cross-origin read of the dashboard is refused  -> 403
[ok  ] GET / (the SPA shell) is 200 text/html  -> 200
[ok  ] same-origin GET /api/dashboard is 200  -> 200
[ok  ] same-origin GET /api/repertoire/tree is 200  -> 200
[ok  ] same-origin POST /api/repertoire/clear is 200  -> 200 {"status":"ok","message":"Entire repertoire database reset successfully.","deleted_count":…}
[ok  ] same-origin POST /api/settings is 200  -> 200
[ok  ] same-origin POST /api/scout/check_line is 200  -> 200
[ok  ] POST from the localhost spelling of the origin is 200  -> 200
[ok  ] same-origin DELETE /api/repertoire/move (JSON body) is 200  -> 200
[ok  ] GET /api/scout/recent is 200 and fast  -> 200
```

The pre-fix snapshot answers the same requests `200 {"status":"ok","deleted_count":N}` with `ACAO: http://attacker.example, ACAC: true` (`audit/evidence/FIXER-L_before_after.txt`, `check_hostile_origin_cannot_mutate` BEFORE column).

### Ablation proof — the suite cannot be green for the wrong reason

`audit/scripts/FIXER-L_ablation.py` removes **one defence at a time** from the live module (runtime monkeypatch; the file on disk is never touched) and requires the relevant checks to notice. `audit/evidence/FIXER-L_ablation.txt`:

```
[ok  ] A. origin check removed, host/sec-fetch/preflight kept: all 4 expected check(s) noticed
[ok  ] B. host check removed -> check_dns_rebinding_is_blunted noticed
[ok  ] C. Sec-Fetch-Site check removed -> the check noticed
[ok  ] D. only the pre-fix module reflects a foreign Origin
[ok  ] control: all 9 checks pass with every defence restored
No ablation gaps
```

This directly answers the orchestrator's objection:

* **A** — removing the `Origin` rule alone (Host and `Sec-Fetch-Site` left intact) fails `check_hostile_origin_cannot_mutate`, `check_cross_origin_preflight_is_refused`, `check_mutating_request_without_origin_is_allowed` and `check_hostile_origin_refused_because_of_the_origin`.
* **B** — `check_dns_rebinding_is_blunted` was rewritten to prove the Host rule **in isolation** (a browser-driven request whose `Origin` and `Sec-Fetch-Site` are both allow-listed but whose `Host` is `evil.test`), because the realistic rebinding case is *also* caught by `Sec-Fetch-Site: same-site` and would otherwise have passed for the wrong reason. It now fails when only the Host rule is removed, and asserts the log says `is not a loopback name`.
* `check_hostile_origin_refused_because_of_the_origin` asserts on the **captured log**, not just the status code: the refusal must contain `Refused cross-origin` and must **not** contain `is not a loopback name`, for a loopback `Host`, for `TestClient`'s `testserver`, and for a rebound `Host` + `Origin` pair.

---

## 2. ABUSE-07 / SYNC-07 (MEDIUM) — unbounded `/api/blunders` params

**Root cause.** `limit: int = 50` was a bare `int` with no `ge`/`le` and no `offset`, bound straight into `LIMIT ?`. In SQLite a negative `LIMIT` means *no limit*, so `?limit=-1` dumped the whole blunder table (each row plus a parsed `refutation_pv`). A Python int above 2^63-1 is not a legal SQLite `INTEGER`, so `cursor.execute` raised an uncaught `OverflowError` → 500. `blunder_id: int` on the detail route had the same problem.

**Change — `backend/api.py:1311-1375`**
```diff
-    color: Optional[str] = None,
-    eco: Optional[str] = None,
-    min_loss: Optional[int] = None,
-    category: Optional[str] = None,
-    limit: int = 50
+    color: Optional[str] = Query(None, pattern="^(white|black)$"),
+    eco: Optional[str] = Query(None, max_length=8),
+    min_loss: Optional[int] = Query(None, ge=0, le=100000),
+    category: Optional[str] = Query(None, max_length=64),
+    limit: int = Query(50, ge=1, le=200),
+    offset: int = Query(0, ge=0, le=1_000_000)
```
```diff
-    if min_loss:
+    if min_loss is not None:          # so ?min_loss=0 is expressible
-    query += " ORDER BY b.cpl_loss DESC LIMIT ?"
-    params.append(limit)
+    query += " ORDER BY b.cpl_loss DESC, b.id DESC LIMIT ? OFFSET ?"
+    params.extend([limit, offset])
```

**Change — `backend/api.py:1515-1519`** (detail route; `Path` because this FastAPI version rejects `Query` for a path param)
```diff
-def get_blunder_detail(blunder_id: int):
+    blunder_id: int = Path(..., ge=SQLITE_INT64_MIN, le=SQLITE_INT64_MAX),
+    include_master_stats: bool = Query(True),
```

**Backstops** — `@app.exception_handler(OverflowError)` (`:245`) and `@app.exception_handler(RecursionError)` (`:233`) turn any remaining 500-class input error into a 4xx JSON body.

**Verified** — `check_blunder_list_is_bounded`, `check_blunder_id_out_of_int64_is_4xx`, `check_recursion_and_overflow_are_4xx`. Before: `limit=-1 -> 200 [{"id":6,…`, `id=9223372036854775808 -> 500`, `RecursionError -> 500: Internal…`. After: all 422/400 with a JSON body.

---

## 3. ABUSE-08 (MEDIUM) — nested JSON → uncaught `RecursionError` → 500

**Root cause.** Pydantic recurses through `Dict[str, Any]` / `Any` with no depth limit, and `RecursionError` was covered by no handler and there was no catch-all. A ~2 KB body of `[[[[…]]]]` produced a bare 500 on **every** POST-JSON route.

**Change.** Two layers:
1. **Reject before pydantic sees it** — `json_max_nesting_depth` (`:479-509`), a *string-aware* bracket scanner (a `[` inside a JSON string does not open a container, an escaped quote does not close one, so a PGN such as `[%clk 0:05:00]` in a study name is scored correctly), called from the boundary middleware at `:686-694` → **400**.
2. **Backstop** — `@app.exception_handler(RecursionError)` (`:233`) → **400**.
3. **Catch-all** — `@app.exception_handler(Exception)` (`:256-274`) so no route can ever answer a bare `500 Internal Server Error` from an unclassified exception. It lives in Starlette's `ServerErrorMiddleware`, i.e. *outside* the boundary middleware, so it attaches the security headers explicitly.

**Verified** — `check_deeply_nested_json_is_4xx` (depths 1000 / 20000 / 5000 on two different routes), plus a bracket-in-a-string case that must still be accepted. Before: `depth=1000 -> 500`. After: `400`, and a real shallow payload still returns 200.

---

## 4. ABUSE-09 / SYNC-14 (MEDIUM) — no body / field / list size caps

**Root cause.** `app.add_middleware` was called exactly once (for CORS) and every payload model used unbounded `str` / `List[Dict[str, Any]]`, so a 32 MB body was accepted and fully JSON-parsed, and a 4 MB study name / token was **persisted** into SQLite.

**Change.**
* `MAX_BODY_BYTES = 2 MiB` (`:32`), enforced at `:670-685` — the declared `Content-Length` is rejected with **413** up front, and the body is then counted **as it streams** (aborting mid-read, not after full buffering). Bodies are only read for methods that can have one, so long-lived SSE `GET`s are untouched. `DELETE` with a JSON body (`frontend/scout.js:2101-2109`) is verified working end to end.
* `MAX_JSON_DEPTH = 32` (`:37`) — see §3.
* Field and list caps on **every** payload model (`:733-810`): `MAX_LICHESS_TOKEN_LEN = 512` (a real Lichess token is 64 chars), `MAX_USERNAME_LEN = 64`, `MAX_API_KEY_LEN = 512`, `MAX_STUDY_NAME_LEN = 120`, `MAX_DESCRIPTION_LEN = 2000`, `MAX_COMMENT_LEN = 4000`, `MAX_ARROWS_LEN = 4000`, `MAX_FEN_LEN = 120`, `MAX_UCI_LEN = 8`, `MAX_SCOUT_SOURCE_LEN = 8192`, `MAX_SCOUT_LINES = 200`, `MAX_SCOUT_TREE_LINES = 500`, `MAX_SOURCE_ID_LEN = 128`, `MAX_OPENING_NAME_LEN = 200`; plus `search` on `/api/games/recent` (`:1072`) and `game_id` on `ImportGameOpeningRequest` (`:1062`).
* The one `Dict[str, Any]` body, `POST /api/repertoire/train/recent_opening` (`:1946-1970`), has its two persisted values length-checked inline → **400**.
* `eval_score` is bounded (`ge=-100000, le=100000`) so it cannot reach SQLite as an out-of-int64 int.

**Verified** — `check_oversized_body_is_413` (8 MB and 32 MB JSON, plus a 4 MB form-encoded body), `check_persisted_fields_are_capped` (7 routes × 4 MB field, then asserts nothing was written to `settings`), `check_unbounded_list_is_rejected` (2000 lines / 5000 moves). Before: `8MB -> 200`, `POST /api/settings with a 4 MB field` accepted. After: `413`/`422`, and the 100 KB opening name never reaches the `settings` table.

---

## 5. ABUSE-13 (MEDIUM) — security response headers

**Root cause.** `app.add_middleware` was called once (CORS) and the only custom middleware set `Cache-Control` for `.js`/`.css`/`.html`. No `CSP`, `X-Content-Type-Options`, `X-Frame-Options` or `Referrer-Policy` on any response.

**Change.** `SECURITY_RESPONSE_HEADERS` (`:376-395`), applied by `send_with_security_headers` in the boundary middleware (`:557-563`) to **every** response — including the `StaticFiles` mount, the 404/405 fall-through, the 403 refusals, and (explicitly) the `ServerErrorMiddleware` 500. `setdefault` is used, so a route that sets one deliberately is never clobbered.

Emitted: `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin`, `Permissions-Policy: camera=(), microphone=(), geolocation=(), interest-cohort=()`.

**Coordination with FIXER-E (not duplication, not weakening).** `electron/main.js:299-308` (`applySessionHardening`) **deletes** any server CSP for the trusted main frame and injects its own `APP_CSP`. The server directive set is therefore **byte-identical to `APP_CSP`**, so the shell's override is a no-op rather than a downgrade, and the server header is what protects the plain-browser path. `check_security_headers_present` asserts this; `FIXER-L_e2e_origin.py` §4 asserts the equality by parsing `electron/main.js`:

```
[ok  ] server CSP == electron APP_CSP (directive for directive)
[ok  ] the shell deletes any server CSP for the main frame before injecting
```

Both `<form>` elements in `index.html` (`#settingsForm`, `#scoutSearchForm`) are `preventDefault`-ed by their JS handlers (`app.js:26`, `scout.js:619`), so `form-action 'none'` does not break them — and this is already FIXER-E's shipped behaviour.

**Verified** — `check_security_headers_present` over `/`, `/api/dashboard`, `/api/settings` and a 404 path. Before: `/ is missing content-security-policy`. After: all present, with `script-src 'self'` (no `unsafe-inline` in `script-src`) and `frame-ancestors 'none'`.

---

## 6. SYNC-08 (MEDIUM) — blocking, un-cached explorer call per blunder view

**Root cause.** `get_blunder_detail` called `LichessClient().get_opening_explorer_stats(fen)` on **every** request: an un-cached, un-rate-limited, fully blocking `requests.get(timeout=8)` (a *second* one on a 401, so up to 16 s), issued from a `def` route — i.e. a worker thread in the shared anyio pool that also serves the SSE endpoints. `explorer_cache`, which every other explorer read in this app uses, was bypassed entirely.

**Change — `backend/api.py:1380-1512`.** `get_master_stats(fen)` with three layers, cheapest first:
1. **`explorer_cache`** (`_master_stats_from_explorer_cache`, `:1427`) — the table `get_candidate_moves_2000` writes, read-only, 7-day freshness, same 4-token `normalize_fen` key.
2. **In-process TTL memo** — 6 h for a positive result, **120 s for a negative one**, so a timeout/429 is never frozen into a permanent "no statistics" (this is the same lesson FIXER-A applied to `get_explorer_game_count`). Bounded at 512 entries.
3. **Single-flight lock** — N concurrent views of the same position make **one** outbound call.
Plus `?include_master_stats=false` to skip the network entirely.

**Verified** — `check_blunder_detail_explorer_is_cached` counts the outbound calls: **2 views of the same blunder ⇒ 1 call** (and `?include_master_stats=false` ⇒ 0). `check_blunder_detail_reads_explorer_cache` seeds `explorer_cache` and replaces `LichessClient` with a class that *raises*, so a cache miss would fail the test. Before: `the explorer was called 0 times for 2 views` → actually the pre-fix module has no memo at all (`no in-process explorer memo to reset`).

---

## 7. SYNC-10 (MEDIUM) — `max_games` unbounded on the analysis endpoints

**Root cause.** `max_games: Optional[int] = Query(None)`, and `LichessClient.fetch_user_games` treats a falsy value as *"send no `max` parameter at all"* — so **omitting** the parameter, which is exactly what `frontend/repertoire_builder.js:758` does, queued a depth-12 Stockfish analysis of the user's **entire** game history. The stream variant's `ge=1` rejected `0`/negatives only when passed *explicitly*.

**Change — `backend/api.py:2100-2125`**
```diff
-def sync_repertoire_games(max_games: Optional[int] = Query(None)):
-    """Fetches user's games (all games if max_games is None or 0) and analyzes opening blunders."""
+def sync_repertoire_games(max_games: int = Query(30, ge=1, le=100)):
-    max_games: Optional[int] = Query(None, ge=1)
+    max_games: int = Query(30, ge=1, le=100)
```
The default of 30 matches the progress bar's own `?max_games=30` on the sibling `/api/games/sync_recent_stream`.

**Verified** — `check_analysis_max_games_is_bounded` substitutes a recording `RepertoireManager` and asserts every forwarded value is `1 <= v <= 100`, that both routes default to a bounded value, and that `0` / `-5` / `999999999` are 422 on both. Before: `AssertionError: 0` (i.e. `None`/unbounded was forwarded).

---

## 8. SYNC-09 (LOW) — dashboard reports one arbitrary ECO per group

**Root cause.** `SELECT opening_name, eco, color, COUNT(*), AVG(cpl_loss) … GROUP BY opening_name, color` — `eco` is a bare column, so SQLite returns it from an arbitrary row of the group, and `ORDER BY blunder_count DESC LIMIT 10` had no tie-break, so equal-count openings could swap places between requests.

**Change — `backend/api.py:1239-1266`**
```diff
-    SELECT opening_name, eco, color, COUNT(*) as blunder_count, AVG(cpl_loss) as avg_loss
+    SELECT opening_name,
+           (SELECT b2.eco FROM blunders b2
+            WHERE IFNULL(b2.opening_name,'') = IFNULL(blunders.opening_name,'')
+              AND IFNULL(b2.color,'')       = IFNULL(blunders.color,'')
+            ORDER BY b2.cpl_loss DESC, b2.id DESC LIMIT 1) AS eco,
+           color, COUNT(*) as blunder_count, AVG(cpl_loss) as avg_loss
     FROM blunders
     GROUP BY opening_name, color
-    ORDER BY blunder_count DESC
+    ORDER BY blunder_count DESC, opening_name ASC
```
The ECO is now the one belonging to the group's **worst** blunder — deterministic (`cpl_loss DESC, id DESC` is a total order) and meaningful ("this is the ECO where you lose most in this opening"). **The row count is unchanged**: `eco` is still *not* part of the grouping key, so the panel returns exactly the same number of rows and the same `blunder_count` values as before.

**Verified** — `check_dashboard_eco_is_deterministic` seeds a group with `B20` (100 cp) inserted **first** and `B90` (900 cp) second, then asserts 6 consecutive requests all report `B90`. That insertion order is deliberate: it is what makes the pre-fix bare-column query report the *wrong* ECO. Before: `reported eco 'B20' is not the ECO of the worst blunder in the group (B90)`.

---

## 9. BUILDER-12 (LOW) — chess960/Shredder-FEN accepted into a standard repertoire

**Root cause.** `is_valid_fen` delegated entirely to `chess.Board(fen).is_valid()`, and `python-chess` accepts Shredder-FEN castling notation (`AHah` = "White's king-side rook is on the H file") as a synonym for standard notation. A chess960 position was accepted, moves were validated against it, and the stored row came back with a **standard** castling field — a different game smuggled into a standard repertoire, which `backend/db.py`'s boot reachability walk then counts as an orphan forever (it starts from the standard start FEN).

**Change — `backend/api.py:50-85`.** The castling field is now checked explicitly against `STANDARD_CASTLING_CHARS = frozenset("KQkq-")` (`:47`), the field count is capped at 6, and the halfmove/fullmove counters (when present) must be short non-negative integers.

**Verified** — `check_chess960_fen_is_rejected` asserts `is_valid_fen("r3k2r/… w AHah - 0 1") is False`, that `POST /api/repertoire/move` and `POST /api/repertoire/move_meta` both answer 400 and write nothing, that `GET /api/repertoire/eval_stream` answers 400, **and** that four real standard FENs (including the 6-field form, castling rights present, and a castling-free king-and-pawn endgame) are still accepted. Before: `Shredder-FEN was accepted`.

---

## 10. Hand-offs implemented from finished fixers

### From FIXER-C (`audit/patches/FIXER-C-01.md` §6)
1. **§6.1 — close the sync pool on shutdown** (`backend/api.py:730-737`). `get_sync_engine_pool().close_all()` now runs next to `await evaluator.close()`, in a `try/except` so a failure can never block shutdown.
2. **§6.2 — `EngineBusy` → 503 + `Retry-After`**, in two places, importing `EngineBusy` from `backend.evaluator` (`:23`):
   * `POST /api/sync` (`:1023-1033`) — a distinct `except EngineBusy` placed **before** the generic per-game `except Exception`, so a saturated pool is a retryable 503 rather than a 300 s block counted as a failed game.
   * `POST /api/train/evaluate_move` (`:1721-1734`) — placed **before** the generic `except`, and the SQLite connection is closed before raising, so a drill attempt is **not** recorded as a failed attempt in `drill_reviews`.

   Verified by `check_engine_busy_is_503` (both routes → 503 + `Retry-After: 5`) and `check_shutdown_closes_the_sync_pool`.

### From FIXER-F (item 16)
`game_gaps.gaps[].all_blunders[]` / `earlier_mistake` carrying `best_san` / `cpl_loss` is **already satisfied** by FIXER-A's `repertoire.py` rewrite (`backend/repertoire.py:1490` already projects `best_san, …, cpl_loss` in that query). Those rows are built in `repertoire.py`, which is not my file, so there was nothing for me to change in `api.py`. What I did do is **lock the contract in**: `check_game_gaps_rows_carry_best_san_and_cpl_loss` is a non-vacuous regression guard (it seeds two blunders in one game, so `all_blunders` is multi-row and `earlier_mistake` is non-null, and fails if fewer than three chained rows are exercised).

---

## 11. Full-suite result

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
Ran 426 tests in 20.749s
FAILED (failures=2)
```

**The 2 remaining failures are not mine and are not caused by this patch.** Both are in `tests/test_scout_frontend_contracts.py` and both are *frontend source-string assertions*:

* `test_import_reports_imported_count_and_errors` — asserts the literal `if (data.status !== 'success')` appears in `frontend/scout.js`. That code has since been refactored by the frontend owner to `const okStatus = data.status === 'success' || data.status === 'already_present';` followed by `if (!okStatus) {`. The behaviour is strictly better; the test's literal is stale.
* `test_node_contract_harness` — the same module's Node harness provenance check.

That module contains **zero** occurrences of `Origin`, `Sec-Fetch` or any of the boundary helpers, and it fails identically in isolation:

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_scout_frontend_contracts
FAIL: test_node_contract_harness
FAIL: test_import_reports_imported_count_and_errors
Ran 38 tests in 2.542s
FAILED (failures=2)
```

Owners: `frontend/scout.js` (frontend) and `tests/test_scout_frontend_contracts.py` (tests).

Isolated, my own modules plus FIXER-B's contract tests — **green**:

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_api tests.test_api_contract \
    tests.test_api_security tests.test_user_workflows tests.test_user_behavior_probe
Ran 36 tests in 4.906s
OK
```

Earlier in the wave the full suite reached `Ran 408 … OK` **twice consecutively** with all of my changes in place; the count has since risen to 426 as other fixers landed their tests.

**Earlier in-flight defect, found by the orchestrator and fixed (worth recording).** An intermediate revision applied the `Host` rule to *every* request. Starlette's `TestClient` sends `Host: testserver`, so that refused **51 unrelated tests** — including plain mutating requests carrying no `Origin` at all — and, worse, made the hostile-origin case pass for the wrong reason (the log blamed the Host). Fixed by (1) scoping the `Host` rule to browser-driven requests (`backend/api.py:646-650`, rationale at `:300-320`) and (2) evaluating it **after** the `Origin` rule so the log names the real cause. `check_mutating_request_without_origin_is_allowed` and `check_hostile_origin_refused_because_of_the_origin` are the permanent regression guards, and `audit/scripts/FIXER-L_ablation.py` proves neither can pass vacuously.

I did **not** weaken or delete any existing test. `tests/test_api_contract.py` (FIXER-B) is green. `tests/test_docs_contract.py` was transiently red while another agent was mid-edit on `AGENTS.md`; it has since passed.

The one `AGENTS.md` line that *is* about my file I did correct, because AGENTS.md rule 5 ("Documentation Is Part of the Build") makes a false claim a live hazard:

```diff
-    ├── test_api_security.py             # Injection, traversal, resource-exhaustion probes
+    ├── test_api_security.py             # Local-origin/CSRF boundary, request-size + field caps, security headers, ablation proof
```

---

## 12. Hand-offs owed to other owners

### For the frontend / tests owners — the 2 remaining suite failures
`tests/test_scout_frontend_contracts.py::test_import_reports_imported_count_and_errors` asserts the literal string `if (data.status !== 'success')` is present in `frontend/scout.js`. The import path has since been rewritten to `const okStatus = data.status === 'success' || data.status === 'already_present'; if (!okStatus) {…}` — behaviourally stricter, and it is still correct against `backend/api.py` (which returns `okStatus` values unchanged; FIXER-B only changed `/api/repertoire/seed`, not `/api/scout/import_repertoire`). **The test's literal needs updating, or the assertion re-pointed at `okStatus`** — the check is right, the string it greps for is stale. `test_node_contract_harness` in the same module fails alongside it. Neither involves `backend/api.py`.

### For the frontend owners (`frontend/**`)1. **`GET /api/blunders?limit=` is now capped at 200** and `offset` was added. Any caller that relied on `?limit=-1` (or `limit > 200`) to fetch everything now gets a **422**. The frontend does not call this route today (`grep -rho "/api/[a-zA-Z0-9_/{}$.-]*" frontend/*.js` does not list it), so this only affects a restored blunder-review UI: page with `?limit=N&offset=M` instead.
2. **`GET /api/blunders/{id}` accepts `?include_master_stats=false`** to skip the explorer lookup entirely.
3. **`POST /api/repertoire/sync_games_stream` now defaults to `max_games=30`** (was: the whole history when the parameter is omitted, which is what `repertoire_builder.js:758` does). If the "Sync Games" button should analyse more or all games, pass `?max_games=N` explicitly — the stream already reports per-game progress, so the UI needs no change.
4. **Optional, for defence in depth: send a custom header on mutating requests** (e.g. `X-Chess-Client: 1`). This is *not* required — the `Origin` boundary already blocks every cross-origin write, and the preflight is refused — but it is a cheap second layer. It is the one part of the CSRF design I deliberately did **not** ship, because it is only enforceable if `frontend/**` and `electron/**` change, and both belong to other fixers in this wave.
5. `POST /api/settings` now rejects a token longer than 512 characters, a username longer than 64, etc. Nothing the UI produces comes close.
6. `POST /api/scout/check_tree` accepts at most 500 lines and `check_line`/`import_repertoire` at most 200 moves per line. A scouted opening line is well under 200 plies; a session tree with more than 500 branches should be split client-side.

### For `backend/repertoire.py` (FIXER-A)
1. `GET /api/repertoire/game_gaps` publishes a contract the frontend now depends on: **`gaps[].all_blunders[]` and `gaps[].earlier_mistake` must project `best_san` and `cpl_loss`** (alongside the top-level `gaps[]` row). Your `:1490` already does; `tests/test_api_security.py::check_game_gaps_rows_carry_best_san_and_cpl_loss` will fail if that ever regresses.
2. `is_valid_fen` in `backend/api.py:50-85` is now **strictly standard-chess** (it rejects Shredder-FEN/chess960 castling fields and FENs with more than 6 tokens). `backend/repertoire.py:471-476` has its own copy of the check — it should get the same treatment, or better, import the one from `api.py`, so the two layers cannot disagree.

### For the Electron owner (`electron/**`, FIXER-E)
Nothing is required. `backend/api.py` now emits a `Content-Security-Policy` that is **byte-identical** to `APP_CSP` in `electron/main.js:66-79`, and `applySessionHardening` still deletes the server's CSP for the trusted main frame and injects its own. If `APP_CSP` ever changes, the server copy in `SECURITY_RESPONSE_HEADERS` (`backend/api.py:376-386`) must change with it; `FIXER-L_e2e_origin.py` §4 asserts the equality.

### For the DB owner (`backend/db.py`, FIXER-P0)
Nothing required. I added no columns and no migrations. `explorer_cache` is read (never written) by `_master_stats_from_explorer_cache` (`backend/api.py:1427-1466`) using the same `(fen, ratings)` key and `data_json` shape `get_candidate_moves_2000` already writes, so any normalisation of that key must keep the 4-token `normalize_fen` form.

---

## 13. Residual risks / not fixed

1. **No authentication.** A non-browser client on the machine can call anything. That is a product decision, out of scope, and documented in the module comment (`backend/api.py:283-284`) and in §1 above.
2. **A required custom header on mutating requests** is not shipped (§12.1). The `Origin` + `Sec-Fetch-Site` + preflight-refusal combination is what is enforced, and the ablation proves each rule is independently covered.
3. **The `Host` rule cannot fire for a request with no browser markers** (no `Origin`, no `Sec-Fetch-*`). That is intentional and documented (`backend/api.py:300-312`): such a request is a direct client, and DNS rebinding requires a browser.
4. **`Origin: null` is refused**, so a `file://`-hosted copy of the UI would not work. `electron/main.js` loads over `http`, so the shipped shell is unaffected. If the project ever wants a `file://` build, it must be added to the allow-list explicitly.
5. **Body buffering.** The middleware reads a mutating request's body in full (bounded at 2 MiB) before dispatching, in order to enforce the cap and the nesting depth before pydantic sees them. A client that opens a connection, sends headers and never sends the body now blocks in the middleware rather than in the route. For a loopback single-user app with a 2 MiB cap this is not a concern, and the alternative (streaming depth analysis) would be far more complex for no real gain.
6. **`/api/scout/check_tree` still fans out O(lines × repertoire rows)** for the lines that *are* accepted. The 500-line cap bounds it, and the scout layer has its own cap, but the underlying cost is in `repertoire.py` and is not mine to change.
7. **The `explorer_cache` freshness window for master stats is 7 days** (matching `get_candidate_moves_2000`) while the in-process memo is 6 h. The two are intentionally different: the memo protects against per-view repetition, the table against re-fetching a position the app has already paid for.
8. **`min_loss` is now capped at 100 000 cp.** A stored `cpl_loss` above that would become unreachable through this filter — no real cpl_loss is anywhere near it, but it is a new bound that did not exist before.
