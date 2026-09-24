# ABUSE-18 — `GET /api/games/recent` opens a database WRITE transaction on every request, and it does not need to: the ordering it protects is already correct without it
- **Severity:** medium
- **Type:** spec-violation (read endpoint writes) / race (availability)
- **File:** `backend/api.py`
- **Line(s):** 1084 (`migrate_played_at_units()` called at the top of the read route); 170-190 (the function itself — `UPDATE games SET played_at = played_at * 1000 WHERE ...`); 1113-1116 (the SQL `ORDER BY` that already normalises); 1129 (`normalize_played_at()` in the row loop, which also already normalises); 726 (the legitimate caller — `startup`)
- **Component:** games API / boot-migration helper
- **Repro script:** `audit/scripts/atk5_26_write_on_read.py` (write trace + lock-blocking + growth cost), `audit/scripts/atk5_27_write_necessity.py` (A/B proof the write is unnecessary)
- **Detected by:** ATK-5

## Expected

Two things, per `AGENTS.md` and the repo's own stated contract:

1. **A read route must not write.** The codebase already decided this: `AGENTS.md` §4B.4 documents that
   `get_game_gaps()` "is a read endpoint … never persists a probe result", and
   `tests/test_repertoire_write_safety.py` exists purely to enforce "read endpoints perform no writes".
2. **A per-request cost must buy something.** A table-wide `UPDATE` on the hottest read route is only
   acceptable if the read is actually wrong without it.

Measured: the write is real, it fires on every request, and it buys nothing.

## Actual

### 1. The read route issues a WRITE, every time (proved by tracing the app's own connection)

```
[A] does the read route actually issue writes?  (trace every app connection)
    HTTP 200; SQL on the app's own connection(s): 5  (2 SELECT / 1 WRITE)
    of which WRITE statements: 1
      UPDATE games SET played_at = played_at * 1000 WHERE played_at IS NOT NULL
                        AND played_at > 0 AND played_at < 100000000000
    independent check: PRAGMA data_version 2 -> 2 (no commit)
```

(Two independent measurements agree: the `sqlite3` trace callback attached to the connection the *application*
opens shows the `UPDATE`, and `PRAGMA data_version` read from a separate reader connection confirms that
zero rows needed changing in that particular run. My first attempt at [A] attached the trace hook to a
connection the *probe* opened and saw nothing — that was a measurement artifact, not a negative result; the
corrected probe wraps `get_db()` and is the one quoted above.)

The function's own docstring states the design intent plainly: *"Safe to call on every request: after the first
run the WHERE clause matches zero rows."* That is an acknowledgement that it is a per-request write which is a
no-op in steady state — while still paying a full implicit write transaction to find that out.

### 2. Consequence: the read route blocks on the database WRITE lock

```
[B] does it take the SQLite WRITE lock even with nothing to rewrite?
    GET /api/games/recent while another connection holds BEGIN IMMEDIATE:
      -> HTTP 200 after 2.64s  (it BLOCKED on the write lock)

[atk5_27] GET /api/games/recent during a 4 s foreign write lock: HTTP 200 after 3.64s
```

SQLite allows only one writer at a time. A pure `SELECT` cannot complete while any other connection holds a
write transaction, and this route now *is* a writer, so it joins the queue it used to only observe. The user
sees the game list stall for as long as the sync/analysis pipeline — which takes write transactions throughout
`POST /api/sync`, `/api/repertoire/sync_games` and every repertoire/study write — holds the lock. Per
`audit/STATE.md` (SYNC-03) the busy timeout is 30 s, so the realistic worst case is a **30-second hang, or a
`database is locked` 500** at the end of it. The availability window is exactly when the app is busiest.

```
[B2] do concurrent GET /api/games/recent calls serialise on that write?
    16 concurrent GETs: wall 0.09s, latency min 0.034s / median 0.070s / max 0.083s
    concurrency factor 11.4x (1.0x = fully serialised)
```

Reported honestly: at the current table size the per-request write is *fast enough* that 16 concurrent reads
still overlap (11.4x, not 1.0x). The stampede is **not** the problem here — the lock contention against
*other writers* in §2 is. I am not claiming a concurrency collapse.

### 3. The ordering it protects is already correct without it (A/B on a mixed-unit table)

```
  built 12 rows, half seconds-valued / half ms-valued

  naive raw ORDER BY played_at (what the write exists to fix):
    ['synUnitM0','synUnitM1','synUnitM2','synUnitM3','synUnitM4','synUnitM5'] ...
    -> seconds-valued rows ARE buried (all 6 ms rows first)

  --- WITH the per-request write (current behaviour) ---
    API order : ['synUnitS0','synUnitM0','synUnitS1','synUnitM1','synUnitS2','synUnitM2', ...]
    rows the write actually converted: 12/12 (the write ran: yes)

  --- WITHOUT the per-request write (write neutralised) ---
    API order : ['synUnitS0','synUnitM0','synUnitS1','synUnitM1','synUnitS2','synUnitM2', ...]

  --- verdict ---
    identical order with and without the write : True
```

The raw column *does* misorder the rows (all ms-valued first) — so the underlying problem CODE-02-06 diagnosed
is real. But **this route never reads the raw column**: the `ORDER BY` normalises inline
(`api.py:1113-1116`) and each row passes through `normalize_played_at()` (`api.py:1129`). Byte-identical
ordering with and without the write proves the per-request `UPDATE` is dead weight *on this route* — the
one-shot migration at `startup` (`api.py:726`) is the correct and sufficient place for it.

### 4. It is an unindexed full-table scan on the hot read path

```
[C] cost of the unindexed UPDATE as the games table grows
    {'rows':>8}  {'UPDATE ms':>10}  {'GET /recent ms':>15}
             12         0.3              5.1
           1000         0.4              5.7
           9012         1.1              8.4
          91000         8.8             27.8   <- every GET pays this
```

`played_at` has no index (see the CODE-02-08/CODE-02-10 "no `CREATE INDEX` anywhere" hand-off). At the
user's current 307 games this is irrelevant; it is a linear cost that grows without bound on a route the
frontend polls, and it is paid *in addition to* the query the user actually asked for.

### 5. The existing guard does not cover it

```
[D] grep games/recent tests/test_repertoire_write_safety.py
    -> (not covered -- the write-safety suite does not include this route)
```

The suite that exists to enforce "read endpoints perform no writes" omits this route, which is why the
violation is invisible to `unittest discover tests`.

## Root cause

`migrate_played_at_units()` is a **boot-time data migration** (correctly called from `startup`,
`api.py:726`) that has additionally been wired into a **request path** (`api.py:1084`) as a belt-and-braces
measure against writers this module does not own. The belt is unnecessary because the read path was already
made unit-safe twice over — in SQL at `api.py:1113-1116` and in Python at `api.py:1129` — and it is expensive
because an `UPDATE`, unlike a `SELECT`, must acquire SQLite's single write lock.

`api.py:726` is inside a `try/except` that logs and continues; `api.py:1084` is not guarded at all, so a
failure of the write path fails the whole read request.

## Exploit / escalation

How it was reached: `GET /api/games/recent` — the route `frontend/repertoire_builder.js` polls to populate the
game list. No special input is needed; the write fires on the plain, well-formed, unauthenticated request.

Adjacent weaker variants of the same root cause (all worth checking in the same pass):

1. **`migrate_played_at_units()` is reachable twice; check the other caller.** `api.py:726` (`startup`) is
   correct. If any *request* path also calls it, the same lock-acquisition applies — `grep` shows only those
   two, so today this route is the only offender, but the helper should be marked startup-only so it cannot be
   reintroduced per-request.
2. **The permanent mutation is a data-integrity concern, not just a lock concern.** In the mixed-unit test the
   `UPDATE` converted **12 of 12** rows — i.e. when seconds-valued rows *do* exist (and per the queued FIXER-B
   hand-off, `repertoire.py` still writes `played_at` in seconds on two paths), a plain **GET** silently and
   permanently rewrites the user's game rows. Read endpoints that can mutate data are exactly what
   `test_repertoire_write_safety.py` was written to prevent, and this is the counter-example.
3. **Un-guarded failure.** Because `api.py:1084` is not inside the `try/except` that `api.py:726` has, a
   failure of the write (lock timeout → `sqlite3.OperationalError: database is locked`) turns the game list into
   a **500**. Raising the read to also need the write lock therefore converts a would-be-200 into a 500 exactly
   under the contention it creates.
4. **Amplifier for the sync path.** Every `/api/recent` poll during a long `POST /api/sync` now contends for
   the same single write slot the sync needs, so the polling traffic slows the write it is waiting on — a
   feedback loop, not just added latency.
5. **Same shape elsewhere to check:** any other route that calls a `migrate_*`/`normalise_*`/`init_db()`
   helper per-request. `grep -rn "migrate_\|normalise_" backend/api.py` is the cheap sweep; today the answer
   is this one line plus the safe startup call.

## Suggested fix shape (for the `api.py` owner)

* Delete `api.py:1084`. Keep the migration in `startup` (`api.py:726`) only, and keep the two read-side
  normalisations (SQL `ORDER BY` at 1113-1116, `normalize_played_at()` at 1129) which are what actually make
  the route correct.
* If belt-and-braces is genuinely wanted, the correct place is a `SELECT`-only expression, not an `UPDATE`.
* Add `/api/games/recent` to `tests/test_repertoire_write_safety.py` so the class cannot return (the
  measurement recipe is in `atk5_26_write_on_read.py` §[A]: wrap `get_db()` and count `UPDATE`/`INSERT`/
  `DELETE`/`REPLACE` on the application's own connection).
* Separately: add a `CREATE INDEX` on `games(played_at)` (or make the `ORDER BY` expression index-usable) if
  the table is expected to grow past a few thousand rows — this belongs with the queued CODE-02-08/CODE-02-10
  index work rather than being duplicated here.
