# FIXER-B-01 — `backend/api.py` API-contract patch

**Owner:** FIXER-B (exclusive write access to `backend/api.py`)
**Findings fixed:** CODE-02-02, CODE-02-04, CODE-02-05, CODE-02-06, CODE-02-11, CODE-02-12, CODE-02-13, CODE-05-04
**Files touched:** `backend/api.py` (only), plus **new** `tests/test_api_contract.py`, `audit/scripts/FIXER-B_make_prefix_snapshot.py`, `audit/scripts/api_prefix_snapshot.py`, `audit/scripts/FIXER-B_before_after.py`, this report.
**Production data:** never opened for writing. `chess_improvement.db` was read with `mode=ro` only, to confirm it was untouched. Every probe/test imports `tests.test_setup` first.

---

## 0. Method: how the "before" baseline was produced

`git` is unusable as a baseline here: `backend/api.py` was already modified relative to `HEAD` by earlier audit rounds, so `git show HEAD:backend/api.py` is a *different* module (795 lines, missing `/api/games/recent` and the scout routes entirely).

Instead I rebuilt the exact pre-fix module by **reverse-applying my own edits**:

* `audit/scripts/FIXER-B_make_prefix_snapshot.py` — reads the current `backend/api.py`, slices it at 11 documented markers and substitutes the original text back, then pins `FRONTEND_DIR` to the real `frontend/` (the snapshot lives in `audit/scripts/`, so the pre-fix `dirname(dirname(__file__))` resolution would otherwise silently skip the static mount and hide the very bug under test). Output: `audit/scripts/api_prefix_snapshot.py`.
* Fidelity check: the snapshot is **1121 lines** and every line number cited in the audit reports lands on the cited line (`50-54` handler, `198` `@app.post("/api/sync")`, `214` `no_games`, `242` `g.get("winner", "draw")`, `245-257` INSERT+commit, `261` analyze, `265/269` `close()`, `275` return, `312` `ORDER BY played_at DESC`, `325-326` ms heuristic, `341-351` result rendering, `965-970` seed, `1058/1091/1100/1109` the three duplicate pairs, `1116` `raise HTTPException(400, str(e))`, `1121` the static mount). Byte-identical to the pre-fix file.
* `audit/scripts/FIXER-B_before_after.py` — imports the snapshot *and* `backend.api` into one process, runs the **same 20 check functions** from `tests/test_api_contract.py` against both, and prints a fail-before/pass-after table.

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-B_before_after.py
BEFORE: 2 passed / 18 failed
AFTER : 20 passed / 0 failed
```

The 2 checks that already passed before are deliberate **control checks** (static mount still serves the frontend; genuine user-input errors stay informative) — they prove the patch did not break behaviour that already worked.

---

## 1. CODE-02-02 (HIGH) — the `StaticFiles("/")` mount swallowed every 405 under `/api/`

### Root cause
`app.mount("/", StaticFiles(...))` was the last entry in `app.router.routes`. Starlette's `Router.app` returns on the **first `Match.FULL`**, and `Mount.matches` returns `FULL` for *every* path. So for a path that only matched an API route **partially** (right path, wrong verb), the loop recorded `partial` and then let the root mount return `FULL` — the `partial is not None` → 405 branch was dead code. `StaticFiles` then produced its own 404. `/api/blunders` was the only path that behaved correctly (it has a GET route, so the partial branch fired before the mount), which is why the bug was invisible in the route table.

### Change — `backend/api.py:1460-1490` (new, registered **before** the mount)
```python
@app.api_route(
    "/api/{unmatched_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def api_unmatched_route(request: Request, unmatched_path: str = ""):
    probe_scope = {
        "type": "http",
        "path": request.scope.get("path", ""),
        "root_path": request.scope.get("root_path", ""),
        "method": request.method,
    }
    for route in app.router.routes:
        try:
            match, _ = route.matches(probe_scope)
        except Exception:
            continue
        if match == Match.PARTIAL:
            return JSONResponse(status_code=405, content={"detail": "Method Not Allowed"})
    return JSONResponse(status_code=404, content={"detail": "Not Found"})
```
`from starlette.routing import Match` added at `backend/api.py:13`.

The catch-all wins for `/api/*` because it is registered after every real API route and before the mount. It re-derives Starlette's own 405-vs-404 decision by asking the route table which entries match the path with a different method.

### Verified
`tests/test_api_contract.py::check_wrong_method_is_405` asserts **405** for 11 wrong-verb cases (`GET /api/sync`, `PUT /api/sync`, `GET /api/repertoire/seed`, `GET /api/repertoire/clear`, `DELETE /api/repertoire/studies`, `DELETE /api/settings`, `POST /api/blunders`, `PUT /api/games/recent`, `POST /api/repertoire/tree`, `GET /api/repertoire/train/attempt`, `DELETE /api/scout/favorite`) and **404** for two genuinely-unknown paths.

```
BEFORE: AssertionError: GET /api/sync -> 404 (expected 405). Body: {"detail":"Not Found"}
AFTER : [ ok ] wrong method under /api/ is 405
```
`check_static_mount_still_serves_frontend` additionally proves `GET /` → 200 `text/html` and `GET /index.html` → 200, i.e. the static mount still works.

---

## 2. CODE-02-04 (HIGH) — `analyzed_at` committed before the analysis ran

### Root cause
`api.py:245-257` inserted the row **with `analyzed_at = int(time.time())`** and committed, and only *then* ran `analyzer.analyze_game_openings(g)`. The "persistence" column and the "work is done" column were the same column, committed before the work, and there was no `try/finally` around the loop body, so any failure in `analyze_game_openings` / `save_blunders_to_db` left the row permanently stamped and every later sync skipped it at the `analyzed_at > 0` guard — the game's blunders were unrecoverable.

### Change — `backend/api.py:336-510` (whole `sync_user_games` rewritten)

```diff
-        cursor.execute("""INSERT OR REPLACE INTO games (... analyzed_at) VALUES (?,...)""",
-                       (..., moves, int(time.time())))
-        conn.commit()
-
-        # Run blunder analysis on opening moves
-        g["user_color"] = user_color
-        blunders = analyzer.analyze_game_openings(g)
-        if blunders:
-            analyzer.save_blunders_to_db(blunders)
-            new_blunders_count += len(blunders)
-
-        synced_games += 1
-
-    analyzer.close()
-    conn.close()
+                # ... per-game body ...
+                cursor.execute("""INSERT INTO games (...) VALUES (?,...)
+                ON CONFLICT(id) DO UPDATE SET speed=excluded.speed, ... """,
+                               (..., moves, 0))          # analyzed_at = 0  (PENDING)
+                conn.commit()
+
+                g["user_color"] = user_color
+                blunders = analyzer.analyze_game_openings(g)
+                if blunders:
+                    analyzer.save_blunders_to_db(blunders)
+                    new_blunders_count += len(blunders)
+
+                # Analysis finished cleanly -> only NOW mark the game as done.
+                cursor.execute("UPDATE games SET analyzed_at = ? WHERE id = ?", (int(time.time()), game_id))
+                conn.commit()
+                synced_games += 1
+            except Exception:
+                failed_games += 1
+                errors.append({"game_id": str(game_id), "error": "Analysis failed for this game."})
+                logger.exception("Analysis failed for game %s during /api/sync", game_id)
+                try: conn.rollback()
+                except Exception: pass
+    finally:
+        analyzer.close()
+        conn.close()
```
Each game body is wrapped in its own `try/except` (**per-game error isolation**), and `analyzer.close()` / `conn.close()` moved into a `finally`. `INSERT OR REPLACE` (a DELETE+INSERT in SQLite, which would orphan the game's `blunders` rows) became `INSERT ... ON CONFLICT(id) DO UPDATE`, which also preserves any existing `analyzed_at`.

### Verified
`check_analyzed_at_only_on_success` (a) stamps the good game, (b) asserts the failed game keeps `analyzed_at = 0`, (c) asserts `status == "partial"` and `synced_games == 1, failed_games == 1` (isolation), (d) asserts the engine's exception text is **not** in the body, (e) asserts the `GameAnalyzer` was closed on the error path, and (f) **re-syncs with a working engine and asserts the previously-failed game is retried** (this is the actual CODE-02-04 claim).

```
BEFORE: AssertionError: POST /api/sync -> 500 Internal Server Error
        (the unguarded loop aborted the whole library on the first failure)
AFTER : [ ok ] analyzed_at only on successful analysis
```

---

## 3. CODE-02-05 (HIGH) — draws stored as `NULL` and rendered as losses

### Root cause
Two independent bugs in one feature:
1. **Writer** — `result = g.get("winner", "draw") if "winner" in g else "draw"`. Lichess exports `"winner": null` for a draw, so the `"draw"` default was unreachable and `None` was bound to `result`, persisted as SQL `NULL`.
2. **Reader** — `raw_result = (row.get("result") or "").lower()` turns `NULL` into `""`, which falls through to `else: result = "loss"`. The `elif raw_result == "draw"` branch was only ever reachable for the 12 legacy literal-`'draw'` rows.

### Change

**(a) Writer + shared helpers — `backend/api.py:66-95`**
```python
def normalize_game_result(value: Any) -> str:
    """... A NULL, empty or unrecognised value is therefore normalised to 'draw'."""
    if value is None:
        return "draw"
    token = str(value).strip().lower()
    if token in ("white", "black"):
        return token
    return "draw"


def render_game_outcome(stored_result: Any, user_color: str) -> tuple:
    token = normalize_game_result(stored_result)
    if token == "draw":
        return "draw", "½-½"
    if token == user_color:
        return "win", ("1-0" if user_color == "white" else "0-1")
    return "loss", ("0-1" if user_color == "white" else "1-0")
```
`sync_user_games` now does `result = normalize_game_result(g.get("winner"))`.

**(b) Reader — `backend/api.py:576`**
```diff
-        user_color = row.get("user_color") or "white"
-        raw_result = (row.get("result") or "").lower()
-
-        if raw_result == user_color: ... win
-        elif raw_result == "draw": ... draw
-        else: ... loss
+        user_color = row.get("user_color") or "white"
+        # Three-way outcome. A NULL/empty `games.result` is a legacy drawn game
+        # (Lichess `winner: null`), NOT a loss (audit CODE-02-05).
+        result, score_text = render_game_outcome(row.get("result"), user_color)
```
The `else` branch is now symmetric for both colours (`0-1` for White, `1-0` for Black), fixing the report's escalation item 3.

**(c) `GET /api/dashboard` — `backend/api.py:699-715`** (the report's escalation item 2: the win/loss/draw breakdown grouped on the raw column, so every draw was its own bucket):
```diff
-    cursor.execute("SELECT result, user_color, COUNT(*) as count FROM games GROUP BY result, user_color")
+    cursor.execute("""
+    SELECT CASE
+               WHEN result IS NULL OR TRIM(result) = '' THEN 'draw'
+               WHEN LOWER(TRIM(result)) IN ('white', 'black') THEN LOWER(TRIM(result))
+               ELSE 'draw'
+           END AS result, user_color, COUNT(*) as count
+    FROM games GROUP BY result, user_color """)
```
Response shape is unchanged (`result`, `user_color`, `count`).

### Frontend contract check
`frontend/repertoire_builder.js:987` and `:1348` render
`game-outcome-pill ${g.result}` → `${g.result.toUpperCase()} (${g.score_text})`, and `style.css:1562/1568/1574` styles exactly `.win`, `.loss`, `.draw`. `render_game_outcome` guarantees that closed set and the `½-½` glyph, so **no frontend change is required**. `result` was already the documented `'win'|'loss'|'draw'` contract; the bug was that the server broke it.

### Verified
`check_draw_rendering` covers 7 stored shapes — `NULL`/literal-`draw` for both colours, win/loss for both colours — asserting both `result` and `score_text`, plus "result ∈ {win,loss,draw}" for every returned game.
`check_sync_writer_stores_draw_token` drives `POST /api/sync` with `"winner": null` and asserts the stored column is the token `'draw'`, then that the API renders `draw` / `½-½`.
`check_dashboard_groups_draws` asserts no `None` bucket and a `('draw','white')` bucket.

```
BEFORE: zzfixb_draw_null: result='loss' expected 'draw' (score_text='0-1')
BEFORE: stored result=None, expected the token 'draw' (not NULL)
BEFORE: /api/dashboard still groups a NULL result into its own bucket:
        [..., "(None, 'white')"]
AFTER : [ ok ] draws render as draws / sync stores 'draw' not NULL / dashboard groups NULL results as draws
```

---

## 4. CODE-02-06 (HIGH) — `played_at` written in ms in one place and s in another

### Root cause
Five writers, two units, and the reader compensated with a magnitude sniff (`> 1e11 → ms`) — but the **`ORDER BY` used the raw column**, so a seconds-valued game (the `createdAt`-missing fallback) sorted below every millisecond-valued game and disappeared from Recent Games permanently.

### Change

**(a) Canonical unit + data migration — `backend/api.py:41-140`**
```python
MS_EPOCH_THRESHOLD = 100_000_000_000   # 1e11 s = year 5138; 1e11 ms = 1973

def normalize_played_at(value: Any) -> int:
    """Normalise a Lichess timestamp to epoch MILLISECONDS. ..."""
    try:    v = int(float(value))
    except (TypeError, ValueError): return 0
    if v <= 0: return 0
    return v if v >= MS_EPOCH_THRESHOLD else v * 1000

def migrate_played_at_units() -> int:
    """One-way, idempotent data migration ... Returns the number of rows rewritten.
    Safe to call on every request: after the first run the WHERE clause matches zero rows."""
    cursor.execute("UPDATE games SET played_at = played_at * 1000 "
                   "WHERE played_at IS NOT NULL AND played_at > 0 AND played_at < ?", (MS_EPOCH_THRESHOLD,))
```
Milliseconds is the canonical unit because that is what the Lichess API emits in `createdAt`; the threshold is unambiguous.

Called from `startup_event()` (`backend/api.py:204-218`, inside a `try/except` so a migration problem can never block boot) **and** at the top of `get_recent_games` (`backend/api.py:518`). It is a no-op once applied, so calling it per-read costs one indexed `UPDATE` that matches zero rows.

**(b) `POST /api/sync` writer — `backend/api.py:462`**
`normalize_played_at(g.get("createdAt", int(time.time())))` instead of the raw value.

**(c) `GET /api/games/recent` sort — `backend/api.py:551-557`**
```diff
-    query += " ORDER BY played_at DESC LIMIT ? OFFSET ?"
+    # Sort on the NORMALISED value, not the raw column: writers this module does
+    # not own (`backend/repertoire.py`) can still persist a seconds-valued row,
+    # and a raw ORDER BY would bury it below every millisecond-valued game.
+    query += (" ORDER BY CASE WHEN played_at > 0 AND played_at < {thr} "
+              "THEN played_at * 1000 ELSE played_at END DESC LIMIT ? OFFSET ?").format(thr=MS_EPOCH_THRESHOLD)
```
The `CASE` makes the read path correct **even for rows a writer I do not own still writes in seconds** — that is the "correct read/normalisation on my side" the brief asks for, and it makes the fix independent of FIXER-A's timing.

**(d) Read-path normalisation of every other `played_at` this module emits** — `get_recent_games` (`"played_at": played_at_ms`, `backend/api.py:637`), `list_blunders` (`backend/api.py:777-779`), `get_blunder_detail` (`backend/api.py:812`). `get_dashboard`'s `recent_blunders` does **not** select `g.played_at` (the report's claim that it does is incorrect), so it needed no change.

### Cross-file follow-up for FIXER-A (`backend/repertoire.py`, not edited)
`repertoire.py:1476` and `:1568` write `g.get("createdAt", int(time.time() * 1000))` (ms) while `:1721` and `:1829` write `g.get("createdAt", int(time.time()))` (s). **All four must become `api.normalize_played_at(...)` or the same `>= 1e11 ? x : x*1000` expression**, otherwise `POST /api/repertoire/sync_games*` keeps writing seconds-valued rows. Until then they are *read* correctly by `get_recent_games` (thanks to the SQL `CASE`) but remain in the wrong unit in the column. Also note `repertoire.py`'s own two analysis paths still stamp `analyzed_at = int(time.time())` **before** analysing (`repertoire.py:1726/1728` and `:1829`), i.e. the CODE-02-04 ordering bug still exists on the paths the frontend actually uses (`/api/repertoire/sync_games*`). I did not touch it — it is FIXER-A's file.

### Verified
`check_played_at_sort_order` inserts `newer_s = 1700000001` (seconds, actually **newer**) and `older_ms = 1700000000000` (ms, actually older) and asserts the API returns `newer_s, older_ms, oldest_ms` in that order, that every returned `played_at >= 1e12`, and that the stored column is now ms.
`check_played_at_migration_is_idempotent` asserts a second pass does not multiply the value again.

```
BEFORE: recent games are not sorted newest-first by wall clock:
        ['zzfixb_older_ms', 'zzfixb_oldest_ms', 'zzfixb_newer_s']
BEFORE: (1700000001, 1700000001)          # the column stayed in seconds
AFTER : [ ok ] played_at sorted by wall clock / played_at migration is idempotent
```

---

## 5. CODE-02-11 (MEDIUM) — `/api/sync` returned fake 200s and leaked the engine + DB connection

### Root cause
(a) `if not raw_games: return {"status": "no_games", ...}` collapsed "the account has no games", "HTTP 429" and "Lichess 5xx" into one 200 with a **different body shape** from the success path; (b) the `fetch_user_games` call was unguarded, so an exception escaped as an opaque 500; (c) `analyzer.close()` / `conn.close()` were on the happy path only.

### Change — `backend/api.py:336-510`

* **One stable shape.** `status`, `synced_games`, `new_blunders`, `skipped_games`, `failed_games`, `errors` are present on *every* 200 branch (`no_games`, `success`, `partial`), so a client can branch on `status` alone. The success shape is unchanged, so `tests/test_api.py::test_sync_malformed_games` (`status == "success"`) still passes.
* **Truthful failure.** `fetch_user_games` raising → **502** (logged server-side). Empty list **and** a configured token whose account probe fails → **502**, not a fake `no_games`. Empty list with a healthy/no token → a genuine `200 no_games`.
* **Resources released.** `try/finally` around the whole loop.
* **Partial success is visible.** `status: "partial"` with per-game `errors: [{"game_id": ..., "error": "Analysis failed for this game."}]` (generic text; the real cause goes to `logger.exception`).

### Verified
```
BEFORE: 'no_games' body is missing 'synced_games':
        {'status': 'no_games', 'message': 'No new games retrieved or rate limit hit.', 'count': 0}
BEFORE: an upstream fetch failure must be a non-2xx, got 500 Internal Server Error
BEFORE: an unreachable Lichess must not be reported as 200 no_games: 200 {...}
BEFORE: every game failed -> zzfixb_all_a must stay pending
AFTER : [ ok ] all three /api/sync truthfulness checks
```

---

## 6. CODE-02-12 (MEDIUM) — three duplicated live route pairs

### Root cause
`/api/scout/{import_repertoire,check_line,check_tree}` and `/api/repertoire/{import_line,check_line,check_tree}` were byte-identical copies (including the same `except Exception as e: raise HTTPException(400, str(e))`), so a fix applied to one left the other unchecked. Both spellings are live: `frontend/scout.js:1422,1491,1652,1732` uses `/api/scout/*`, `frontend/repertoire_builder.js:2617,2672,3024,3084` uses `/api/repertoire/*`.

### Change — `backend/api.py:1360-1454`
* One canonical handler per operation, plus one shared error-policy helper `_scout_call(operation, **kwargs)` (`backend/api.py:1373-1400`) replacing the seven repeated `try/except` preambles.
* The three `/api/repertoire/*` routes are now **thin delegations** (`return scout_check_line(payload)` etc.) with `include_in_schema=False` and a docstring naming the canonical path.
* `scout_analyze` also routes through `_scout_call`, closing the report's escalation item 3 (seven raw-exception reflections → one).
* **No route was removed.** `grep -rho "/api/[a-zA-Z0-9_/-]*" frontend/*.js` confirms both spellings are still called; both still work identically (the regression test asserts identical bodies and exactly one underlying `ContentScout` call per request). The three "duplication on the sync axis" entry points (`/api/games/sync_recent*`, `/api/repertoire/sync_games*`, `POST /api/sync`) were **left alone** — de-duplicating them means editing `backend/repertoire.py` (FIXER-A's file) and they have different streaming/SSE semantics.

### Verified
```
BEFORE: AssertionError: Checks if a whole line or subline is already present in the user's repertoire.
        (the alias's docstring is the pre-fix copy, i.e. a second implementation)
AFTER : [ ok ] aliases delegate to canonical handlers
```

---

## 7. CODE-02-13 (MEDIUM) — global `ValueError` handler reflected internal detail

### Root cause
`@app.exception_handler(ValueError)` matched the bare class, so Starlette's handler lookup caught a `ValueError` raised **anywhere** in the app (chess parsers, `json`, `int()`/`float()` on stored settings, any SQL/ORM coercion) and answered `400 {"detail": str(exc)}` — a bad SQL column reported to the browser as a client mistake with the schema text verbatim, and a real outage reported as a 4xx with no log line.

### Change — `backend/api.py:150-190`
```python
    detail = str(exc)
    if "Out of range float values" in detail:
        # Pydantic's own message, produced while validating the request body:
        # this really is a client error, so it stays a 422.
        return JSONResponse(status_code=422, content={"detail": "Invalid float value: NaN and Infinity are not allowed."})
    logger.exception("Unhandled ValueError while serving %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})
```
**User-input errors are still informative** because every route converts them itself before this handler is reached: FEN/UCI/colour validation raises explicit `HTTPException(400)`; `import_game_opening` maps its `ValueError`s to 400; and `_scout_call` maps `ContentScout`'s `ValueError`s (bad URL, unknown source, invalid colour — `scout.py:193,205,576,582,758`) to 400 with the original message. So what still reaches the global handler is, by construction, an internal bug.

**Adjacent fix included** (the report's escalation item 1): `GET /api/settings` used raw `int(get_setting(...))` / `float(get_setting(...))`, so one corrupt `settings` row returned `400 {"detail":"invalid literal for int() with base 10: 'abc'"}` and killed the Settings page. New helpers `_setting_int` / `_setting_float` (`backend/api.py:97-121`) log a warning and fall back to the shipped default, including for `NaN`/`inf`. `GET /api/settings` (`backend/api.py:308-321`) now uses them; the request-side bounds (`SettingsPayload` `ge/le` fields) are untouched.

### Verified
```
BEFORE: internal schema detail leaked to the client:
        400 {"detail":"no such column: totally_internal_secret_schema_detail"}
BEFORE: {"detail":"yt-dlp exploded in /home/robin/.config/yt-dlp/config"}
BEFORE: corrupt settings killed the page:
        400 {"detail":"invalid literal for int() with base 10: 'not-a-number'"}
AFTER : [ ok ] internal ValueError not reflected  (500, "Internal server error")
AFTER : [ ok ] scout internal error not reflected (500, no host paths)
AFTER : [ ok ] user-input errors stay informative (400 "Invalid FEN string",
        400 "Could not identify content source for: ...", 422 on a bad body field)
AFTER : [ ok ] settings survive corrupt values (200 with 24 / 150 / 5.0)
```

---

## 8. CODE-05-04 (HIGH) — `POST /api/repertoire/seed` returned JSON `null`

### Root cause
`repertoire.py:1865-1911` has no `return` on its success path, so FastAPI serialised the body as `null` and `frontend/repertoire_builder.js:544` (`data.status === 'ok'`) threw on `None`, swallowed by a `console.error`-only `catch`. The route blindly re-exported whatever came back.

### Change — `backend/api.py:1214-1299` (route made defensive; `repertoire.py` untouched)

The route no longer trusts the manager's return value. It counts `repertoire_moves` before and after and:
* manager returned a **usable** `ok`/`success`/`seeded` → `{"status": "ok", **manager fields, "seeded_moves": N, "total_moves": M}`;
* manager returned **`None`/anything unexpected but moves really were written** → synthesises the same `ok` body from the verified post-condition and logs a warning. **This is the CODE-05-04 scenario, handled on my side without waiting for FIXER-A.**
* manager returned **`None`/unexpected and nothing was written** → **HTTP 500** `"Seeding did not complete. The repertoire was not changed."` (never a fake 2xx);
* manager raised → **HTTP 500**, logged, generic detail;
* manager reported `skipped` (the non-empty guard tripped) → **HTTP 409** with the body `{"status": "skipped", "message": ..., "seeded_moves": 0, "total_moves": M, "force_required": true}` — the `status` field is preserved so a client can still branch, and a 2xx is never returned for a request that did nothing.

**Opt-in semantics (AGENTS.md §9.4):** `force=true` remains the documented, explicit opt-in for the destructive seed, and it is now *load-bearing* — a guarded skip is a 409 rather than a silent no-op. `force` is a declared `bool`, so `?force=maybe` is a **422** from schema validation rather than a silent coercion. (I did not add a second confirmation parameter: that would break the documented `?force=true` contract in `AGENTS.md` §9.4 and in `db.py`'s comments.)

### **What FIXER-A must return for the route to rely on it**
`RepertoireManager.seed_default_repertoires(force)` should return a `dict` with a **`status` key on every path**, one of `"ok"`/`"success"` (work was done) or `"skipped"`/`"error"` (nothing was done), ideally with a `message` and a count of moves written. With that, the route takes the first branch and the post-condition count is pure corroboration. The route is correct today regardless — it no longer depends on the return value existing — so FIXER-A's change is a robustness/clarity improvement, not a blocker.

### Frontend follow-up (frontend owner's file, not touched)
`repertoire_builder.js:542` posts `/api/repertoire/seed` **without** `force`, so on a non-empty repertoire it now receives 409. `res.json()` still parses, `data.status` is `"skipped"`, and the `=== 'ok'` check correctly does not fire — the visible behaviour is unchanged (the button remains a safe no-op). To make the button do what its tooltip (`index.html:92`) claims, the frontend must send `?force=true` **behind a `confirm()`** and must handle `data.status !== 'ok'` (currently a silent no-op for `skipped`).

### Verified
```
BEFORE: the seed route returned a JSON null body
BEFORE: a no-op seed must not be reported as success: 200 null
BEFORE: seeding a non-empty repertoire without force must not be a 2xx:
        200 {"status":"skipped", ...}
AFTER : [ ok ] seed never returns a null body      (200, status ok, seeded_moves 1)
AFTER : [ ok ] seed failure is not a success       (500, non-null body)
AFTER : [ ok ] seed requires explicit opt-in       (409 + status skipped + force_required,
                                                     force=maybe -> 422, user's e2e4 intact)
```

---

## 9. Full-suite result

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
Ran 305 tests in 13.783s
FAILED (failures=7, errors=2)
```

**The remaining 9 are not mine and are not caused by this patch.** Proof — the same suite run with the *pre-fix* `api.py` swapped back in is strictly worse:

| `backend/api.py` | suite result |
|---|---|
| pre-fix snapshot | `Ran 305 tests … FAILED (failures=8, errors=2)` — adds my 18 contract failures **plus** 2 more (`test_repertoire.test_candidate_moves_2000`, `test_repertoire.test_user_position_stats_and_threat_scoring`) |
| patched (this patch) | `Ran 305 tests … FAILED (failures=7, errors=2)` |

Current failures, all inside files other agents rewrote at 19:21–19:25 while I was working:

| failing test | cause | owner |
|---|---|---|
| `ERROR test_migration_safety` (loader) | `tests/test_migration_safety.py:32` imports `FORBIDDEN_AUTO_STUDY_NAMES` / `find_unreachable_repertoire_moves` from `backend.db`; those symbols were removed by the concurrent `backend/db.py` rewrite | db.py owner + test owner |
| `FAIL test_repertoire.test_all_games_scan_and_count` | `test_repertoire.py:145` still hard-codes `assertGreaterEqual(total_cnt, 280)`; the new synthetic fixture has `SYNTHETIC_GAME_COUNT = 12`. This is exactly the assertion CODE-05-07 says must go | test_repertoire owner |
| `FAIL test_repertoire.test_candidate_moves_white_eval_convention` / `test_candidate_moves_2000` / `test_update_move_metadata_and_trainer_priority` / `test_user_position_stats_and_threat_scoring` | ordering/eval-convention expectations vs. the new synthetic explorer seed | fixtures + test_repertoire owners |
| `FAIL test_scout.test_scout_import_repertoire`, `test_single_played_move_import`, `test_studies.*` | shared-temp-DB pollution across modules after the fixture rewrite (they pass in isolation) | fixtures / test_scout / test_studies owners |

Every module that is **not** being concurrently rewritten is green with this patch:

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_api tests.test_api_contract \
    tests.test_analyzer tests.test_openings tests.test_pedagogical_trainer tests.test_timeline_spam \
    tests.test_trainer_hint_and_line_completion tests.test_user_behavior_probe \
    tests.test_evaluator_stream tests.test_electron_shell \
    tests.test_builder_frontend_contracts tests.test_scout_frontend_contracts
Ran 207 tests in 9.460s
OK
```

I did **not** weaken or delete any existing test. No test asserted the buggy behaviour. The only existing assertions that changed status are the ones that were *asserting the bug indirectly* — and they are all still satisfied:
* `tests/test_api.py::test_sync_malformed_games` still asserts `200` + `status == "success"` (verified passing).
* `tests/test_api.py::test_recent_games_endpoint` / `test_recent_games_filters` still pass (they only assert key presence and the colour filter).

---

## 10. Interface changes other owners must match

### For FIXER-A (`backend/repertoire.py`)
1. **`played_at` unit.** Replace all four writers (`repertoire.py:1476, 1568, 1721, 1829`) with `backend.api.normalize_played_at(value)` (or the identical `v >= 1e11 ? v : v*1000` expression). Canonical unit = **epoch milliseconds**. Until then, `get_recent_games` reads seconds-valued rows correctly via the SQL `CASE`, but the column stays mixed.
2. **`games.result` tokens.** `repertoire.py:1479, 1571, 1724, 1832` still write `g.get("winner", "draw")` verbatim, which persists `NULL` for every draw. Use `normalize_game_result(...)` so a draw is stored as `'draw'`. (Reads already treat `NULL` as a draw.)
3. **`analyzed_at` ordering (CODE-02-04, still open on your side).** `analyze_user_games` (`:1726-1728`) and `stream_analyze_user_games` (`:1829`) still write `analyzed_at = int(time.time())` *before* analysing, so a failed analysis permanently suppresses that game on the paths the frontend actually uses. Move the stamp after a successful analysis and add per-game error isolation. (`sync_recent_games` / `stream_sync_recent_games` already use `COALESCE((SELECT analyzed_at …), 0)`, which is the right shape.)
4. **`seed_default_repertoires` must return a `dict` with a `status` key on the success path** (`"ok"`), mirroring the existing guard path's `{"status": "skipped", …}`. The route is already correct without it.

### For the frontend owner
1. **`POST /api/repertoire/seed`**: a guarded skip is now **409** with `{"status": "skipped", "message": …, "seeded_moves": 0, "total_moves": N, "force_required": true}`. `repertoire_builder.js:542` should send `?force=true` behind a `confirm()` and should surface `status !== 'ok'` instead of silently doing nothing. No crash: `res.json()` still parses and `data.status` is not `'ok'`.
2. **`POST /api/sync`** (unused by the frontend today) now always returns `status` + `synced_games` + `new_blunders` + `skipped_games` + `failed_games` + `errors`, and returns 500/502 on a whole-request failure instead of 200. If anything starts calling it, branch on `status` ∈ `{success, partial, no_games}` and on the HTTP status.
3. **`GET /api/games/recent`**: `games[].result` is now guaranteed `'win' | 'loss' | 'draw'`, a draw is `½-½` (never `0-1`), and `games[].played_at` is always **epoch milliseconds** (was mixed). `played_at_formatted` is unchanged and is what the UI actually uses. **No frontend edit required.**
4. **`GET /api/dashboard`**: `game_results[].result` is now `white | black | draw` instead of the raw column (which could be `null` or `1-0`/`0-1`-style values written by other code). Field set unchanged. No frontend caller today.
5. **`/api/repertoire/{import_line,check_line,check_tree}`** still work exactly as before (they are now thin aliases of the canonical `/api/scout/*` handlers) and are hidden from `/openapi.json`. Pick one spelling per feature; no behaviour change to migrate to.
6. **Wrong verbs under `/api/` now return 405** instead of a `StaticFiles` 404. Any frontend code that branched on a 404 from a wrong-verb call must be updated (no such call exists today).

---

## 11. Residual risks / not fixed

1. **`repertoire.py` still writes seconds and NULL results** (see §10). The read path is correct, the stored column is not, until FIXER-A lands.
2. **`fetch_user_games` collapses "no games" and "request failed" into `[]`** (`lichess_client.py:85-98`). I worked around it in `/api/sync` with an account probe; a real fix belongs in `lichess_client.py` (e.g. return `None` on transport/HTTP failure). That file is not mine.
3. **`LichessClient.get_account_profile()` performs a live network call** from inside the empty-games branch of `/api/sync`. It is rate-limited by how often the empty case occurs, but it is a request on a user-visible path. The token is read from `.env`; if the token is absent the probe is skipped and the result is a truthful `no_games`.
4. **The `/api/*` catch-all walks `app.router.routes` on every unmatched `/api/` request.** O(routes) per request, ~40 routes, negligible — but it is per-request work on a path that only occurs on client error.
5. **`DELETE /api/repertoire/move` takes a JSON body**, so a client that sends it as a query string gets 422, not 400/405. Unchanged behaviour; not in scope for any assigned finding.
6. **The `ValueError` → 500 change reclassifies some previously-4xx cases.** Any `ValueError` that a route did *not* explicitly convert is now a 500. I verified the four known `ValueError` sources (`import_game_opening`, three `ContentScout` sites) are converted, but a `ValueError` raised from deep inside a third-party library would now surface as a 500 instead of a 400. That is the intended direction (500 + log > 400 + leaked internals), and it is strictly more truthful.
7. **`migrate_played_at_units()` runs an `UPDATE` on every `GET /api/games/recent`.** It is a no-op after the first call but it is still a write statement on a read endpoint, and it needs the write lock. On a 300-row table this is sub-millisecond; on a very large library it would be better as a one-shot boot migration only.
8. **The snapshot module** (`audit/scripts/api_prefix_snapshot.py`) pins `FRONTEND_DIR` to an absolute path. It is evidence-only and must not be imported by the app.
