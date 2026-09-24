# SYNC-14 — the Lichess token is stored in plaintext with no length or format validation, cannot be cleared, and is replayed to `explorer.lichess.org`
- **Severity:** low
- **Type:** security
- **File:** backend/api.py
- **Line(s):** 226-232 (`SettingsPayload`), 324-330 (`update_settings`); backend/lichess_client.py:13-17, 101-120
- **Component:** settings / token handling
- **Repro script:** `audit/scripts/atk3_settings_resource_repro.py` (ATK3-14a/b/c)
- **Detected by:** ATTACKER-3 (SYNC)

## Expected
The token is not echoed back, is length-bounded, can be cleared, and is only sent to the
host it was issued for.

## Actual
```
  ATK3-14a — can the token be read back through any API?
  POST /api/settings -> 200 {"status":"ok","message":"Settings saved successfully."}
  GET  /api/settings -> {"lichess_username":"RobiDeli","has_token":true,...}
  GET  /api/account  -> {"connected":false,"username":"RobiDeli","perfs":{},"token_present":true}
  raw settings table : lip_SECRETTOKEN123 <- stored in PLAINTEXT
  token echoed by any GET /api/* ? False

  ATK3-14b — token validation and size
  empty string               HTTP 200 -> stored ''
  garbage token              HTTP 200 -> stored 'not-a-lichess-token' len=19
  integer token              HTTP 422 -> stored 'not-a-lichess-token' len=19
  null token (does not clear) HTTP 200 -> stored 'not-a-lichess-token' len=19
  200 KB token               HTTP 200 -> stored 'AAAA...' len=200000

  ATK3-14c — which hosts receive the Bearer token?
    https://lichess.org/api/account                    Authorization={'Bearer AAAAAAA...(200007 chars)'}
  -> the same Bearer token is attached to explorer.lichess.org as well
```

Good news: the token is **not** echoed by any GET (only `has_token` / `token_present`).
Problems:

* **no length cap** — a 200 000-character token is accepted and written verbatim to SQLite;
* **no format check** — `not-a-lichess-token` is accepted, so a typo is only discovered on
  the first 401, and the settings page reports `has_token: true`;
* **cannot be cleared** — `lichess_token: null` means "leave unchanged"
  (`if payload.lichess_token is not None:`), so a user who wants to disconnect must post an
  empty string and then still gets `has_token: bool("" or env_token)`;
* **plaintext at rest** in the `settings` table (the DB file sits next to the repo and is
  world-readable by default);
* **replayed to a second host** — `LichessClient.headers` is reused for
  `explorer.lichess.org` in `get_opening_explorer_stats` and in
  `repertoire.get_candidate_moves_2000`, so the Lichess API credential leaves the
  `lichess.org` origin.

## Root cause
```python
# backend/api.py:226-232
class SettingsPayload(BaseModel):
    lichess_token: Optional[str] = None
    ...
# backend/api.py:324-330
if payload.lichess_token is not None:
    set_setting("lichess_token", payload.lichess_token.strip())
```
No `Field(..., max_length=…)`, no pattern, no `""`-means-clear semantics, and
`set_setting` writes `str(value)` straight into the `settings` table.
```python
# backend/lichess_client.py:13-17
self.headers = {"User-Agent": "..."}
if self.token:
    self.headers["Authorization"] = f"Bearer {self.token}"   # reused for every host
```

## Exploit / escalation
1. **Token at rest:** any process that can read the app directory (Electron auto-spawns the
   backend from the repo, and `chess_improvement.db` is not encrypted) recovers a token that
   grants full read/write access to the user's Lichess account.
2. **Cross-host replay:** `explorer.lichess.org` is a different origin from the token's
   issuer; the token should only be attached to `lichess.org` calls.
3. **`.env` fallback:** `LichessClient._resolve_token` reads a project-root `.env` and takes
   the first line starting with `lip_`, or the value after the first `=`; combined with
   `git status` showing `.env` tracked, the token is also exposed to version control.
4. **Unbounded growth:** a 200 KB token is written on every save, and `_resolve_token` reads
   the whole file into memory on **every** `LichessClient()` construction — i.e. on every
   `/api/account`, every `/api/blunders/{id}` and every sync.
