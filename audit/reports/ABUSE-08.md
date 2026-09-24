# ABUSE-08 — Deeply nested JSON body causes an uncaught `RecursionError` → HTTP 500 on every POST-JSON route
- **Severity:** medium
- **Type:** crash
- **File:** `backend/api.py`
- **Line(s):** 37-48 (the only body-level exception handler, `RequestValidationError`); route bodies at 243-252 (`ScoutCheckLinePayload`, `ScoutCheckTreePayload`, `ScoutImportPayload`, `TrainerMovePayload`, `UpdateMoveMetaPayload`, `SettingsPayload`, `Dict[str, Any]` bodies)
- **Component:** request parsing / global exception handling
- **Repro script:** `audit/scripts/atk5_route_fuzz.py`, `audit/scripts/atk5_dos_repro.py` (section C)
- **Detected by:** ATK-5

## Expected
A malformed or hostile body must produce a 4xx. The app installs
`@app.exception_handler(RequestValidationError)` and
`@app.exception_handler(ValueError)` but **no** catch-all, so any exception that
escapes a route body becomes a bare `500 Internal Server Error` (and, in
production, a full traceback on stderr for an unauthenticated caller to trigger
at will).

## Actual
### Observed
`atk5_route_fuzz.py` fired ~1150 hostile requests across all 30 routes and found
one clean 500; `atk5_dos_repro.py` section C localised it:

```
C) deeply nested JSON body -> RecursionError?
  depth=100      -> 422     3.13 ms  '{"detail":[{"type":"dict_type",…}]}'
  depth=1000     -> 500     5.00 ms  'Internal Server Error'
  depth=5000     -> 500     5.39 ms  'Internal Server Error'
  depth=50000    -> 500    14.03 ms  'Internal Server Error'
```

Confirmed exception type with `TestClient(raise_server_exceptions=True)`:

```
E) deeply nested JSON -> 500; which exception?
  depth=1000  -> EXC RecursionError: maximum recursion depth exceeded
  depth=20000 -> EXC RecursionError: maximum recursion depth exceeded
```

The threshold sits between 100 and 1000 levels of nesting and is reached with a
**~2 KB request body**:

```
  depth=1000  body = '{"repertoire":"white","moves":' + '['*1000 + ']'*1000 + '}'   # 2 004 bytes
```

Any route whose body model is permissive reproduces it, e.g.
`POST /api/scout/check_line`, `POST /api/scout/check_tree`,
`POST /api/repertoire/import_line`, `POST /api/repertoire/train/recent_opening`
(body is `Dict[str, Any]`), `POST /api/settings`, and any endpoint using
`List[Dict[str, Any]]` — pydantic recurses through `Dict[str, Any]` /
`Any` without a depth cap.

## Root cause
Pydantic v2 recursion on `Any`-typed containers is not depth-limited, and
`RecursionError` is not an `Exception` subclass that the registered handlers
cover, and there is no `@app.exception_handler(Exception)` fallback. Note this is
the *same* `except`-coverage hole that makes `concurrent.futures.CancelledError`
(also a `BaseException`) escape in ABUSE-13.

## Exploit / escalation
1. **Cheap, unauthenticated 500 generator.** 2 KB per request, no rate limit, no
   auth. Because CORS reflects any origin (ABUSE-03), a web page can trivially
   fill the server's error log with tracebacks and make every dashboard widget
   that shares the connection fail.
2. **Side effect on the worker threadpool.** `RecursionError` unwinds through
   Starlette's `anyio.to_thread` worker; repeated raising near the recursion limit
   can leave thread state and the SQLite `conn` objects opened in the handler
   frames un-closed (`get_db()` is called *inside* the route body, not in a
   `finally`).
3. **Adjacent variants:** the same handler gap is hit by
   * `GET /api/blunders?limit=10**30` → `OverflowError` → 500 (ABUSE-07)
   * a `Dict[str, Any]` body containing 50 000 nested dicts → the same
     `RecursionError` on `POST /api/repertoire/train/recent_opening`
   * binary garbage and malformed JSON are handled correctly (422), so the bug is
     specifically *nesting*, not syntax.

**Required fix:** add a catch-all
`@app.exception_handler(Exception)` (and one for `BaseException`-derived
`RecursionError`/`CancelledError`) returning a generic 500 with no internals, and
reject bodies whose nesting exceeds a small limit before pydantic sees them (or
declare explicit `concrete` types instead of `Dict[str, Any]`/`List[Dict[str, Any]]`).
