# ABUSE-16 — FIXER-L's new `RequestValidationError` handler is itself a 500: 1–7 byte requests crash it, and the ABUSE-08 nesting guard is bypassed by lying about `Content-Type`
- **Severity:** medium
- **Type:** crash / security (unbounded resource: one full traceback per request)
- **File:** `backend/api.py`
- **Line(s):** 195-206 (`validation_exception_handler`, `sanitize()` at 197-204); 683-692 (the `Content-Type`-gated JSON-depth gate that ABUSE-08's fix depends on)
- **Component:** request parsing / global error handling / boundary middleware
- **Repro script:** `audit/scripts/atk5_19_validation_handler_500.py` (minimal triggers), `audit/scripts/atk5_22_exact_triggers.py` (trigger matrix), `audit/scripts/atk5_23_real_socket_ground_truth.py` (**ground truth over a real uvicorn socket**), `audit/scripts/atk5_21_surrogate_persistence.py` (escalation attempt — negative), `audit/scripts/atk5_17_boundary_attack.py` (guard cost + Content-Type sweep)
- **Detected by:** ATK-5

## Expected

ABUSE-08 was reported as: *"a ~2 KB nested JSON body causes an uncaught `RecursionError` → HTTP 500 on every
POST-JSON route."* Its contract, restated in the new `LocalBoundaryMiddleware`, is **"a hostile body is refused
with a 4xx, at the boundary, before pydantic or any route body sees it."**

`backend/api.py:195-206` is the last thing between a malformed request and the client, and it must therefore be
**total**: for *any* input it is handed, it must produce a well-formed response. Today it raises for two
independent, trivially-reachable classes of input, so a client mistake is reported to the browser as an internal
server error and a full traceback is written to the server log on every request.

## Actual

### Ground truth (real uvicorn socket, isolated temp DB, `audit/scripts/atk5_23_real_socket_ground_truth.py`)

```
  POST /api/scout/check_tree   body = "\xed\xa0\x80"            (5 bytes, Content-Type: application/json)
     status : HTTP/1.1 500 Internal Server Error
     body   : '{"detail":"Internal server error."}'

  POST /api/scout/check_tree   body = ["\xed\xa0\x80"]          (7 bytes, Content-Type: application/json)
     status : HTTP/1.1 500 Internal Server Error

  POST /api/scout/check_tree   body = \xff                       (1 byte,  Content-Type: text/plain)
     status : HTTP/1.1 500 Internal Server Error

  POST /api/scout/check_tree   body = 4000-deep nest            (        Content-Type: text/plain)
     status : HTTP/1.1 500 Internal Server Error        <-- ABUSE-08, still 500

  POST /api/scout/check_tree   body = 4000-deep nest            (        Content-Type: application/json)
     status : HTTP/1.1 400 Bad Request                  <-- the guard, when it runs
     body   : '{"detail": "Request body is nested too deeply (max 32)."}'
```

The server stays healthy afterwards (`GET /api/settings` → `200`), so this is **per-request, not fatal** — the
severity is *not* a DoS on the process; it is that a 1-byte client error becomes a 500 plus a logged
traceback, on **16 of 20** POST routes:

```
    500  /api/settings                      200  /api/repertoire/seed
    500  /api/repertoire/import_game_opening  200  /api/repertoire/clear
    500  /api/repertoire/move               200  /api/sync
    500  /api/repertoire/studies            200  /api/games/sync_recent
    500  /api/repertoire/move_meta
    500  /api/repertoire/train/attempt
    500  /api/repertoire/train/recent_opening
    500  /api/repertoire/import_line
    500  /api/repertoire/check_line
    500  /api/repertoire/check_tree
    500  /api/scout/analyze
    500  /api/scout/import_repertoire
    500  /api/scout/check_line
    500  /api/scout/check_tree
    500  /api/scout/favorite
    500  /api/train/evaluate_move
    -> 16/20 return 5xx from a 5-byte request
```

### The two escaping exceptions (server side)

**Root cause A — `sanitize()` has no case for `bytes`.** When pydantic cannot parse the body at all it reports
the *raw* body as `input`:

```
Unhandled ValueError while serving POST /api/scout/check_tree
  {'type': 'model_attributes_type', 'loc': ('body',),
   'msg': 'Input should be a valid dictionary or object to extract fields from',
   'input': b'\xff'}
...
  File ".../backend/api.py", line 206, in validation_exception_handler
    return JSONResponse(status_code=422, content={"detail": cleaned_errors})
TypeError: Object of type bytes is not JSON serializable
when serializing dict item 'input'
```

**Root cause B — `sanitize()` has no case for a `str` holding a lone UTF-16 surrogate.** `\ud800` is a legal
Python `str`; it only fails at the `.encode("utf-8")` that `JSONResponse` performs:

```
UnicodeEncodeError: 'utf-8' codec can't encode character '\ud800' in position 0: surrogates not allowed
```

`UnicodeEncodeError` is a subclass of `ValueError`, which is why the POST case degrades to a clean
`{"detail":"Internal server error."}` (the `value_error_handler` at `api.py:208` catches it) instead of a
dropped connection. Note the handlers do **not** nest: an exception raised *inside* the
`RequestValidationError` handler is not re-dispatched through the `ValueError` handler in every path, so this
is a real hazard, not a safety net.

### Why ABUSE-08 is not actually fixed

The nesting guard added for ABUSE-08 is gated on the declared content type:

```python
content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
if body and (content_type == "application/json" or content_type.endswith("+json")):
    depth = json_max_nesting_depth(body)
    if depth > self.max_json_depth:
        ... 400
```

Nothing enforces that declaration. Starlette parses the body as JSON regardless of `Content-Type`, so any
other value routes straight past the guard into the parser — and then into root cause A. Measured
(`atk5_17_boundary_attack.py` section B, `atk5_22_exact_triggers.py` section B):

| declared `Content-Type` | 1-byte `\xff` | lone surrogate | 4000-deep nest |
| :--- | :--- | :--- | :--- |
| `application/json` | 400 | **500** | 400 (guard) |
| `application/vnd.api+json` | 400 | – | – |
| *(absent)* | **500** | **500** | **500** |
| `text/plain` | **500** | **500** | **500** |
| `application/x-www-form-urlencoded` | **500** | **500** | **500** |
| `application/octet-stream` | **500** | **500** | **500** |

**Why the shipped security suite is green anyway.** Every hostile-body assertion in
`tests/test_api_security.py` declares `Content-Type: application/json`:

* `check_deeply_nested_json_is_4xx` (lines 438-461) — `headers={**LOCAL_HOST, "Content-Type": "application/json"}`
  on all three nesting cases.
* `check_oversized_body_is_413` (lines 464-475) — the one form-urlencoded case sends a **4 MB** body, which is
  stopped at the *size* gate (413) before any parsing happens, so it never exercises the guard either.

So the Content-Type gate is never tested with a *small* non-JSON body, and the surrogate is never tested at
all. Both holes sit in the gap between two tests that are individually correct.

## Escalation attempts that FAILED (reported so they are not re-tried)

* **Cannot poison storage.** I tried to persist a lone surrogate through `study name`, `study description`,
  `lichess_username`, `recent_learned_opening`, `move comment`, and `move arrows`
  (`audit/scripts/atk5_21_surrogate_persistence.py`). All 7 attempts return **422** and nothing lands: pydantic
  rejects the surrogate in a typed `str` field, and `set_setting()` straight to SQLite raises
  `UnicodeEncodeError` itself. Every read endpoint stays 200 afterwards. There is **no** stored, permanent
  denial of service here.
* **A surrogate inside a *valid* object is harmless.** `{"source":"\ud800","repertoire":"white"}` →
  `200 OK`. It is only the *body-level* parse error that carries the surrogate into `input`.
* **The GET query-parameter vector is a `TestClient` artefact, not a bug.** `soft.get(path, params={"fen":
  "\ud800"})` raises `UnicodeEncodeError` inside httpx, which led me to suspect a GET 500. Driven over a real
  socket, uvicorn decodes `%ED%A0%80` with replacement semantics and every one of the 12 GET routes answers
  `200`/`400`/`422` correctly. **Do not "fix" the GET path on the strength of the TestClient result.**
* **No cross-origin reachability.** The origin boundary (ABUSE-03) means a hostile web page cannot send these;
  the trigger is a non-browser client, a client that omits `Content-Type`, or any future/alternative client.
  This is a robustness-and-log-flooding defect, not a remote-unauthenticated-write chain.

## Adjacent variants an attacker could also try (all unfixed, all 500)

1. `b'"\xed\xb0\x80"'` — lone **low** surrogate, same path.
2. `b'[["\xed\xa0\x80"]]'` — surrogate nested one array deeper; still `model_attributes_type`, still 500.
3. `b'"a\ud800b"'` — surrogate embedded in a longer bare string; `UnicodeEncodeError` reports a different
   byte position, same 500.
4. `{"<surrogate>": 1}` — surrogate as an **object key**; `sanitize()` rebuilds dicts as `{str(k): ...}`, so
   the key survives into the response and dies in `json.dumps`.
5. Any *future* field of type `bytes`/`Decimal`/`set` in an error payload reproduces root cause A.

## Suggested fix shape (for the `api.py` owner)

* **A:** make the handler total rather than enumerating types — build the response with a `default=` that can
  never raise (e.g. `json.dumps(..., default=repr, ensure_ascii=True)`), or drop the echo entirely
  (`exc.errors(include_input=False)` / strip `input`/`ctx`), or add `bytes -> repr` and a
  lone-surrogate scrub to `sanitize()`. `ensure_ascii=True` (the default) already escapes surrogates *inside
  strings*, so the surviving failure is the **key** path and `bytes`; both must be covered.
* **B:** run the depth check on **every** body, not only declared-JSON ones — a body that is going to be
  parsed as JSON must be depth-checked regardless of what the client claims, and a body that is *not* going to
  be parsed need not be.
* Regression tests to add next to the existing ones: the same nesting and surrogate cases with
  `Content-Type` absent, `text/plain`, and `application/octet-stream`, asserting `status < 500` **and**
  `"Internal Server Error" not in response.text` (the assertion style already used at
  `tests/test_api_security.py:445`).
