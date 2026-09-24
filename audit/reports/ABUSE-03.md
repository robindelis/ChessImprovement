# ABUSE-03 — CORS reflects any Origin with credentials on a completely unauthenticated API: any web page can read the user's chess history and wipe the repertoire
- **Severity:** high
- **Type:** security
- **File:** `backend/api.py`
- **Line(s):** 190-195 (`CORSMiddleware`), plus the whole route table (no auth dependency anywhere); destructive handlers at 1302 (`/api/repertoire/clear`), 1111-1112 (`DELETE /api/repertoire/studies/{id}`), 324 (`POST /api/settings`), `/api/repertoire/seed`
- **Component:** CORS / authn / CSRF
- **Repro script:** `audit/scripts/atk5_cors_noauth_repro.py`
- **Detected by:** ATK-5

## Expected
A localhost-only personal app that performs destructive, unauthenticated writes
must either (a) require a per-install secret / `Origin` check on every route, or
at minimum (b) not reflect arbitrary origins. `allow_origins=["*"]` combined
with `allow_credentials=True` is the worst of both: Starlette cannot answer `*`
when credentials are allowed, so it **echoes the caller's `Origin`** and marks
the response `Access-Control-Allow-Credentials: true` — i.e. every origin on the
internet is granted full read/write access to `http://127.0.0.1:38281`.

## Actual
`backend/api.py:190-195`:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Observed with `atk5_cors_noauth_repro.py` (isolated temp DB, `TestClient`; the
live server on 38281 was never contacted):

```
1) CORS preflight for the destructive endpoint, from a foreign origin
  status: 200
   access-control-allow-methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT
   access-control-max-age: 600
   access-control-allow-credentials: true
   access-control-allow-origin: http://attacker.example
   access-control-allow-headers: content-type

2) Any origin is reflected + credentials allowed
  Origin=http://attacker.example  -> ACAO='http://attacker.example'  ACAC='true'
  Origin=https://evil.test        -> ACAO='https://evil.test'        ACAC='true'
  Origin=null                     -> ACAO='null'                     ACAC='true'
  Origin=http://169.254.169.254   -> ACAO='http://169.254.169.254'   ACAC='true'

3) READ chain
  /api/games/recent -> 200 total games: 307
   sample record keys: ['eco','game_url','id','moves_san','opening_name', ... 'opponent_name','opponent_rating','user_rating']
   opponent_name: renen12345 rating: 1855 opening: Philidor Defense: Steinitz Variation
  /api/blunders?limit=-1 -> 200 rows returned: 153

4) WRITE chain (no credentials, no CSRF token, no Origin check)
  repertoire_moves before: 1
  POST /api/repertoire/clear (no credentials) -> 200
     {"status":"ok","message":"Entire repertoire database reset successfully.","deleted_count":1}
  repertoire_moves after : 0
```

`atk5_cors_noauth_repro.py` also greps the route module for auth primitives:

```
  Depends(           occurrences in backend/api.py: 0
  Security( / HTTPBearer / APIKeyHeader / OAuth2 / Authorization : 0
```

**No route in the application declares any authentication or authorisation
dependency.** `AGENTS.md` §9.3/§9.4 treats "RobiDeli's two hand-built White
studies (Anti French, Caro-Kann Fantasy Variation)" as a protected asset; the
`POST /api/repertoire/seed?force=true` endpoint (which overwrites them) and
`POST /api/repertoire/clear` (which deletes them) are both reachable from any
web page the user visits.

## Root cause
The CORS middleware was configured as "allow everything, with credentials", and
because the app is a single-user local tool the author assumed loopback binding
(`start.py:53`, `--host 127.0.0.1`) was sufficient protection. Loopback binding
does **not** stop a browser on the machine from issuing cross-origin requests,
and `Access-Control-Allow-Origin: <attacker>` + `Allow-Credentials: true` removes
the SOP read restriction that would otherwise make those requests useless.

## Exploit / escalation
1. **Exfiltration.** A page on any `http://` origin (or `null`, e.g. from a
   `data:`/sandboxed iframe) runs
   `fetch('http://127.0.0.1:38281/api/games/recent?limit=100')` and reads the
   user's full Lichess game list — opponent handles, ratings, openings, dates —
   plus `/api/blunders` and `/api/repertoire/studies`.
2. **Destruction.** The same page runs
   `fetch('http://127.0.0.1:38281/api/repertoire/clear', {method:'POST'})`
   (observed: `deleted_count: 1`, repertoire → 0) or
   `DELETE /api/repertoire/studies/{id}?delete_moves=true` (observed:
   `{"status":"success","deleted_moves":1,"kept_moves":0}`), irreversibly
   destroying the studies `AGENTS.md` §9.3 says must never be lost.
3. **Adjacent — credential/state overwrite.** `POST /api/settings` with
   `{"lichess_username": "attacker"}` re-points the sync target, and
   `POST /api/scout/analyze` with an attacker URL writes attacker HTML into
   `scouted_content`, which is then rendered with `innerHTML` (see ABUSE-04).
   There is no CSRF token either, so even a *non*-CORS form POST from a page
   that cannot read the response still succeeds for the write endpoints.

**Required fix:** replace `allow_origins=["*"]` with an explicit allowlist (or
remove the middleware entirely), require a random per-install shared secret on
every mutating route (checked against a `settings` row or a launch-time header
injected by `electron/preload.js`), and add `SameSite`-style origin checking to
the destructive handlers.
