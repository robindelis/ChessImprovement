# ABUSE-13 — No security headers on any response (no CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy) while the app renders unescaped remote HTML
- **Severity:** medium
- **Type:** security
- **File:** `backend/api.py`
- **Line(s):** 197-206 (the only response-header middleware — `no-cache` for `.js`/`.css`/`.html` and `/`); 1494 (`app.mount("/", StaticFiles(...))`)
- **Component:** HTTP hardening
- **Repro script:** `audit/scripts/atk5_cors_noauth_repro.py` (section 6), `audit/scripts/atk5_static_exposure_repro.py` (section E)
- **Detected by:** ATK-5

## Expected
Even a localhost app should emit a baseline set of response headers, especially a
`Content-Security-Policy` that forbids inline/`on*` handlers — that alone would
neutralise ABUSE-04 — plus `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`.

## Actual
`backend/api.py` installs exactly two middlewares: `CORSMiddleware` and a
`no-cache` header decorator that only touches paths ending in `.js`, `.css`,
`.html` or `/`. No security headers are ever added.

### Observed (`atk5_cors_noauth_repro.py` section 6, real `GET /`)
```
6) Security headers present on a static response?
  content-type: text/html; charset=utf-8
  accept-ranges: bytes
  content-length: 69747
  last-modified: Sat, 26 Sep 2026 13:36:50 GMT
  etag: "477c919cd34ef1946dd7237bfc15bf28"
  access-control-allow-origin: http://attacker.example
  access-control-allow-credentials: true
  vary: Origin
  cache-control: no-cache, no-store, must-revalidate
  pragma: no-cache
  expires: 0
  MISSING? x-content-type-options: <<ABSENT>>
  MISSING? x-frame-options: <<ABSENT>>
  MISSING? content-security-policy: <<ABSENT>>
  MISSING? strict-transport-security: <<ABSENT>>
  MISSING? referrer-policy: <<ABSENT>>
  MISSING? permissions-policy: <<ABSENT>>
  MISSING? cross-origin-opener-policy: <<ABSENT>>
  MISSING? x-xss-protection: <<ABSENT>>
```

Note also that `Access-Control-Allow-Origin` is attached to the **static frontend
document** as well, so the HTML shell itself is readable cross-origin.

## Root cause
Security headers were never considered: the only custom middleware is a
cache-busting helper added for the zero-build dev workflow, and the author relied
on the loopback bind (plus, in the desktop shell, on
`contextIsolation`/`sandbox`, which do not constrain what the *page itself* may
inject).

## Exploit / escalation
1. **ABUSE-04 has no second line of defence.** The verified
   `<img src=x onerror=…>` payload executes because there is no CSP; adding
   `script-src 'self'` (or at least `script-src-attr 'none'`) would block it even
   while the sink is unfixed.
2. **Adjacent — clickjacking.** With no `X-Frame-Options`/`frame-ancestors`, the
   local app can be iframed by any page (the `Access-Control-Allow-Origin` header
   does not affect framing), and a transparent overlay can turn any user click in
   the Builder into a click in an attacker's frame.
3. **Adjacent — MIME sniffing.** `assets/` is served by `StaticFiles` with
   `content-type` guessed from the extension and no `nosniff`; a file dropped in
   `frontend/` (e.g. a `.txt` or a polyglot) could be re-interpreted as script in
   the Electron renderer.
4. **Adjacent — `Referrer-Policy`:** the app embeds
   `https://www.youtube.com/embed/${videoId}?enablejsapi=1` and links out to
   `https://lichess.org/<gameId>` (`frontend/repertoire_builder.js:1013`), leaking
   the local URL (and therefore the fact that the user runs this app) to YouTube
   and Lichess.

**Required fix:** add a middleware that sets
`Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-src https://www.youtube.com; img-src 'self' data: https://i.ytimg.com; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer` and `Cross-Origin-Opener-Policy: same-origin` on
every response.
