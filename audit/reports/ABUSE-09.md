# ABUSE-09 — No request-size or field-size cap anywhere: 32 MB bodies accepted and 4 MB secrets/study names persisted into SQLite
- **Severity:** medium
- **Type:** security (resource exhaustion)
- **File:** `backend/api.py`
- **Line(s):** 196-206 (middleware stack: CORS + no-cache only, no body-limit middleware); 244-252 (`ScoutCheckLinePayload.moves: List[Dict[str, Any]]`, `ScoutCheckTreePayload.lines: List[Dict[str, Any]]`); 275-290 (`SettingsPayload` — every field an unconstrained `Optional[str]`); 1103-1108 (`StudyCreatePayload.name: str`)
- **Component:** request handling / storage
- **Repro script:** `audit/scripts/atk5_dos_repro.py` (sections B and D), `audit/scripts/atk5_misc_repro.py` (section D)
- **Repro script note:** all measurements are on the isolated temp DB
- **Detected by:** ATK-5

## Expected
A local app should still bound what a single request can make the server allocate
and persist. Body size, string lengths, and list lengths should all be capped
(`constr(max_length=…)`, `Field(max_length=…)`, `Field(max_length=…)` on lists, or
a `Content-Length` / streaming-limit middleware).

## Actual
There is no body-size middleware (`app.add_middleware` is called exactly once, for
CORS) and every payload model uses unbounded types.

### Observed — body size
```
D) unbounded request body (no Body-size middleware)
  body~1MB  -> 200     15.37 ms
  body~8MB  -> 200     75.13 ms
  body~32MB -> 200    282.95 ms
```
A single 32 MB POST to `POST /api/scout/check_line` is accepted, fully buffered
and JSON-parsed, at ~113 MB/s. Because there is no auth (ABUSE-03) and no
concurrency cap, ~50 concurrent 32 MB requests is >1.5 GB of transient RSS.

### Observed — unbounded list length
```
B) unbounded List[Dict[str, Any]] -> /api/scout/check_tree
  request body bytes: 280034        (2000 lines x 1 move)
  -> 200 in 1.34s  resp='[{"status":"new",…'
```
`check_tree` then runs `check_lines_overlap` → `check_line_overlap`, and each call
re-reads and re-indexes **every** row of `repertoire_moves`
(`repertoire.py:925-947` builds `moves_by_parent` / `moves_by_parent_3` per
call), so cost is `O(lines × repertoire_rows)`.

### Observed — unbounded string length, persisted
```
D) no request-size cap: 4 MB settings value / study name
  POST /api/settings 4MB lichess_token -> 200 {"status":"ok","message":"Settings saved successfully."}
  stored token length in DB: 4194304
  POST /api/repertoire/studies 4MB name -> 200 {"status":"success","study":{"id":2,"name":"AAAA…
```
`set_setting` (`backend/db.py:414-419`) writes it verbatim into the `settings`
table; `create_study` writes it into `repertoire_studies.name`, from where it is
returned by `GET /api/repertoire/studies` and injected into a `<select>` in
`frontend/app.js:266-271` and into every `study_name LIKE ?` query
(`repertoire.py:2620-2621`) — a 4 MB study name therefore degrades every trainer
query.

## Root cause
Payload models were written for a single trusted local user, so all strings are
`str`/`Optional[str]` and all collections are `List[Dict[str, Any]]`, with no
`max_length` constraints; and no middleware enforces a `Content-Length` ceiling.
`start.py:56` starts uvicorn with default `--limit-max-requests`/`h11-max-body-size`
behaviour (uvicorn does not cap body size by default).

## Exploit / escalation
1. **Memory DoS from any web page** (CORS reflects any origin, no auth): loop
   `fetch('http://127.0.0.1:38281/api/scout/check_line', {method:'POST', mode:'no-cors',
   body: 'A'.repeat(32*1024*1024)})` — each request is read into memory in full
   before the 200. Measured 283 ms and ~32 MB of transient allocation per request.
2. **Persistent storage abuse.** 4 MB per field, no cap: 250 requests fill ~1 GB
   of the user's SQLite file with junk `settings` rows / study names. The DB is a
   single local file that the user backs up by copying; there is no vacuum, no
   quota and no cleanup.
3. **Adjacent variants:** `POST /api/repertoire/move_meta` `comment`/`arrows` are
   unconstrained strings stored on every repertoire move; `arrows` is later
   `json.loads`-ed and rendered as board arrows, so an oversized value is parsed on
   every board render. `ScoutCheckTreePayload.lines` (ABUSE-18) is the same class.

**Required fix:** add a body-size middleware (reject `Content-Length > ~1 MB` and
stream-abort above it), constrain every `SettingsPayload`/`StudyCreatePayload`/
`UpdateMoveMetaPayload` string with `Field(max_length=…)`, and cap
`moves`/`lines` with `Field(max_length=…)`.
