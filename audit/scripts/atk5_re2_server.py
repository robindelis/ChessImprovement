"""Server shim for ATK-5 re2-1c.  Runs in its OWN process.

It does NOT import tests.test_setup -- it deliberately inherits the parent's
already-redirected CHESS_DB_PATH, exactly like a helper subprocess would.  The
isolation invariants are asserted BEFORE the app is imported, so a mis-set path
fails closed rather than touching anything.
"""
import os
import sys
import threading
import time

sys.path.insert(0, "/home/robin/Projects/ChessImprovement")
os.chdir("/home/robin/Projects/ChessImprovement")

PORT = int(sys.argv[1])
LAT = float(os.environ.get("ATK5_LAT", "2.0"))
CALLS_FILE = os.environ.get("ATK5_CALLS_FILE", "/tmp/opencode/atk5_re2_calls.txt")

# --- fail-closed isolation assertions, BEFORE any backend import -------------
import tempfile  # noqa: E402

PROD = os.path.join(os.getcwd(), "chess_improvement.db")
_db = os.environ.get("CHESS_DB_PATH") or ""
assert _db, "CHESS_DB_PATH unset -- refusing to start (would default to production)"
assert os.path.abspath(_db) != os.path.abspath(PROD), "CHESS_DB_PATH == production!"
assert os.path.basename(_db).startswith("chess_test_"), f"not a chess_test_* file: {_db}"
assert os.path.abspath(_db).startswith(tempfile.gettempdir() + os.sep), "not in tempdir"

import backend.lichess_client as lc  # noqa: E402

_lock = threading.Lock()
_count = 0


def stub(self, fen, play=""):
    global _count
    with _lock:
        _count += 1
        n = _count
    try:
        with open(CALLS_FILE, "w") as fh:
            fh.write(f"{n} {time.time()}\n")
    except OSError:
        pass
    time.sleep(LAT)
    return None


lc.LichessClient.get_opening_explorer_stats = stub

from backend.api import app  # noqa: E402
from backend import db as _dbm  # noqa: E402
import sys as _sys
print("CHILD resolved DB:", _dbm.get_db_path(), file=_sys.stderr, flush=True)
import logging  # noqa: E402
import uvicorn  # noqa: E402
logging.getLogger("backend.api").setLevel(logging.DEBUG)

uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                              log_level="critical", access_log=False)).run()
