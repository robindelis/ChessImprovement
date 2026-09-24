# ABUSE-21 — `LocalBoundaryMiddleware` never reads a GET body, so a GET carrying `Content-Length` bypasses both the 2 MiB cap and the ABUSE-08 nesting guard
- **Severity:** low
- **Type:** spec-violation (incomplete input validation)
- **File:** `backend/api.py`
- **Line(s):** 368 (`BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})`), 652 (`has_body = method in BODY_METHODS or "transfer-encoding" in headers`), 653-701 (the entire read/cap/depth block, all inside `if has_body:`)
- **Component:** the ABUSE-03/07/08/09 boundary middleware
- **Repro script:** `audit/scripts/atk5_re2_4_get_body.py` (sections 1-3), `audit/scripts/atk5_re2_3_boundary_edge.py` (section 6)
- **Detected by:** ATK-5 (re-attack pass 2)

## Expected

`LocalBoundaryMiddleware` is the single place where a hostile request body is supposed to be bounded, and its own
docstring says the body cap exists so that "the body cap must abort *while the body is being read*". A client
that declares a body on a verb the middleware does not consider body-bearing should still be bounded, or the
cap should be explicitly documented as body-methods-only. Today it is neither.

## Actual

The middleware decides whether to read (and therefore cap and depth-check) a body with a verb allowlist:

```python
has_body = method in BODY_METHODS or "transfer-encoding" in headers
if has_body:
    ...  # read, enforce max_body_bytes, enforce json_max_nesting_depth
```

`BODY_METHODS` is `{POST, PUT, PATCH, DELETE}`. A `GET` that declares `Content-Length` is therefore never read.
Measured over a real uvicorn socket, same body, two verbs:

```
[1] the cap itself: GET with a body, vs POST with the same body
    GET   with a 3 MiB Content-Length -> ['200']
    POST  with a 3 MiB Content-Length -> ['413']

    and the depth guard on a GET (a POST would be refused at 400):
    GET   with a 4000-deep JSON body   -> ['200']
    POST  with a 4000-deep JSON body   -> ['400']
```

Both the size cap **and** the ABUSE-08 nesting guard are skipped on a GET. A chunked GET *is* caught, because
`transfer-encoding` is checked explicitly — so the protection depends on which framing header the client
happens to use, which is not a security property:

```
    GET with 3 MiB chunked (no Content-Length)  -> ['413']
```

## Why this is LOW, and what I could NOT make it into

I tried hard to escalate this and could not. Reporting the negative results so the next agent does not repeat
them:

* **No request smuggling.** A GET whose body is never read does not desynchronise the connection — uvicorn
  drains the unread body before the next request. Measured on one keep-alive connection:
  ```
  [2] SMUGGLING: does the unread GET body desynchronise a keep-alive connection?
      responses on the connection: ['200']
      lichess_username now: None  (not smuggled)
  [3] under-declared GET body (Content-Length smaller than the bytes sent)
      -> ['200']  (no desync)
  ```
  The smuggled `POST /api/repertoire/clear` was **not** executed, and the under-declared-body case produced
  exactly one response. A 404 would have been ambiguous; I used an observable settings write instead.
* **No memory amplification.** An unread body is not buffered, so 3 MiB costs nothing. The 2 MiB cap exists to
  bound *buffering*; skipping it for a body that is never buffered costs nothing today.
* **No GET route in this app consumes a body.** Every GET handler takes query parameters only, so the skipped
  depth check has nothing to protect.

It is therefore a real but currently harmless gap: a defence that is stated unconditionally and implemented
conditionally, which is exactly the shape that becomes exploitable the moment someone adds a GET route that
reads a body.

## Root cause

`has_body` is a verb allowlist where the correct predicate is "does this message declare a payload" —
`Content-Length` **or** `Transfer-Encoding`, exactly as the code already does for the second of the two. The
`transfer-encoding` disjunct shows the author knew about framing headers; `content-length` was simply not added
to the same test, even though it is read two lines later at `:654`.

## Exploit / escalation

How it was reached: `GET /api/settings HTTP/1.1` + `Content-Type: application/json` + `Content-Length: 3000000`.
No authentication, no cross-origin requirement (the origin boundary is checked before the body block, so a
refused origin never reaches this).

Adjacent weaker variants of the same root cause:

1. **Any future GET-with-body route inherits the bypass.** A `GET` that reads a stream, a filter blob, or a
   large query-in-body would be entirely uncapped and un-depth-checked.
2. **The two framings disagree.** `Content-Length` skips the cap; `Transfer-Encoding: chunked` gets it. A
   client picks which defence applies. That inconsistency is the finding, independent of severity.
3. **The depth guard's own comment overpromises.** `json_max_nesting_depth`'s docstring (`:490-493`) describes
   it as rejecting "a hostile body *before* pydantic recurses into it", with no mention that the check is
   verb-conditional. Anyone reasoning about ABUSE-08's fix from the docstring would draw the wrong conclusion —
   which is how this survived the last pass.

## Suggested fix shape (for the `api.py` owner)

* One-line fix at `api.py:652`:
  ```python
  has_body = (method in BODY_METHODS
              or "transfer-encoding" in headers
              or "content-length" in headers)
  ```
  This makes the two framing headers agree and closes the gap for any future route.
* If a GET body is deliberately not supported, say so explicitly in the middleware docstring *and* reject it
  (413/400) rather than silently ignoring it — silently ignoring a declared payload is what makes the boundary
  hard to reason about.
* Regression test to add next to the existing ones in `tests/test_api_security.py`: the 3 MiB and
  4000-deep-nest cases with `method="GET"`, asserting `413` and `400` respectively. Both assertions already
  exist for POST; the GET variants are the gap.
