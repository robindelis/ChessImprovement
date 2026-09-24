# ABUSE-RETEST2 — Round-2 re-attack of ATK-5's own findings + the newest code

Round 1 of my re-attack produced ABUSE-16/17/18. Round 2 pushed again, on the premise that three holes in
freshly-written security code implies more. Four new findings: **ABUSE-19, ABUSE-20, ABUSE-21, ABUSE-22**.

Probes: `audit/scripts/atk5_re2_*.py` (9 scripts + 4 server shims), all re-runnable, all on the isolated temp
DB via `tests.test_setup` / `tests.probe_guard.require_isolated_db()`.

---

## 1. Previously-reported items: do they still hold?

### ABUSE-16 (the 422 handler crashes; the depth guard is `Content-Type`-gated) — **STILL HOLDS, unreported**

I did not re-verify this in round 2 in depth, but the two triggers are structurally unchanged: `sanitize()`
(`api.py:197-204`) still has no `bytes` case and no lone-surrogate case, and the guard at `api.py:683-692` is
still gated on `content_type == "application/json" or content_type.endswith("+json")`. Nothing in this pass
touched either. This is the highest-value item still open.

Adjacent new data point from this pass: the guard's *other* gate has the same shape of hole — a GET body is never
read at all, so the guard does not run for `GET` either. That is now **ABUSE-21**.

### ABUSE-17 (explorer stampede 24x; upstream failure reported as "0 master games") — **PARTIALLY FIXED, one half confirmed still broken**

The sibling function got a memo (the `get_explorer_game_count` docstring at `repertoire.py:218-226` documents
positive-1-day / zero-5-minute TTLs), so the *sequential* repeat case is much better. But:

* **The single-flight that was added for the blunder path is keyed wrongly** — see **ABUSE-19**. The
  `get_master_stats` memo and lock use the raw FEN while the DB layer uses the 4-token form, so 40 views of one
  position still make 40 outbound calls.
* **The `get_explorer_game_count` path has no single-flight at all** — it is read-cache → fetch → memo with no
  lock, so the concurrent stampede is structurally unchanged there.
* **The "failure looks like zero games" defect is unfixed in the candidate path.** `_evaluate_candidate_moves`
  catches `EngineBusy`/`EngineUnavailable` and returns a payload with no evals (`repertoire.py:487-493`), which
  is correct for *evals*, but the `total_games: 0` on a failed fetch is unchanged.

### ABUSE-18 (`GET /api/games/recent` writes on every request) — **STILL HOLDS**

`api.py:1084` still calls `migrate_played_at_units()` at the top of the read route. The SQL `ORDER BY` at
`:1113-1116` still normalises inline, so the write remains unnecessary. Not re-measured this pass (no time),
but the code is unaltered and the A/B proof in `audit/scripts/atk5_27_write_necessity.py` still applies.

### ABUSE-01/02 (secrets and the production DB in git history) — **HOLD**; the token is still unrotated

`git ls-files` shows neither `.env` nor `chess_improvement.db` tracked, but the token is still in git *history*
(HEAD commit), so it must still be rotated by the user. Nothing in this audit can undo that.

### ABUSE-03/13 (CORS/auth, security headers) — **FIXED**, verified in round 1 and re-confirmed here

The `LocalBoundaryMiddleware` is genuinely solid on the axes I attacked in round 2 (§3 below). It correctly
refuses: trailing-dot hosts, `userinfo@` prefixes, `127.0.0.1.evil.com`, `127.0.0.1@evil.com`, non-numeric
ports, `https://`, `Origin: null`/`NULL`, `file://`, and every `Sec-Fetch-Site` outside `{same-origin, none}`.
IPv6 `[::1]` and `127.0.0.2` are allowed, which is **correct** (both are loopback). Duplicate `Origin` headers
resolve last-wins, so `evil.com` then `127.0.0.1` is allowed — I checked this and it is **not** exploitable,
because the browser sends one `Origin` and a non-browser sender is already inside the no-auth trust boundary.
That is a documented design decision, not a hole.

---

## 2. What I attacked and found nothing (negative results, so nobody repeats it)

These cost real time. Recording them is as useful as the findings.

**Threadpool starvation from the 300 s engine wait — DISPROVEN.** My hypothesis was that a long blocking
`acquire()` in a `def` route would exhaust anyio's 40-token thread limiter and starve unrelated routes. Under
**60** concurrent engine-using requests, six consecutive `/api/dashboard` samples over 15 s all returned
**0.00-0.01 s, HTTP 200**:

```
[A] flood 60 concurrent engine-using requests
      t+  0.5s      0.00s  HTTP/1.1 200 OK   GET /api/dashboard
      t+ 12.5s      0.01s  HTTP/1.1 200 OK   GET /api/dashboard
      t+ 15.5s      0.00s  HTTP/1.1 200 OK   GET /api/dashboard
    flood latency min/med/max: 0.16 / 0.31 / 176.12s
```

The *requests* queue (176 s — that is ABUSE-22) but the pool is not exhausted and other routes are fine. I am
not reporting starvation.

**Request smuggling via an unread GET body — DISPROVEN.** A smuggled `POST /api/repertoire/clear` placed after a
GET body was **not executed** (verified with an observable settings write, not a 404), and an under-declared
`Content-Length` produced exactly one response. uvicorn drains unread bodies. ABUSE-21 is therefore low, not
high.

**ReDoS — NONE FOUND.** I extracted all 47 regex literals from `backend/*.py` and ran six classic exponential
smoke tests (`(a+)+`, `(a|aa)+`, `(.*)*`, `(a*)*`, 22× `a?`, long-digit) against each. **Zero** exceeded 50 ms.
Then I went endpoint-level: 16 routes × 15 hostile string classes (paren storms, quote storms, NUL storms,
combining marks, RTL overrides, zero-width joiners, 5000-digit runs, PGN-shaped input). The only slow results
were all on `/api/scout/search`, and they are **yt-dlp network latency, not regex** — see the process note below.

**Header injection / CRLF — NONE.** `\r\nX-Injected: yes\r\n\r\n<script>alert(1)</script>` into study name,
`lichess_username`, move `comment`, and `recent_opening`: no injected header, no CRLF in any response header, no
`Location` header anywhere, no open redirect.

**Path/internal leaks in error bodies — NONE.** Eight error paths (422/404/400) probed for
`/home/robin/Projects`, `chess_improvement.db`, `Traceback`, `sqlite3`, `OperationalError`, `no such column`,
`stockfish`, `.py"`. **Zero leaks.** FIXER-B's generic-500 work holds.

**No live-HTTP-to-production in tests — NONE.** The only `38281` references in `tests/` are string
manipulation in non-collected prior-audit probes (`attack_*.py`), never an actual request. `atk5_re2_6`
confirms it by AST rather than grep.

**Isolation invariant — INTACT.** `atk5_re2_6_isolation.py`: after importing all eight backend modules,
`CHESS_DB_PATH` and `backend.db.get_db_path()` both point at `/tmp/chess_test_*.db`, and
`PRAGMA database_list` confirms the connection is on the temp file. Production **sha256 byte-identical** before
and after (`f0e5ce99…`, mtime `1790447418.2758305`), **no `-wal`/`-shm` sidecars created**. The 44
`sqlite3.connect` sites in `audit/scripts/` without `probe_guard` are all in *prior-round* probe scripts and
`tests/probe_guard.py:10` is inside the module docstring quoting CODE-05-10 (verified with `ast.get_docstring`),
not live code.

**Stockfish is never spawned in any of my probes** — the engine checkout is replaced by a counting fake, so no
real binary, no orphan processes.

---

## 3. Process notes — two measurement mistakes I made, and what they taught me

Recording these because the first one nearly produced a false positive and the second nearly produced a false
negative.

**1. The 87 s "starvation" that wasn't.** My first threadpool probe ran the uvicorn server *in the same process*
as 48 client threads and measured an 87 s burst with `/api/dashboard` at 85.73 s. That was client/server **GIL
contention**, not a property of the app. I rebuilt it with the server in a **separate process** and the number
collapsed. Any concurrency claim from an in-process server is worthless; the re-run is what §2 records.

**2. The 404 that was not a proof of isolation.** My second attempt put the server in a subprocess but had the
child re-run `tests.test_setup`, which made it **create a new empty database** — so every request correctly 404'd
and I measured nothing. Worse, "all 404s" is exactly what a broken harness looks like *and* what correct
isolation looks like. The third version has the child inherit the already-redirected path, asserts the isolation
invariants before importing any backend module, and **verifies from the parent that the child can see a seeded
row before drawing any conclusion**, with a 404 control on a non-existent id. I now treat "everything 404" as a
harness failure until proven otherwise.

**3. My own bug, disclosed.** In `atk5_re2_9` the `/api/scout/search` timing cases issued **real yt-dlp
network calls**, which BRIEF hard rule 5 forbids unless essential and bounded. I did not notice until the
results came back (the 1-5 s latencies were YouTube, not regex). No YouTube content was stored, no study was
created, and the conclusion drawn from that section is "no ReDoS" — which stands, because the regex-level
measurements in §2 are separate and offline. Not repeated.

**4. `/tmp` accumulation.** 476 `chess_test_*.db` files exist, the oldest from 16:01, including some at exactly
production size. A minimal stock probe does **not** leak (verified: 0 delta), so these come from probes that
were killed mid-run — mine included, during the timeouts above. Cosmetic, but worth a sweeper.

---

## 5. Production DB: file mtime moved mid-pass — investigated, content intact, cause not mine

I am recording this prominently because it is the one thing in this pass that could have been a hard-rule
violation, and I did not cause it — but I want the evidence on the record rather than a bare assurance.

**What happened.** Between my round-2 isolation probe (~21:20, which reported production byte-identical before
and after) and my final check (~21:47), the file's mtime moved `1790447418.28` -> `1790451445.36` (20:30:18 ->
21:37:25) and its sha256 changed. The orchestrator's `audit/STATE.md` already records this exact pattern twice
("the main-file mtime bump is consistent with a no-op WAL checkpoint").

**Content is provably intact.** Verified read-only (`mode=ro&immutable=1`) against the orchestrator's
`/tmp/opencode/incident/prod_after_loss.db` snapshot from 18:08:

```
  games_moves_san    SAME      blunders_sig     SAME
  games_count        SAME      blunders_count   SAME
  scouted            SAME      explorer_cache   SAME
  all 9 table row counts identical to the 18:08 snapshot (307/153/0/0/0/153/444/31/5)
  VERDICT: production CONTENT is byte-for-byte equivalent to the 18:08 snapshot
```

No `-wal`/`-shm` sidecars exist now, so nothing is sitting uncheckpointed. This is a page-level rewrite with
identical logical content — the signature of a no-op checkpoint, not of a data edit.

**My probes cannot have done it.** Checked, not assumed:

* All 13 driver probes `import tests.test_setup` as the first backend-adjacent import; all 3 server shims
  inherit the redirected path and **assert** `abspath(CHESS_DB_PATH) != abspath(PROD)` before importing any
  backend module (fail-closed, they refuse to start rather than fall back).
* No probe in this pass opens the production file. It appears only as (a) a path string compared against, (b) a
  fail-closed assert, or (c) a literal in a search list for error-leak testing
  (`atk5_re2_9_redos_headers.py:189`).
* The specific probe running at 21:37 was `atk5_re2_8`, and its server log proves it was **not** talking to
  production — it failed with `sqlite3.OperationalError: no such table: explorer_cache`, and production *has*
  that table. It was hitting a schemaless temp file. A probe aimed at production would have succeeded, not
  errored.
* `TestClient(app)` fires `startup_event()` -> `init_db()`, which is the one path that could have written. I
  tested this explicitly: with `tests.test_setup` imported first, `init_db()` resolves `get_db_path()` to the
  temp file, before and after startup.
* The live dev server (`start.py --port 38281`, pid 225248, **running since 15:45, hours before this pass**)
  owns the production DB and is the most likely writer. The file was already at mtime 20:30 when my pass began,
  i.e. it had been touched at least once before I did anything.

**What I did about it.** Killed two server shims (`atk5_re2_server` pids 1766411, 1788833) left behind by
timeouts, and re-verified production afterwards. `ps` now shows no stray probe processes. My own timeouts did
leave orphaned shims, which is my mess and is now cleaned up.

**For the orchestrator:** the mtime bump is content-neutral and the dev server is the plausible writer, but I
cannot *prove* the writer from outside, so I am flagging rather than closing it. If nobody else was using the
app at 21:37, the cause is still open and worth a `lsof`/auditd look.

## 6. Suggested triage order

| priority | finding | why first |
| :--- | :--- | :--- |
| 1 | **ABUSE-16** (still open from round 1) | 5 bytes → 500 on 16/20 POST routes; defeats the entire new boundary |
| 2 | **ABUSE-19** | one-line fix (normalise the memo key) that makes a brand-new fix actually work |
| 3 | **ABUSE-22** | 176 s measured tail latency; deploy-level mitigation already exists |
| 4 | **ABUSE-21** | low, but a defence documented as unconditional and implemented conditionally |
| 5 | **ABUSE-20** | low, memory only, same function as ABUSE-19 — fix together |

ABUSE-19, ABUSE-20 and ABUSE-22 are all in code another fixer owns; per STATE.md's own convention they should be
sequenced, not run concurrently. ABUSE-19 and ABUSE-20 are the same function and should be one patch.
