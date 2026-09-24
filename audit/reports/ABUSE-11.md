# ABUSE-11 — The "isolated" test harness opens the production database READ-WRITE to copy it, breaking the `AGENTS.md` §9 zero-pollution invariant
- **Severity:** medium
- **Type:** security / data-loss
- **File:** `tests/test_setup.py`
- **Line(s):** 31-42 (`prod_db` discovery + `sqlite3.connect(prod_db)` + `src.backup(dst)`)
- **Component:** test infrastructure
- **Repro script:** `audit/scripts/atk5_testsetup_wal_repro.py` (proves the semantics on a synthetic WAL DB; the production file is **never** opened by the probe)
- **Detected by:** ATK-5

## Expected
`AGENTS.md` §9: *"Under NO circumstances should automated tests, adversarial
probes, or developer scripts modify or pollute this database!"* and *"Every
probe/test script MUST … point `CHESS_DB_PATH` at a throwaway temp DB"*. Copying
the production DB to bootstrap the temp DB is a reasonable idea, but it must be a
strictly **read-only** read. The read-only form in `sqlite3` is
`sqlite3.connect("file:<path>?mode=ro", uri=True)`.

## Actual
```python
    prod_db = getattr(db_mod, "DEFAULT_DB_PATH", os.path.join(PROJECT_ROOT, "chess_improvement.db"))
    if os.path.exists(prod_db):
        try:
            src = sqlite3.connect(prod_db)          # <-- READ-WRITE handle on production
            dst = sqlite3.connect(temp_path)
            src.backup(dst)
            src.close()
            dst.close()
        except Exception as e:
            print(f"Warning: Failed to copy production DB to test DB: {e}")
```

`sqlite3.connect(path)` is read-write by default. Proven on a synthetic WAL
database (the production file is never opened):

```
$ /home/robin/miniconda3/bin/python3 audit/scripts/atk5_testsetup_wal_repro.py
WAL mode: wal
writer holds the db. side files: {'atk5_wal2.db': True, 'atk5_wal2.db-wal': True, 'atk5_wal2.db-shm': True}
src.isolation_level:
can this handle WRITE? -> YES  (plain sqlite3.connect() is read-write; nothing stops a test from
      writing to, or checkpointing, the user's live database)
read-only handle rejects writes: attempt to write a readonly database

after last read-write close, -wal/-shm removed: True True  (a plain read of the user's DB therefore rewrites their main DB file)
main db mtime before last close: 1790439506.7854087
main db mtime after : 1790439506.7855144 changed: True
```

Two consequences:
* every `unittest discover tests` run (and every one of the ~15 `audit/scripts/*`
  probes) takes a **write-capable connection and a shared-cache lock** on the
  user's live, running database (PID 225248 holds the production DB open on
  port 38281);
* when that connection is the last one to close, SQLite **checkpoints the WAL into
  the main `.db` file and deletes `-wal`/`-shm`** — i.e. a "read" of the user's
  data physically rewrites `chess_improvement.db` (mtime changes, demonstrated).

Additionally, the temp DB is a **full copy of production data**, so the test suite
runs assertions against the user's real games/blunders: `atk5_route_fuzz.py`
observed `307` games and `153` blunders through `/api/games/recent` and
`/api/blunders` on the temp DB. Any test that asserted on absolute counts, or that
iterates every blunder, is asserting on live user data.

## Root cause
Convenience: the author wanted tests to run against realistic data, and reached for
`sqlite3.connect()` without specifying `mode=ro`. There is also **no fail-safe**:
`backend/db.py:17-22` re-reads `os.environ["CHESS_DB_PATH"]` on every connection, so
a script that forgets `import tests.test_setup` silently opens the production file
with full read-write access and no warning at all. The invariant is enforced purely
by convention (`AGENTS.md` §9.1) plus a docstring.

## Exploit / escalation
1. **Any future probe/test/probe-by-LLM that forgets the import writes to
   production.** `atk5_route_fuzz.py` hit 16 POST routes with hostile bodies and
   3 destructive endpoints; had `import tests.test_setup` been missing, every one
   of those would have landed in `chess_improvement.db` (which at the time was
   *also* a git-tracked file — see ABUSE-02, so it would have been committed).
2. **Adjacent — WAL corruption window.** A read-write connection to a WAL database
   that is concurrently being written by uvicorn participates in WAL recovery and
   `-shm` mapping; a probe killed mid-`backup()` (as happened twice during this
   audit, when I had to `kill -9` my own hung probes) can leave the production
   `-wal` in a state that requires recovery on the next server start.
3. **Adjacent — privacy.** `/tmp/chess_test_*.db` copies of the user's private
   archive are created with `tempfile.mkstemp` (mode 0600, good) but they are
   only removed by an `atexit` handler; a `SIGKILL`ed probe leaves a full copy of
   the user's games in `/tmp` indefinitely.

**Required fix:** open production with
`sqlite3.connect(f"file:{prod_db}?mode=ro", uri=True)` (and `immutable=1` if the
live server may be stopped), and add a hard guard in `backend/db.py` — e.g. refuse
to open `DEFAULT_DB_PATH` when `PYTEST_CURRENT_TEST`/`CHESS_DB_PATH`-absent **and**
an env var such as `CHESS_ALLOW_PROD_DB` is not set — so the convention becomes an
enforced invariant.
