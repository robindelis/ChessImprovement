"""
`tests/probe_guard.py` — fail-closed helpers for the standalone audit probe
scripts that live in `tests/`.

Audit finding CODE-05-10 (MEDIUM): nine prior-audit probe scripts sat inside
`tests/`, were not collected by `unittest discover` (their names do not match
`test*.py`), and **failed open to the production database**:

```python
DB_PATH = os.environ.get("CHESS_DB_PATH", "chess_improvement.db")   # <- the bug
...
conn.execute("DELETE FROM repertoire_moves WHERE repertoire = 'black'")
```

That default inverts the safety direction: the *safe* value should be the
fallback, not the override. `cd /tmp && python .../tests/attack_fixes_abuse.py`
therefore either creates a stray database in `$PWD` or, from the repo root,
opens and mutates `chess_improvement.db` — user `RobiDeli`'s real repertoire.

This module provides the fail-closed replacement:

* `require_isolated_db()` — returns the temp DB path or **raises**. It never
  falls back to any default, and it rejects a path that is not a
  `chess_test_*.db` inside the system temp directory (which is what production
  is not).
* `require_no_live_http()` — the base URL a probe may use. Probes must go
  through `TestClient(app)`, never `http://127.0.0.1:38281`.
* `assert_production_db_untouched(snapshot)` — optional helper used by
  `tests/test_isolation_guard.py`.

Every script in this directory must do this, in this order:

```python
import tests.test_setup          # FIRST backend-adjacent import
from tests.probe_guard import require_isolated_db
DB_PATH = require_isolated_db()
```

`tests/test_isolation_guard.py` enforces that statically and dynamically.
"""
import hashlib
import os
import sqlite3
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

#: The production database. Referenced for its path only — never opened.
PRODUCTION_DB_PATH = os.path.join(PROJECT_ROOT, "chess_improvement.db")

#: The port the live, production-backed server listens on. Probes must never
#: talk to it (audit/BRIEF.md hard rule 3).
LIVE_SERVER_URL = "http://127.0.0.1:38281"


class ProductionDataRisk(RuntimeError):
    """Raised when a probe is about to run without a provably isolated DB."""


def require_isolated_db() -> str:
    """Return the isolated temp DB path, or raise `ProductionDataRisk`.

    There is deliberately **no default**. `os.environ.get("CHESS_DB_PATH", ...)`
    is the exact pattern that produced CODE-05-10.
    """
    path = os.environ.get("CHESS_DB_PATH")
    if not path:
        raise ProductionDataRisk(
            "CHESS_DB_PATH is not set. Import `tests.test_setup` BEFORE any "
            "backend module (and before opening sqlite3) so the process is "
            "pointed at a throwaway database. Refusing to guess: the default "
            "would be the production database at %s." % PRODUCTION_DB_PATH
        )
    abs_path = os.path.abspath(path)
    if abs_path == os.path.abspath(PRODUCTION_DB_PATH):
        raise ProductionDataRisk(
            "CHESS_DB_PATH points at the PRODUCTION database %s. Refusing to "
            "run." % PRODUCTION_DB_PATH
        )
    if not os.path.basename(abs_path).startswith("chess_test_"):
        raise ProductionDataRisk(
            "CHESS_DB_PATH=%s is not a `chess_test_*.db` throwaway file. Probes "
            "must use the database that `tests.test_setup` creates." % abs_path
        )
    if not abs_path.startswith(tempfile.gettempdir() + os.sep):
        raise ProductionDataRisk(
            "CHESS_DB_PATH=%s is outside the system temp directory %s; a test "
            "database must never be a repository file."
            % (abs_path, tempfile.gettempdir())
        )
    return abs_path


def require_no_live_http(url) -> str:
    """Reject any URL aimed at the live, production-backed server."""
    text = str(url)
    if LIVE_SERVER_URL in text or "127.0.0.1:38281" in text or "localhost:38281" in text:
        raise ProductionDataRisk(
            "Refusing to build a request for the live server %s: it owns the "
            "production database. Use TestClient(app) instead." % LIVE_SERVER_URL
        )
    return text


def assert_writable_path_is_not_production(path) -> None:
    """Raise if `path` is (or resolves to) the production database."""
    if os.path.abspath(str(path)) == os.path.abspath(PRODUCTION_DB_PATH):
        raise ProductionDataRisk(
            "refusing to write to the production database %s" % PRODUCTION_DB_PATH
        )


def open_production_readonly():
    """Open the production database **read-only and immutable**. Fail-closed.

    `mode=ro` refuses any write at the SQLite level; `immutable=1` additionally
    tells SQLite the file cannot change, so it never takes a lock, never reads a
    `-wal`/`-shm` sidecar and — critically — never *checkpoints* on close. That
    matters because the old `tests/test_setup.py` used `src.backup(dst)` against
    this very file, and a checkpointing connection can modify a database that is
    only supposed to be read.

    Prefer `fingerprint()` (a plain `os.stat` + `sha256` read) where a mere
    existence/mtime check is enough; this is for the rare case a test genuinely
    needs to read production rows to prove it is not the fixture source.
    """
    path = PRODUCTION_DB_PATH
    if not os.path.exists(path):
        raise ProductionDataRisk("production database %s does not exist" % path)
    uri = "file:%s?mode=ro&immutable=1" % path.replace("?", "%3f").replace("#", "%23")
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def fingerprint(path=PRODUCTION_DB_PATH):
    """`(mtime_ns, size, sha256)` of a database file, for before/after proofs.

    Reads the bytes with plain `open(..., 'rb')` — no SQLite connection, so it
    cannot checkpoint or lock the file.
    """
    if not os.path.exists(path):
        return None
    stat = os.stat(path)
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    return (stat.st_mtime_ns, stat.st_size, digest)


def open_isolated_db(read_only: bool = False) -> sqlite3.Connection:
    """Open a raw sqlite3 connection to the isolated temp DB. Fail-closed."""
    path = require_isolated_db()
    if read_only:
        uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")
        return sqlite3.connect(uri, uri=True, timeout=30.0)
    assert_writable_path_is_not_production(path)
    return sqlite3.connect(path, timeout=30.0)
