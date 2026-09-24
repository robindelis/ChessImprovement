"""
Test environment setup: configures an isolated temporary SQLite database for
unit tests and seeds it with a **synthetic, self-contained** dataset.

Prevents any test, probe or benchmark from writing to — or reading from — the
user's real `chess_improvement.db`.

Audit history for this file
---------------------------
* **CODE-05-07 (MEDIUM-HIGH)**: it used to `sqlite3.connect(prod_db)` +
  `src.backup(dst)` and clone the user's whole database into the temp DB, and
  `tests/test_repertoire.py` then hard-coded `assertGreaterEqual(cnt, 280)`.
  The suite's verdict therefore depended on how many rated games `RobiDeli` had
  played, and a fresh clone without the DB was red out of the box. **Fixed:** the
  production DB is never opened. The schema comes from `backend.db.init_db()`
  (the one legitimate way to obtain it) and the rows come from
  `tests/fixtures.py`, which is pure literals.
* **CODE-05-10 (MEDIUM)**: the four `except Exception: pass` blocks here let a
  half-configured temp DB look like a healthy one, and several probe scripts in
  this directory fall back to `chess_improvement.db` when this module is absent.
  **Fixed:** every step is fail-closed — a failure raises, and
  `assert_isolated()` is verified by `tests/test_isolation_guard.py`.
"""
import atexit
import asyncio
import os
import sqlite3
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.db as db_mod  # noqa: E402  (must follow the sys.path insert)

from tests import fixtures  # noqa: E402

#: The production database. It is read for its *path* only — never opened.
PRODUCTION_DB_PATH = os.path.join(PROJECT_ROOT, "chess_improvement.db")

_TEMP_DB_PATH = None
_IS_TEST_ENV = False
_FIXTURE_SUMMARY = None


def assert_isolated():
    """Hard-fail unless the suite is provably pointed at a throwaway temp DB.

    Raises `RuntimeError` — it never falls back to the production database. A
    degraded isolation must be loud, not silent.
    """
    path = db_mod.get_db_path()
    if not path:
        raise RuntimeError(
            "test isolation FAILED: backend.db.get_db_path() is empty. "
            "Refusing to run; the production database is at %s" % PRODUCTION_DB_PATH
        )
    if os.path.abspath(path) == os.path.abspath(PRODUCTION_DB_PATH):
        raise RuntimeError(
            "test isolation FAILED: CHESS_DB_PATH points at the PRODUCTION "
            "database %s" % PRODUCTION_DB_PATH
        )
    if os.environ.get("CHESS_DB_PATH") != path:
        raise RuntimeError(
            "test isolation FAILED: CHESS_DB_PATH (%r) does not match "
            "backend.db.get_db_path() (%r)" % (os.environ.get("CHESS_DB_PATH"), path)
        )
    if not os.path.abspath(path).startswith(tempfile.gettempdir()):
        raise RuntimeError(
            "test isolation FAILED: %s is outside %s; a test DB must live in the "
            "system temp directory so it can never be a repo file" % (path, tempfile.gettempdir())
        )
    if not os.path.exists(path):
        raise RuntimeError("test isolation FAILED: temp database %s does not exist" % path)
    return path


def seed_synthetic_fixture(temp_path: str):
    """Seed the temp DB with `tests/fixtures.py`. Never touches production."""
    conn = sqlite3.connect(temp_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        summary = fixtures.seed_synthetic(conn)
        conn.commit()
    finally:
        conn.close()
    return summary


#: Environment variable holding the PID that *owns* the temp database.
#:
#: `CHESS_DB_PATH` alone is not enough to identify "our" database: it is
#: inherited by every child process. Without an owner check, a test that spawns a
#: helper subprocess (e.g. `tests/test_evaluator_stream.py`'s `HELPER`) makes the
#: child adopt the *parent's* temp DB via `_adopt_existing_temp_db()`, and the
#: child's `atexit` cleanup then unlinks the parent's database out from under it.
#: The next `get_db()` in the parent silently creates an empty file, so every
#: later module fails with `no such table: games`. Adoption and deletion are
#: therefore both gated on this PID.
OWNER_ENV_VAR = "CHESS_TEST_DB_OWNER_PID"


def _adopt_existing_temp_db():
    """Reuse a temp DB that *this process* already created, if there is one.

    `python -m unittest discover tests` imports `tests/test_setup.py` TWICE: once
    as the top-level module `test_setup` (it matches the `test*.py` discovery
    pattern) and once as `tests.test_setup` (because every test module does
    `import tests.test_setup`, which first imports the `tests` package). Those
    are two distinct module objects with two distinct `_TEMP_DB_PATH` globals.

    Without this guard the second import created a *second* temp database and
    overwrote `CHESS_DB_PATH`, so `backend.db` (which reads the env var) and
    `tests.test_setup` (which reads `_TEMP_DB_PATH`) pointed at different files.
    `reset_fixtures()` then seeded one database while the tests read the other —
    a silent, order-dependent split-brain that surfaced as "unexpected
    non-fixture games in the test DB".

    Adoption is keyed on the `chess_test_*.db` name prefix in the system temp
    directory AND on `OWNER_ENV_VAR` naming *this* PID, so:
      * a DB that is not ours (notably production) is rejected loudly, and
      * a child process that merely inherited `CHESS_DB_PATH` never adopts
        (and therefore never deletes) the parent's database.
    """
    if os.environ.get(OWNER_ENV_VAR) != str(os.getpid()):
        return None
    current = os.environ.get("CHESS_DB_PATH")
    if not current:
        return None
    abs_current = os.path.abspath(current)
    if not os.path.basename(abs_current).startswith("chess_test_"):
        return None
    if not abs_current.startswith(tempfile.gettempdir() + os.sep):
        return None
    if not os.path.exists(abs_current):
        return None
    return abs_current


def setup_test_db() -> str:
    """Point the whole process at a fresh temp DB seeded with synthetic data.

    Idempotent, and idempotent *across module identities* (see
    `_adopt_existing_temp_db`): a second import of this file reuses the database
    the first one created instead of silently making a new one.
    """
    global _TEMP_DB_PATH, _IS_TEST_ENV, _FIXTURE_SUMMARY
    if _IS_TEST_ENV and _TEMP_DB_PATH and os.path.exists(_TEMP_DB_PATH):
        return _TEMP_DB_PATH

    adopted = _adopt_existing_temp_db()
    if adopted is not None:
        _TEMP_DB_PATH = adopted
        _IS_TEST_ENV = True
        os.environ["CHESS_DB_PATH"] = adopted
        db_mod.set_db_path(adopted)
        try:
            assert_isolated()
        except Exception as exc:
            _abort(adopted, str(exc))
        # Re-seed: the adopting copy may be looking at a half-built database.
        _FIXTURE_SUMMARY = seed_synthetic_fixture(adopted)
        _reset_singletons()
        atexit.register(_make_cleanup(adopted))
        return adopted

    fd, temp_path = tempfile.mkstemp(prefix="chess_test_", suffix=".db")
    os.close(fd)
    os.unlink(temp_path)  # let SQLite create the file with the right permissions

    if os.path.abspath(temp_path) == os.path.abspath(PRODUCTION_DB_PATH):  # pragma: no cover
        raise RuntimeError("tempfile handed us the production DB path: %s" % temp_path)

    _TEMP_DB_PATH = temp_path
    _IS_TEST_ENV = True

    # Redirect BEFORE any backend module can open a connection. The production
    # database is intentionally never opened: schema comes from init_db(), data
    # comes from tests/fixtures.py.
    os.environ["CHESS_DB_PATH"] = temp_path
    os.environ[OWNER_ENV_VAR] = str(os.getpid())
    db_mod.set_db_path(temp_path)

    try:
        db_mod.init_db()
    except Exception as exc:
        _abort(temp_path, "init_db() failed on the temp database: %r" % (exc,))

    try:
        _FIXTURE_SUMMARY = seed_synthetic_fixture(temp_path)
    except Exception as exc:
        _abort(temp_path, "seeding the synthetic fixture failed: %r" % (exc,))

    # Reset singletons so they bind to the temporary test DB.
    try:
        _reset_singletons()
    except Exception as exc:
        _abort(temp_path, "could not reset backend singletons: %r" % (exc,))

    # Prove isolation is actually in place before any test runs.
    try:
        assert_isolated()
    except Exception as exc:
        _abort(temp_path, str(exc))

    atexit.register(_make_cleanup(temp_path))
    return temp_path


def _make_cleanup(temp_path):
    """Build the atexit handler that removes the temp DB and restores state.

    Deletion is gated on `OWNER_ENV_VAR`: a process that merely *inherited*
    `CHESS_DB_PATH` (a helper subprocess) must never unlink the database it
    inherited, or the parent loses its schema mid-run.
    """

    def cleanup():
        is_owner = os.environ.get(OWNER_ENV_VAR) == str(os.getpid())
        try:
            if is_owner:
                if os.environ.get("CHESS_DB_PATH") == temp_path:
                    del os.environ["CHESS_DB_PATH"]
                os.environ.pop(OWNER_ENV_VAR, None)
            db_mod.set_db_path(PRODUCTION_DB_PATH)
        finally:
            if not is_owner:
                return
            for suffix in ("", "-wal", "-shm"):
                f = temp_path + suffix
                if os.path.exists(f):
                    try:
                        os.unlink(f)
                    except OSError:
                        pass

    return cleanup


def _abort(temp_path, message):
    """Tear the temp DB down and raise. Never continues in a degraded state."""
    for suffix in ("", "-wal", "-shm"):
        f = temp_path + suffix
        if os.path.exists(f):
            try:
                os.unlink(f)
            except OSError:
                pass
    os.environ.pop("CHESS_DB_PATH", None)
    raise RuntimeError("test isolation ABORTED: " + message)


def get_fixture_summary():
    """Summary dict produced by the synthetic fixture (see `tests/fixtures.py`)."""
    if _FIXTURE_SUMMARY is None:
        setup_test_db()
    return dict(_FIXTURE_SUMMARY)


#: Tables the suite requires. `reset_fixtures()` re-creates the schema (via
#: `init_db()`, the only legitimate authority) if a module has mangled it, so a
#: destructive experiment in one module cannot cascade into every later one.
REQUIRED_TABLES = (
    "games", "blunders", "drill_reviews", "explorer_cache", "scouted_content",
    "repertoire_moves", "repertoire_studies", "study_moves", "settings",
)


def reset_fixtures():
    """Restore the temp DB to the documented synthetic baseline.

    Every test module calls this from `setUpClass`. Without it, modules that
    share the process also share the database, and a move added by
    `test_repertoire` silently turns a later module's "first import" into
    `already_present` — a green suite hiding order-dependent behaviour.

    It is also self-healing: if the env var was hijacked by another module, or a
    module dropped a table, the redirection and the schema are re-asserted here
    rather than leaving every later module to fail with `no such table`.

    Idempotent, and it re-asserts isolation afterwards.
    """
    path = temp_db_path()
    _reassert_redirection(path)
    _ensure_schema(path)
    summary = seed_synthetic_fixture(path)
    _reset_singletons()
    assert_isolated()
    return summary


def _reassert_redirection(path):
    """Make sure the whole process still points at OUR temp DB."""
    if os.environ.get("CHESS_DB_PATH") != path or \
            os.environ.get(OWNER_ENV_VAR) != str(os.getpid()):
        os.environ["CHESS_DB_PATH"] = path
        os.environ[OWNER_ENV_VAR] = str(os.getpid())
        db_mod.set_db_path(path)


def _ensure_schema(path):
    """Re-create the schema if any required table is missing. Fail-closed."""
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()
    missing = [t for t in REQUIRED_TABLES if t not in present]
    if not missing:
        return
    db_mod.set_db_path(path)
    try:
        db_mod.init_db()
    except Exception as exc:
        _abort(path, "re-creating the schema after %s went missing failed: %r"
               % (missing, exc))


#: Process-global third-party surfaces this suite stubs, and the **pristine** value each one
#: had when `tests/test_setup.py` was first imported. `backend/repertoire.py` and
#: `backend/lichess_client.py` both do `import requests`, so `rep_mod.requests` and
#: `lc_mod.requests` are the *same* module object: there is no per-backend copy to stub.
#:
#: AUDIT FIXER-W-01: a `mock.patch.object(rep_mod.requests, "get", ...)` that outlives its
#: test therefore does not only affect the explorer — it also answers the Lichess **account
#: probe**, the game NDJSON stream and the scout's HTTP calls for the rest of the process.
#: The concrete leak is fixed at its source (the ledger in
#: `tests/test_repertoire_sync_robustness.py::_SyncTestBase`); this table is the module-boundary
#: backstop that stops the *next* one from becoming an order-dependent suite.
_PROCESS_GLOBAL_SURFACES = {}

#: The attributes of the shared `requests` module that are stubbed somewhere in this suite.
_STUBBED_REQUESTS_ATTRS = ("get", "post", "put", "delete", "head", "patch", "request", "Session")


def _record_process_globals():
    """Snapshot the pristine value of each process-global surface. Import-time only, and
    idempotent across the two module identities `unittest discover` creates."""
    if _PROCESS_GLOBAL_SURFACES:
        return
    try:
        import requests
    except Exception:                                   # pragma: no cover
        return
    for attr in _STUBBED_REQUESTS_ATTRS:
        pristine = getattr(requests, attr, None)
        if pristine is not None:
            _PROCESS_GLOBAL_SURFACES[attr] = pristine


def _reset_process_globals():
    """Restore every process-global surface to the value recorded at import."""
    if not _PROCESS_GLOBAL_SURFACES:
        return
    try:
        import requests
    except Exception:                                   # pragma: no cover
        return
    for attr, pristine in list(_PROCESS_GLOBAL_SURFACES.items()):
        if getattr(requests, attr, None) is not pristine:
            setattr(requests, attr, pristine)


def _reset_singletons():
    """Drop process-wide singletons (and their in-process caches).

    Also resets the *engine pools*, which are process-wide module globals in
    `backend/evaluator.py`. A test that closes a pool (`pool.close()`,
    `close_all()`) otherwise leaves every later module with
    `EngineUnavailable("engine pool is closed")`, which surfaces much later as an
    unexplained HTTP 500 from `/api/sync`. Engines are quit before the
    reference is dropped so a module switch does not leak a ~300 MB process.

    AUDIT FIXER-W-01: it also restores the **process-global third-party surfaces** listed in
    `_PROCESS_GLOBAL_SURFACES`. `reset_fixtures()` is the module boundary, so it is the right
    place to close that door even though the concrete leak is fixed at its source.
    """
    from backend.repertoire import RepertoireManager
    instance = getattr(RepertoireManager, "_instance", None)
    if instance is not None:
        try:
            instance.close()
        except Exception:
            pass
    RepertoireManager._instance = None
    try:
        from backend.scout import ContentScout
        ContentScout._instance = None
    except Exception:
        pass

    _reset_process_globals()

    # --- engine pools (process-wide) -------------------------------------
    try:
        import backend.evaluator as evaluator_mod
    except Exception:
        return
    try:
        from backend.evaluator import AsyncEngineEvaluator, get_sync_engine_pool
    except Exception:
        return

    async_evaluator = getattr(AsyncEngineEvaluator, "_instance", None)
    if async_evaluator is not None:
        try:
            pool = async_evaluator.pool
        except Exception:
            pool = None
        if pool is not None:
            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(pool.close())
                finally:
                    loop.close()
            except Exception:
                pass
    AsyncEngineEvaluator._instance = None

    try:
        sync_pool = get_sync_engine_pool()
        sync_pool.close_all()
    except Exception:
        pass
    try:
        evaluator_mod._sync_pool = None
    except Exception:
        pass


def temp_db_path() -> str:
    """Absolute path of the temp DB the suite is running against."""
    if _TEMP_DB_PATH is None:
        setup_test_db()
    return _TEMP_DB_PATH


# Execute immediately on module import
_record_process_globals()
setup_test_db()
