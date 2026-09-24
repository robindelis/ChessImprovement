# FIXER-U-01 — `backend/api.py`: total boundary, read-only reads, one canonical explorer key

**Owner:** FIXER-U (exclusive write access to `backend/api.py`)
**Findings fixed:** ABUSE-16, ABUSE-18, ABUSE-19, ABUSE-20, ABUSE-21, ABUSE-22
**Files touched:** `backend/api.py` (primary), `backend/evaluator.py` (**one constant**, sanctioned by the brief), **new** `tests/test_api_boundary_hardening.py`, `audit/scripts/api_prefix_snapshot_FIXER-U.py`, `audit/scripts/FIXER-U_before_after.py`, `audit/scripts/FIXER-U_socket_ab.py`, `audit/scripts/fixu_lib.py`, `audit/scripts/FIXER-U_real_socket.py`, `audit/scripts/FIXER-U_root_cause.py`, `audit/evidence/FIXER-U_*.txt`, this report.
**Existing tests touched:** two *injection points* and two *string literals* in `tests/test_api_security.py`, and two *call sites* in `tests/test_api_contract.py` — no assertion was weakened or deleted; see §9.
**Built on:** `FIXER-B-01.md` and `FIXER-L-01.md` — nothing there was undone (see §12 for the two places their tests had to be re-pointed, and why that is a consequence of the fix rather than a relaxation of it).
**Production data:** never written. Production was inspected only via `tests.probe_guard.fingerprint()` (plain `open(...,'rb')`, no SQLite connection). Every probe/test imports `tests.test_setup` first. Port 38281 was never contacted; socket tests bind an OS-assigned throwaway port.
**Interpreter:** `/home/robin/miniconda3/bin/python3` (base conda env) only.

> **Where the "before" baseline comes from.** `git` is not usable: `backend/api.py` was already far from `HEAD`. `audit/scripts/api_prefix_snapshot_FIXER-U.py` is a byte copy of `backend/api.py` as FIXER-L left it, with `FRONTEND_DIR` pinned one level deeper so the `StaticFiles` mount still resolves from `audit/scripts/` (the same trap both predecessors documented). **Evidence only; never imported by the app.**

---

## 0. Evidence

### 0.1 Fail-before / pass-after — `audit/scripts/FIXER-U_before_after.py`

Runs the *same* 15 check functions from `tests/test_api_boundary_hardening.py` against the pre-fix snapshot and the patched module, **in two separate child processes** (both register middleware and exception handlers on one process-global `FastAPI`, so they cannot coexist).

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-U_before_after.py

check                                                          BEFORE  AFTER   detail (before if it failed, else after)
-------------------------------------------------------------------------------------------------------------------------
check_validation_handler_is_total                              FAIL    PASS    TypeError: Object of type bytes is not JSON serializable
check_short_body_never_5xx_on_any_post_route                   FAIL    PASS    5xx on 11/21 POST routes: ['/api/repertoire/import_game_opening
check_lone_surrogate_is_refused_before_any_route               FAIL    PASS    AttributeError: module 'pre_fix_api' has no attribute 'body_has_lone_surrogate'
check_depth_bomb_is_400_under_every_content_type               FAIL    PASS    depth bomb on /api/settings with Content-Type='text/plain' -> 500
check_body_caps_apply_to_get                                   FAIL    PASS    3 MiB body on GET /api/games/recent -> 200
check_recent_games_performs_no_write                           FAIL    PASS    /api/games/recent?limit=5 -> 500 {"detail":"Internal server error."}
check_startup_owns_the_played_at_migration                     FAIL    PASS    migrate_played_at_units() must have exactly one call site
check_explorer_singleflight_is_keyed_on_the_canonical_fen      FAIL    PASS    48 views of one transposed position made 2 outbound calls, expected 1
check_explorer_singleflight_under_concurrency                  FAIL    PASS    16 concurrent views made 2 outbound calls, expected 1
check_explorer_lock_table_stays_bounded                        FAIL    PASS    module 'pre_fix_api' has no attribute 'EXPLORER_STATS_LOCK_LIMIT'
check_pruning_never_drops_a_held_lock                          FAIL    PASS    module 'pre_fix_api' has no attribute 'EXPLORER_STATS_LOCK_LIMIT'
check_retry_after_agrees_with_the_pool_wait                    n/a     PASS    None
check_local_boundary_still_holds                               PASS    PASS    None  [CONTROL: passes both ways by design]
check_short_body_never_5xx_over_a_real_socket                  FAIL    PASS    [socket] 5xx on 11/21 POST routes
check_recent_games_does_not_block_behind_a_foreign_write_lock  FAIL    PASS    GET /api/games/recent is still blocked after 10s behind a foreign
                                                                               write lock -- it is opening a write transaction on a read route
-------------------------------------------------------------------------------------------------------------------------
(1 check(s) are n/a on the pre-fix module and are not counted.)
BEFORE: 1 passed / 13 failed
AFTER : 14 passed / 0 failed
```

Full log: `audit/evidence/FIXER-U_before_after.txt`.

`check_retry_after_agrees_with_the_pool_wait` reads `module.ENGINE_BUSY_RETRY_AFTER`, a symbol the pre-fix module does not have; it is reported `n/a` and excluded from both tallies rather than being counted as a pass.

`check_local_boundary_still_holds` is a **deliberate CONTROL**: FIXER-L's boundary already held, so it must pass on both sides. It is the evidence that this pass did not regress ABUSE-03/13.

### 0.2 Real socket, real headers — `audit/scripts/FIXER-U_socket_ab.py`

Boots the pre-fix snapshot and the patched module on **two separate throwaway uvicorn ports** and drives both with raw `http.client` requests: 4 content types × 11 bodies × **all 21** `POST /api` routes, the depth bomb under each content type, a 3 MiB `GET` body, and a read issued while a second connection holds `BEGIN IMMEDIATE`.

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-U_socket_ab.py

=== BEFORE  (pre-fix snapshot) ===   [21 POST /api routes]
[FAIL] application/json         / lone surrogate              {200: 10, 500: 11}
[FAIL] application/json         / bare lone surrogate         {200: 5, 500: 16}
[FAIL] text/plain               / 5-byte ascii                {200: 5, 500: 16}
[FAIL] text/plain               / 1-byte NUL                  {200: 5, 500: 16}
[FAIL] text/plain               / lone surrogate              {200: 5, 500: 16}
[FAIL] text/plain               / bare lone surrogate         {200: 5, 500: 16}
[FAIL] text/plain               / invalid utf-8               {200: 5, 500: 16}
[FAIL] text/plain               / json-looking                {200: 5, 500: 16}
[FAIL] text/plain               / json array                  {200: 5, 500: 16}
[FAIL] text/plain               / deep-ish json               {200: 5, 500: 16}
[FAIL] text/plain               / form-ish                    {200: 5, 500: 16}
[FAIL] text/plain               / paired astral escape         {200: 5, 500: 16}
[FAIL] text/plain               / escaped backslash then u    {200: 5, 500: 16}
[FAIL] no content-type          / 5-byte ascii                {200: 5, 500: 16}
[FAIL] no content-type          / 1-byte NUL                  {200: 5, 500: 16}
[FAIL] no content-type          / lone surrogate              {200: 5, 500: 16}
[FAIL] no content-type          / bare lone surrogate         {200: 5, 500: 16}
[FAIL] no content-type          / invalid utf-8               {200: 5, 500: 16}
[FAIL] no content-type          / json-looking                {200: 5, 500: 16}
[FAIL] no content-type          / json array                  {200: 5, 500: 16}
[FAIL] no content-type          / deep-ish json               {200: 5, 500: 16}
[FAIL] no content-type          / form-ish                    {200: 5, 500: 16}
[FAIL] no content-type          / paired astral escape         {200: 5, 500: 16}
[FAIL] no content-type          / escaped backslash then u    {200: 5, 500: 16}
[FAIL] application/octet-stream / 5-byte ascii                {200: 5, 500: 16}
[FAIL] application/octet-stream / 1-byte NUL                  {200: 5, 500: 16}
[FAIL] application/octet-stream / lone surrogate              {200: 5, 500: 16}
[FAIL] application/octet-stream / bare lone surrogate         {200: 5, 500: 16}
[FAIL] application/octet-stream / invalid utf-8               {200: 5, 500: 16}
[FAIL] application/octet-stream / json-looking                {200: 5, 500: 16}
[FAIL] application/octet-stream / json array                  {200: 5, 500: 16}
[FAIL] application/octet-stream / deep-ish json               {200: 5, 500: 16}
[FAIL] application/octet-stream / form-ish                    {200: 5, 500: 16}
[FAIL] application/octet-stream / paired astral escape         {200: 5, 500: 16}
[FAIL] application/octet-stream / escaped backslash then u    {200: 5, 500: 16}
[ok  ] depth bomb under application/json         is 400  -> 400
[FAIL] depth bomb under text/plain               is 400  -> 500 {"detail":"Internal server error."}
[FAIL] depth bomb under no content-type          is 400  -> 500 {"detail":"Internal server error."}
[FAIL] depth bomb under application/octet-stream is 400  -> 500 {"detail":"Internal server error."}
[FAIL] 3 MiB body on GET /api/games/recent is refused          -> 200
[FAIL] GET /api/games/recent behind a foreign write lock  -> STILL BLOCKED after 10s
    10/50 socket checks passed

=== AFTER   (patched backend.api) ===   [21 POST /api routes]
[ok  ] application/json         / 5-byte ascii                {200: 5, 422: 16}
[ok  ] application/json         / 1-byte NUL                  {200: 5, 422: 16}
[ok  ] application/json         / lone surrogate              {400: 21}
[ok  ] application/json         / bare lone surrogate         {400: 21}
[ok  ] application/json         / invalid utf-8               {200: 5, 400: 16}
[ok  ] application/json         / json-looking                {200: 5, 422: 16}
[ok  ] application/json         / json array                  {200: 5, 422: 16}
[ok  ] application/json         / deep-ish json               {200: 11, 422: 10}
[ok  ] application/json         / form-ish                    {200: 5, 422: 16}
[ok  ] application/json         / paired astral escape        {200: 11, 422: 10}   <- LEGITIMATE, still served
[ok  ] application/json         / escaped backslash then u   {200: 11, 422: 10}   <- LEGITIMATE, still served
[ok  ] text/plain               / 5-byte ascii                {200: 5, 422: 16}
[ok  ] text/plain               / 1-byte NUL                  {200: 5, 422: 16}
[ok  ] text/plain               / lone surrogate              {400: 21}
...  (all 44 remaining cells [ok  ])
[ok  ] depth bomb under application/json         is 400  -> 400
[ok  ] depth bomb under text/plain               is 400  -> 400
[ok  ] depth bomb under no content-type          is 400  -> 400
[ok  ] depth bomb under application/octet-stream is 400  -> 400
[ok  ] 3 MiB body on GET /api/games/recent is refused          -> 413
[ok  ] GET /api/games/recent behind a foreign write lock  -> {'status': 200, 'elapsed': 0.0082}
    50/50 socket checks passed
```

Full grid: `audit/evidence/FIXER-U_socket_ab.txt`. **10/50 → 50/50.**

Note the two rows marked *LEGITIMATE*: a properly paired astral escape (`\ud83d\ude00`, an emoji) and an escaped backslash (`\\ud800`) are still served. The fix is not "reject anything with a `\` in it".

### 0.3 Root-cause isolation — `audit/scripts/FIXER-U_root_cause.py`

Proves the handler is the crash site, not the route, and that both root causes are real:

```
=== 1. what does sanitize()/json.dumps() do with these inputs? ===
  lone-surrogate   -> TypeError: Object of type bytes is not JSON serializable
  5-byte text      -> TypeError: Object of type bytes is not JSON serializable
  json-looking     -> TypeError: Object of type bytes is not JSON serializable

=== 2. status per (route, content-type, body) ===   [pre-fix]
  /api/settings        application/json  lone-surrogate=200 5-byte text=422 json-looking=422
  /api/settings        text/plain        lone-surrogate=500 5-byte text=500 json-looking=500
  /api/settings        no content-type   lone-surrogate=500 5-byte text=500 json-looking=500
  /api/repertoire/move application/json  lone-surrogate=500 5-byte text=422 json-looking=422
  /api/repertoire/move text/plain        lone-surrogate=500 5-byte text=500 json-looking=500
  ... 16 of 20 POST routes -> 500
```

And the giant-int variant, which is a **fourth** way into the same handler:

```
  10**100000: pre-fix RAISES ValueError: Exceeds the limit (4300 digits) for integer string conversion
  10**5000:   pre-fix RAISES ValueError: Exceeds the limit (4300 digits) for integer string conversion
```

---

## 1. ABUSE-16 (MEDIUM) — the validation handler was a 500, and it bypassed its own fix

### Root cause

Four independent defects. The first two are in the ~11-line `RequestValidationError` handler FIXER-L installed at `backend/api.py:195-206` (pre-fix):

```python
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    def sanitize(val):
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        if isinstance(val, dict):
            return {str(k): sanitize(v) for k, v in val.items()}
        if isinstance(val, (list, tuple)):
            return [sanitize(x) for x in val]
        return val                       # <- everything else passes through raw
    cleaned_errors = sanitize(exc.errors())
    return JSONResponse(status_code=422, content={"detail": cleaned_errors})
```

1. **No `bytes` case.** pydantic reports `'input': b'<raw body>'` whenever the body could not be decoded as JSON — which is *precisely* what FastAPI does for every non-JSON content type (`fastapi/routing.py`: `if content_type is json-ish: body = await request.json() else: body = body_bytes`). So `text/plain` + 5 bytes → `TypeError: Object of type bytes is not JSON serializable` **inside the handler meant to answer 422s**.
2. **No lone-surrogate case.** CPython decodes request bytes with `errors="surrogatepass"`, so `b'{"name":"\xed\xa0\x80"}'` becomes `{'name': '\ud800'}` — a legal Python `str` holding an un-encodable code point. Pydantic echoes it into the error, and `JSONResponse.render` calls `json.dumps(..., ensure_ascii=False).encode("utf-8")` → `UnicodeEncodeError`.
3. **No `int` case with a magnitude bound.** `10**5000` in a body → `str(value)` inside the encoder is itself refused by CPython's 4300-digit guard. Found *while fixing* this: my first `_json_safe` rendered oversized ints with `str()` and reproduced the exact 500. Fixed by rendering from `bit_length()`.
4. **The depth guard was gated on a caller-controlled header.** `if body and (content_type == "application/json" or ...)`. Nothing in FastAPI enforces `Content-Type`, so `text/plain` / absent / `octet-stream` + a 4000-deep JSON bomb skipped the guard entirely and reached pydantic, which 500'd.

And the reason the **shipped security suite was green**: every hostile-body test in `tests/test_api_security.py` declares `application/json`, and the one form-urlencoded case (`check_oversized_body_is_413`) is stopped at the *size* gate before any parsing. The gap sat between two individually-correct tests.

### Changes

**(a) A total renderer — `backend/api.py:201-323`**

`MAX_ERROR_ITEMS = 20`, `MAX_ERROR_DEPTH = 6`, `MAX_ERROR_STRING = 200`, `MAX_ERROR_INT = 10**18`; `_utf8_safe()` (`:216`) neutralises un-encodable characters with the C-level `errors="replace"` round trip and caps length; `_json_safe()` (`:236`) is **total by construction** — it has a case for `bytes`/`bytearray`/`memoryview`, `set`/`frozenset`, non-finite floats, oversized ints (via `bit_length()`), and falls back to `repr()` for anything else, and it is itself depth-bounded so a deep `input` cannot recurse it into a `RecursionError`.

The handler (`:299`) now renders the payload itself and returns a `Response`, so the body is encoded exactly once, with `errors="replace"` as the last line of defence:

```python
    try:
        payload = json.dumps({"detail": _json_safe(errors)}, allow_nan=False)
    except Exception:  # pragma: no cover - _json_safe is total, belt and braces
        logger.exception("Could not render a 422 body for %s %s",
                         request.method, request.url.path)
        payload = FALLBACK_VALIDATION_BODY
    return Response(content=payload.encode("utf-8", "replace"),
                    status_code=422, media_type="application/json")
```

The caps also close an **amplification** hole that was open before: a 2 MiB rejected body used to be reflected verbatim into the 422 response. `check_validation_handler_is_total` asserts a 2 MiB rejected string and a 5000-item list both produce a body under 64 KiB.

**(b) Depth guard gated on the body — `backend/api.py:647-664` + `:916`**

```python
def body_is_json(content_type: str, body: bytes) -> bool:
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        return True
    return body.lstrip()[:1] in (b"{", b"[")
```

The header is caller-controlled, so the body gets a vote. This cannot reject a legitimate non-JSON body: form and multipart payloads percent-encode or quote their content, and real payloads nest 4 deep (the cap is 32).

**(c) A lone surrogate is refused at the boundary — `backend/api.py:667-738` + `:930`**

A third instance of the *same* root cause turned up while fixing: a body that **validates successfully** and is then reflected by a *route* rather than by the handler. `POST /api/repertoire/train/recent_opening` takes `Dict[str, Any]`, so `{"name": "\ud800"}` passes pydantic, the route does `return {"status": "success", "recent_opening": {...}}`, and `JSONResponse` raises `UnicodeEncodeError` → **500**. That route also *persists* the surrogate into the `settings` table.

Fixing it per-instance would be fixing symptoms; the boundary already has the bytes, so `body_has_lone_surrogate()` refuses it once with a **400**:

```python
def body_has_lone_surrogate(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Only the surrogate range decodes under `surrogatepass`; anything else
        # undecodable here is already a 400 further down the stack.
        return bool(_SURROGATE_BYTE_RANGE_RE.search(data))
    if "\\u" not in text and "\\U" not in text:
        return False
    return _text_has_unpaired_surrogate_escape(text)
```

`_text_has_unpaired_surrogate_escape` walks left-to-right and consumes `\\` as a unit, so a literal escaped backslash never starts an escape, and a properly **paired** `𐀀` is a legitimate astral code point and is accepted. `check_lone_surrogate_is_refused_before_any_route` asserts 7 must-refuse shapes and 6 must-not-refuse shapes at the unit level, then 400 on three routes × four content types, then that nothing was written to `settings`.

**(d) Status codes are now consistent** — a validation failure is always 4xx, whatever the body; a depth bomb is 400 under every content type; an oversized body is 413.

### Verified

`check_validation_handler_is_total` (11 error payloads: the two root causes, a surrogate dict **key**, NaN/Infinity, `object()`, a `set`, `10**100000`, `10**5000`, a **cyclic** structure, a 2 MiB string, a 5000-item list), `check_short_body_never_5xx_on_any_post_route` (44 cells over `TestClient`), `check_short_body_never_5xx_over_a_real_socket` (the same 44 cells over a real uvicorn socket), `check_lone_surrogate_is_refused_before_any_route`, `check_depth_bomb_is_400_under_every_content_type`.

```
BEFORE: TypeError: Object of type bytes is not JSON serializable
BEFORE: content-type=application/json body=lone surrogate: 5xx on 11/21 POST routes
BEFORE: [socket] content-type=application/json body=lone surrogate: 5xx on 11/21 POST routes
BEFORE: content-type=text/plain body=5-byte ascii: 5xx on 16/21 POST routes
BEFORE: depth bomb on /api/settings with Content-Type='text/plain' -> 500
AFTER : 50/50 socket checks passed
AFTER : [ok] check_validation_handler_is_total
AFTER : [ok] check_short_body_never_5xx_on_any_post_route
AFTER : [ok] check_lone_surrogate_is_refused_before_any_route
AFTER : [ok] check_depth_bomb_is_400_under_every_content_type
```

---

## 2. ABUSE-18 (MEDIUM) — a read route opened a DB write transaction on every request

### Root cause

`api.py:1084` (pre-fix), the first statement of `get_recent_games`:

```python
    # Bring any historical second-valued `played_at` rows up to the canonical
    # millisecond unit before ordering. Idempotent (no-op once migrated).
    migrate_played_at_units()
```

`migrate_played_at_units()` (`:170-190`) is a table-wide `UPDATE games SET played_at = played_at * 1000 …` plus a `commit()`. SQLite serialises writers, so every single `GET /api/games/recent` needed **the DB write lock**. ATK-5 measured 3.64 s behind a foreign write lock (2.64 s on a second run), and because the `UPDATE` is unguarded a lock timeout turns the game list into a 500.

It was also, as ATK-5 A/B-proved, **dead weight**: the `ORDER BY CASE WHEN played_at > 0 AND played_at < 1e11 THEN played_at * 1000 …` at `:1113-1116` and `normalize_played_at()` at `:1129` already produce byte-identical output with and without it.

### Change — `backend/api.py:1333-1344`

The call is removed from the route; the reasoning is recorded where the call used to be, and `startup_event()` is annotated as the sole owner (`:965-981`):

```diff
-    # Bring any historical second-valued `played_at` rows up to the canonical
-    # millisecond unit before ordering. Idempotent (no-op once migrated).
-    migrate_played_at_units()
-
+    # Audit ABUSE-18: this route used to call `migrate_played_at_units()` first,
+    # i.e. a table-wide `UPDATE` -- a *write* -- on every single read. ...
+    # The migration is a boot concern, so it lives in `startup_event()` and
+    # only there. Nothing about the *response* depends on it.
     conn = get_db()
```

### Verified

`check_recent_games_performs_no_write` enforces this with a SQLite **authorizer** that refuses every write primitive (`SQLITE_INSERT`/`UPDATE`/`DELETE`/`CREATE_*`/`DROP_*`/`ALTER_*`/`REINDEX`/`ANALYZE`), so a write cannot hide behind a helper or a `commit()`. The authorizer receives action codes as **integers** on this build, so it compares against `sqlite3.SQLITE_*` — and the check **first proves the guard refuses a write**, because a name-based comparison silently never matches and would make the whole check vacuous. (It did, on the first run: the pre-fix module passed a name-only version of this check. That is why the guard self-test is in the check.)

`check_recent_games_does_not_block_behind_a_foreign_write_lock` measures it over a real socket: a second connection takes `BEGIN IMMEDIATE`, the read is issued with a hard 10 s budget.

```
BEFORE: GET /api/games/recent is still blocked after 10s behind a foreign write lock
        -- it is opening a write transaction on a read route
BEFORE: /api/games/recent?limit=5 -> 500 {"detail":"Internal server error."}   (authorizer denies the UPDATE)
AFTER : GET /api/games/recent behind a foreign write lock -> {'status': 200, 'elapsed': 0.0082}
AFTER : check_startup_owns_the_played_at_migration — exactly one call site, and it is in startup_event
```

`check_startup_owns_the_played_at_migration` counts the call sites with `inspect.getsource` and asserts **exactly one**, and that it is inside `startup_event()`. Before, there were two.

---

## 3. ABUSE-19 (MEDIUM) — the single-flight was keyed on the wrong FEN

### Root cause

`get_master_stats` stated its goal as "N concurrent views of the same position make ONE outbound call", and the single-flight was structurally correct — but keyed on the **raw 6-token** `fen`:

```python
    memo = _explorer_stats_memo.get(fen)        # raw 6-token
    ...
    lock = _explorer_stats_lock(fen)            # raw 6-token
```

while the `explorer_cache` read immediately beneath it used the **4-token** form (`_normalize_explorer_fen`, which `AGENTS.md` §4F mandates and which `backend/repertoire.py:209` implements). The two layers could therefore never agree.

`1.e4 c6 2.d4 d5 3.Nc3` and `1.e4 c6 2.Nc3 d5 3.d4` reach the same position with halfmove clocks `1` and `0`:

```
raw: rnbqkbnr/pp2pppp/2p5/3p4/3PP3/2N5/PPP2PPP/R1BQKBNR b KQkq - 1 3
raw: rnbqkbnr/pp2pppp/2p5/3p4/3PP3/2N5/PPP2PPP/R1BQKBNR b KQkq - 0 3
identical raw FEN?  False
identical 4-token?  True
```

Two distinct keys, one position → 48 views, 47 outbound calls.

### Change — `backend/api.py:23`, `:1715-1729`, `:1781-1822`

**One canonical implementation, not a third copy.** `_normalize_explorer_fen` now delegates to `backend.repertoire.normalize_fen` (imported at `:23`), which `AGENTS.md` §4F designates as the single source of truth for the `repertoire_moves.parent_fen` / `explorer_cache.fen` / `blunders.fen_before` join key:

```diff
 def _normalize_explorer_fen(fen: str) -> str:
-    """The 4-token key `explorer_cache` is written with (`normalize_fen`)."""
-    parts = (fen or "").strip().split()
-    if len(parts) >= 4:
-        return " ".join(parts[:4])
-    return (fen or "").strip()
+    return normalize_fen(fen) or (fen or "").strip()
```

`get_master_stats` computes the key once and uses it for all three layers:

```python
    key = _normalize_explorer_fen(fen)
    memo = _explorer_stats_memo.get(key)
    ...
    cached = _master_stats_from_explorer_cache(fen)
    if cached:
        _remember_explorer_stats(key, cached)     # was: (fen, cached)
    lock = _explorer_stats_lock(key)             # was: _explorer_stats_lock(fen)
    with lock:
        memo = _explorer_stats_memo.get(key)
        ...
        _remember_explorer_stats(key, stats)     # was: (fen, stats)
```

`_remember_explorer_stats`'s parameter is renamed `key` so the contract is visible at the definition.

### Verified

`check_explorer_singleflight_is_keyed_on_the_canonical_fen` builds the transposition, asserts the two raw FENs differ and the canonical keys match, asserts `_normalize_explorer_fen(f) == backend.repertoire.normalize_fen(f)` for three FENs (proving it is *the* implementation), then does 48 alternating views and counts outbound calls.

`check_explorer_singleflight_under_concurrency` does the same with 16 threads, using a **gate the test controls** to hold the first outbound call open until every thread has reached `get_master_stats`. Without the gate the threads serialise on a warm memo and the check passes for the wrong reason — it did exactly that on the pre-fix module until the gate was added (and one iteration used a racy `len(results) % 2` split, replaced with `itertools.count()`).

```
BEFORE: 48 views of one transposed position made 2 outbound calls, expected 1
BEFORE: 16 concurrent views made 2 outbound calls, expected 1
AFTER : [ok] check_explorer_singleflight_is_keyed_on_the_canonical_fen
AFTER : [ok] check_explorer_singleflight_under_concurrency
```

---

## 4. ABUSE-20 (LOW) — the lock table was unbounded and never pruned

### Root cause

`_explorer_stats_locks` grew one `threading.Lock` per distinct FEN and was **never pruned** — retained for the lifetime of the process — while the memo beside it *was* bounded at 512. An entry only needs to exist while a lookup is in flight, so the table was a pure leak with a per-key cost.

### Change — `backend/api.py:1669-1712`

`EXPLORER_STATS_LOCK_LIMIT = 256` (half the memo limit: the locks are the larger object and the shorter-lived state), and `_prune_explorer_stats_locks()` drops the oldest entries until the table is under the limit. **`dict` preserves insertion order**, so the iteration is oldest-first, and **a currently-held lock is never dropped** — removing it would let a second caller mint a second lock for the same key, and two concurrent outbound calls for one position is precisely what the lock exists to prevent. The table therefore settles at `limit + (locks held right now)`, which concurrency already bounds.

### Verified

`check_explorer_lock_table_stays_bounded` drives `limit * 20` distinct positions and asserts the table is back at or under the limit, that the most recent key still has its entry, and that `_explorer_stats_lock(k) is _explorer_stats_lock(k)` (pruning did not mint a second lock for one key). `check_pruning_never_drops_a_held_lock` acquires a lock, then forces `limit + 50` more keys through the pruner and asserts the held lock survived.

```
BEFORE: module 'pre_fix_api' has no attribute 'EXPLORER_STATS_LOCK_LIMIT'
AFTER : [ok] check_explorer_lock_table_stays_bounded
AFTER : [ok] check_pruning_never_drops_a_held_lock
```

---

## 5. ABUSE-21 (LOW) — GET bodies were never read, so both caps were skipped on GET

### Root cause

```python
        has_body = method in BODY_METHODS or "transfer-encoding" in headers
```

`BODY_METHODS` is `{"POST", "PUT", "PATCH", "DELETE"}` and a `GET` does not normally send `Transfer-Encoding`, so a `GET` that *did* declare a body skipped **both** the 2 MiB cap and the JSON depth guard. ATK-5 verified this is **not** request-smuggling-exploitable — correct — but a cap that applies to every verb except one is not a cap.

### Change — `backend/api.py:871-884`

```python
        has_body = (method in BODY_METHODS
                    or "transfer-encoding" in headers
                    or "content-length" in headers)
```

The cap is a property of the **request**, not of the verb. A `GET` that declares no `Content-Length` and no `Transfer-Encoding` — every normal `GET`, including the two long-lived SSE routes — is still **not** buffered, so streaming behaviour is unchanged.

### Verified

`check_body_caps_apply_to_get` (3 MiB on three GET routes, plus the depth bomb on a `GET`, plus two normal `GET`s that must stay 200) and the socket A/B (`-> 200` before, `-> 413` after).

```
BEFORE: 3 MiB body on GET /api/games/recent -> 200
AFTER : 3 MiB body on GET /api/games/recent is refused -> 413
```

---

## 6. ABUSE-22 (MEDIUM) — `Retry-After: 5` for a 300 s wait

### Root cause

`evaluator.py:126` had `SYNC_POOL_WAIT_TIMEOUT = 300.0` (176 s measured tail latency), and both `EngineBusy` handlers in `api.py` advertised a hard-coded `{"Retry-After": "5"}` — off by **60×**. A client that honoured the header and retried was told help was imminent when the server would keep the caller waiting for five minutes.

### Change

**`backend/evaluator.py:124-135`** — the wait itself, **one constant** (the brief sanctions this explicitly; the file is FIXER-S-adjacent and nothing else there is touched):

```diff
-SYNC_POOL_WAIT_TIMEOUT = _env_float("CHESS_ENGINE_WAIT_TIMEOUT", 300.0)
+SYNC_POOL_WAIT_TIMEOUT = _env_float("CHESS_ENGINE_WAIT_TIMEOUT", 20.0)
```

20 s is the value the async/SSE pool in the same module already uses (`ASYNC_POOL_WAIT_TIMEOUT = 20.0`). A Stockfish analysis of one opening position takes a few seconds, so 20 s covers a legitimately busy pool and still fails fast on a wedged one. Still overridable with `CHESS_ENGINE_WAIT_TIMEOUT`.

**`backend/api.py:490-498`** — the advertised retry is now **derived**, so the two cannot drift again:

```python
ENGINE_BUSY_RETRY_AFTER = max(1, int(round(SYNC_POOL_WAIT_TIMEOUT)))
```

**`backend/api.py:1291` and `:2044`** — both `except EngineBusy` sites use `str(ENGINE_BUSY_RETRY_AFTER)`. The 503 and the `Retry-After` now come from one number.

### Verified

`check_retry_after_agrees_with_the_pool_wait` asserts `ENGINE_BUSY_RETRY_AFTER == max(1, round(SYNC_POOL_WAIT_TIMEOUT))` and `SYNC_POOL_WAIT_TIMEOUT <= 30.0` — the second assertion is what stops anyone putting the 300 s back.

```
BEFORE: n/a (the constant did not exist)
AFTER : [ok] check_retry_after_agrees_with_the_pool_wait
```

---

## 7. Interface changes

**For other owners**

1. **`POST /api/repertoire/train/recent_opening`** now answers **400** for a body containing an unpaired UTF-16 surrogate (raw WTF-8 `\xed\xa0\x80` or a `\ud800` escape). A properly *paired* surrogate pair (an astral character) is still accepted. Before, such a body was accepted, 500'd in the response encoder, and **persisted into `settings`**.
2. **Any request whose body starts with `{` or `[` is now analysed for JSON nesting and for unpaired surrogates, whatever its `Content-Type`.** A body that is *not* JSON-looking under a non-JSON content type is unaffected. A `GET` that declares a body is now size-capped like every other verb.
3. **`GET /api/games/recent` no longer writes.** A seconds-valued `played_at` row is still *read* correctly (`ORDER BY CASE …` + `normalize_played_at`), and the stored column is migrated at boot by `startup_event()`. If another process inserts a seconds-valued row while the app is running, the read stays correct and the row is migrated on the next boot.
4. **`SYNC_POOL_WAIT_TIMEOUT` is now 20 s, not 300 s.** A saturated pool now 503s after ~20 s instead of blocking for five minutes. Override with `CHESS_ENGINE_WAIT_TIMEOUT`. `Retry-After` on both 503s is now `20`.
5. **`backend.api.normalize_fen` is now the name `repertoire.normalize_fen` is bound to** inside this module. Nothing imports it from `api`, so this is internal.

**For the frontend owner:** nothing required. No response shape changed.

---

## 8. Tests

**New:** `tests/test_api_boundary_hardening.py` — 15 module-level check functions plus a `unittest` wrapper, so the same functions run under `discover` and under `FIXER-U_before_after.py`. The matrix sweeps the full `(content-type × body-shape × every POST route)` grid rather than sampling it, because sampling is exactly what let the original defect through. Two checks run over a **real uvicorn socket** on an OS-assigned throwaway port. `tests/test_isolation_guard.py` (which asserts every `tests/*.py` is tracked and imports `tests.test_setup` first, statically via `ast`) passes.

**Isolation:** `import tests.test_setup` is the first import. The POST sweep touches routes that would otherwise run a real Stockfish analysis of the whole library or clear the repertoire, so `RepertoireManager`/`GameAnalyzer`/`LichessClient`/`ContentScout` are replaced with **inert** recorders (BRIEF hard rule 5). Every row is marker-scoped and purged in `tearDown`.

**Existing tests touched — no assertion weakened or deleted.** The brief warns that `test_api_security.py` contains ablation tests and that making one vacuous is a regression; two required a mechanical re-point, and both were strictly *widened*:

| test | what changed | why |
| :--- | :--- | :--- |
| `test_api_security.py::check_recursion_and_overflow_are_4xx` | injection point `module.migrate_played_at_units` → `module.normalize_played_at` | It reached the handler *only because the read route issued a write*. That call is gone (ABUSE-18). Same route, same two forced exceptions, same four assertions (400, no "Internal Server Error", the needle in `detail`, `x-content-type-options: nosniff`) plus the 200 control. |
| `test_api_security.py::check_engine_busy_is_503` | `retry-after == "5"` → `retry-after == expected_retry`, plus two new assertions | The literal `5` was the bug (ABUSE-22). Replaced by the invariant it stood in for: `ENGINE_BUSY_RETRY_AFTER == round(SYNC_POOL_WAIT_TIMEOUT)` and `SYNC_POOL_WAIT_TIMEOUT <= 30.0`. A drift between the two constants now fails here instead of hiding. **Stronger, not weaker.** |
| `test_api_contract.py::check_played_at_sort_order` | the stored-column assertion is now preceded by `module.startup_event()` | It verified the migration by calling the read route that used to do it. `startup_event()` is the caller that owns a migration. Every original assertion is kept, and the first two blocks are now a **stronger** claim: the sort order and the millisecond contract hold with a seconds-valued row still in the column and no write at all. |
| `test_api_contract.py::check_played_at_migration_is_idempotent` | the two `client.get(...)` triggers → two `startup_event()` calls | Same reason. The assertion `first == second == 1700000001 * 1000` is unchanged. |

**`tests/test_api_contract.py` and `tests/test_api_security.py` are green:**

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_api tests.test_api_contract \
    tests.test_api_security tests.test_api_boundary_hardening
Ran 28 tests in 7.772s
OK

$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_docs_contract tests.test_isolation_guard \
    tests.test_api tests.test_api_contract tests.test_api_security tests.test_api_boundary_hardening \
    tests.test_evaluator_stream tests.test_migration_safety tests.test_schema_integrity
Ran 129 tests in 12.120s
OK
```

Note `startup_event()` now calls `init_db()` in those two contract checks. That is the real boot path, and it is idempotent — the checks pass, and they pass repeatedly.

---

## 9. Full-suite result

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
Ran 575 tests in 29.954s
FAILED (failures=3, errors=2)
```

### The 5 remaining failures are **not mine**, and none is in a file I own

Proof — the **same suite run with the pre-fix `api.py` swapped back in** (and `SYNC_POOL_WAIT_TIMEOUT` back to 300.0) is **strictly worse**:

| `backend/api.py` | suite result |
| :--- | :--- |
| pre-fix snapshot (my new test module temporarily removed) | `Ran 574 … FAILED (failures=8, errors=2)` |
| patched (this patch) | `Ran 575 … FAILED (failures=3, errors=2)` |

The pre-fix run fails the same 5, **plus** `test_api_contract::test_00_contract_checks`, `test_api_security::test_all_security_checks` and two `test_docs_contract` checks. So this patch **removes 5 suite failures and adds none**.

| remaining failure | cause | owner |
| :--- | :--- | :--- |
| `FAIL test_concurrent_explorer_calls_never_drive_one_engine_from_two_threads` | `tree produced no evaluations: the lease was not taken` — the engine lease in `backend/repertoire.py`'s explorer path | FIXER-A / FIXER-T (`repertoire.py`) |
| `FAIL test_the_explorer_leases_from_the_shared_bounded_pool_and_hands_the_slot_back` | `pool.spawned == 0` — same path; the pool is never leased | FIXER-A / FIXER-T |
| `FAIL test_black_repertoire_tree_uses_the_black_players_personal_stats` | `the personal stats index was never built` — `repertoire.py`'s `get_user_position_stats` | FIXER-A / FIXER-T |
| `ERROR test_workflow_1_white_repertoire_and_sidelines` | `TypeError: '>' not supported between 'NoneType' and 'float'` — `get_repertoire_tree` returns `coverage_pct = None` | FIXER-A / FIXER-T |
| `ERROR test_workflow_2_black_repertoire_setup_and_multiple_openings` | same | FIXER-A / FIXER-T |

All five are in `backend/repertoire.py` code reached through `api.py` routes, i.e. a file I was told not to edit, and all five fail identically with the pre-fix `api.py` in place. They are FIXER-S's / FIXER-T's in-flight `blunders` natural-key work (`UNIQUE (game_id, ply, played_move)`) plus the explorer engine-lease work, exactly as the brief anticipated. **Reported, not papered over.**

The suite was run three times; the same 5 failed all three times, so this is not flakiness in my changes.

---

## 10. FIXER-L's boundary is not regressed

ATK-5 attacked `LocalBoundaryMiddleware` hard and found no bypass. My changes touch request handling, so those probes were re-run:

* **`check_local_boundary_still_holds`** is a deliberate **CONTROL** in the before/after table: it **passes on both** the pre-fix and the patched module. It re-runs nine refusal shapes (attacker origin, `https`, `Origin: null`, link-local metadata origin, `userinfo@`, trailing-dot host, non-numeric port, `Sec-Fetch-Site: cross-site`, `Sec-Fetch-Site: same-site`), the DNS-rebinding `Host` case, the same-origin 200, the absence of `Access-Control-Allow-Origin` on a refusal, the four security headers on a 200, `nosniff` on a 403, and `nosniff` on a body-cap rejection.
* **The full pre-existing `tests/test_api_security.py` suite is green** (28/28 with the api modules above), which includes all of FIXER-L's origin, preflight, fetch-metadata, host, header, size-cap, field-cap and ablation checks.
* The socket A/B confirms the refusals are unaffected: `Refused cross-origin …` / `Refused … Sec-Fetch-Site …` / `Refused … Host … is not a loopback name` all still fire, and the same-origin SPA shell and dashboard are still 200.
* My new `body_is_json` / `body_has_lone_surrogate` / size-cap logic sit **after** all four origin/host/preflight checks in `LocalBoundaryMiddleware.__call__`, so a hostile origin is still refused first and the log still names the real cause — FIXER-L's ordering decision is preserved.
* The new GET body buffering happens **only** when a `Content-Length` or `Transfer-Encoding` is declared, so the two long-lived SSE `GET`s are still never buffered.

**No ablation test became vacuous.** The two re-pointed checks were reviewed above; both keep every original assertion, and the one whose literal changed is now *stronger*.

---

## 11. Residual risks / not fixed

1. **A `GET` that declares a body is now buffered by the middleware.** A client that opens a connection, sends `GET` headers with a `Content-Length`, and never sends the body now blocks in the middleware rather than in the route. This is the same trade-off FIXER-L already documented for mutating requests (§13.5); it does not affect any real client or the SSE routes.
2. **`body_has_lone_surrogate` scans the whole body on the JSON-looking path.** O(n) with two passes (`lstrip`/`startswith`, then `json_max_nesting_depth`, then the surrogate walk). Bodies are capped at 2 MiB and the middle pass is skipped entirely when there is no `\u` in the body. At 2 MiB this is single-digit milliseconds.
3. **The 422 body is now lossy by design**: rejected values are truncated at 200 characters, lists/dicts at 20 items, nesting at 6 levels, and oversized ints are rendered as `<int with N bits>`. This is deliberate (it also closes a 2 MiB-in/2 MiB-out amplification) and the truncation markers are explicit in the body, but a client that parsed a full rejected value out of a 422 will see less. No client in this repo does.
4. **`explorer_cache` rows written by `repertoire.py` under a non-canonical key would still miss.** `_normalize_explorer_fen` is now provably `repertoire.normalize_fen`, so as long as `get_candidate_moves_2000` keys that table the same way, the three layers agree. If `repertoire.py` ever changes its normalisation, `check_explorer_singleflight_is_keyed_on_the_canonical_fen` fails, because it compares the two implementations directly.
5. **`SYNC_POOL_WAIT_TIMEOUT = 20.0` is a judgement call.** If a real bulk analysis legitimately needs longer than 20 s per position, callers now get a retryable 503 instead of waiting. `CHESS_ENGINE_WAIT_TIMEOUT` raises it without a code change. The previous 300 s was not a viable alternative — it exceeded any reasonable HTTP client timeout while advertising a 5 s retry.
6. **The two long `GET` SSE routes are unchanged**, but they are still `GET`s that write blunders. FIXER-L's `Origin`/`Sec-Fetch-Site` enforcement covers them for browser-driven requests; a direct non-browser client can still trigger an analysis. That is the documented no-auth design (§13 of `FIXER-L-01.md`), not a new gap.
7. **`_json_safe` is total for the types it can be *given*, but `exc.errors()` itself is wrapped.** If pydantic's error extraction raised, the handler falls back to `FALLBACK_VALIDATION_BODY` and still answers 422. The fallback is a module constant rather than a second code path, so it cannot itself fail to encode.
8. **The snapshot and probe scripts in `audit/scripts/` are evidence only.** `api_prefix_snapshot_FIXER-U.py` is a copy of the pre-fix module; nothing in the app imports it. `fixu_lib.py` is a shared helper for the FIXER-U probes.
