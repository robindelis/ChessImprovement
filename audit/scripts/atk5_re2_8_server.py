"""Server shim for ATK-5 re2-8 (own process).

Slow fake engine checkout; sets the sync pool wait timeout from ATK5_WAIT.
Isolation asserted fail-closed before any backend import.  No real Stockfish.
"""
import os
import sys
import time

sys.path.insert(0, "/home/robin/Projects/ChessImprovement")
os.chdir("/home/robin/Projects/ChessImprovement")

PORT = int(sys.argv[1])
WORK = float(os.environ.get("ATK5_WORK", "3.0"))
WAIT = float(os.environ.get("ATK5_WAIT", "300"))

import tempfile  # noqa: E402

PROD = os.path.join(os.getcwd(), "chess_improvement.db")
_db = os.environ.get("CHESS_DB_PATH") or ""
assert _db, "CHESS_DB_PATH unset -- refusing to start"
assert os.path.abspath(_db) != os.path.abspath(PROD), "CHESS_DB_PATH == production!"
assert os.path.basename(_db).startswith("chess_test_"), f"not chess_test_*: {_db}"
assert os.path.abspath(_db).startswith(tempfile.gettempdir() + os.sep), "not in tempdir"

import backend.evaluator as ev  # noqa: E402

ev.SYNC_POOL_WAIT_TIMEOUT = WAIT


class FakeEngine:
    def analyse(self, *a, **k):
        time.sleep(WORK)
        class R:
            score = None
        return R()

    def configure(self, *a, **k):
        return None

    def ping(self):
        return None

    def quit(self):
        return None

    def close(self):
        return None


ev.SyncEnginePool._checkout = lambda self: FakeEngine()

import backend.lichess_client as lc  # noqa: E402


def explorer_stub(self, fen, play=""):
    # The explorer itself is fast; only the ENGINE is slow. This isolates the
    # pool wait from the outbound-call wait.
    return {"white": 100, "draws": 20, "black": 80,
            "moves": [{"san": "Nf6", "uci": "g8f6", "white": 60, "draws": 10,
                       "black": 40, "averageRating": 2200}],
            "total_games": 200}


lc.LichessClient.get_opening_explorer_stats = explorer_stub

from backend.api import app  # noqa: E402
import uvicorn  # noqa: E402

print(f"CHILD wait={WAIT} work={WORK}", file=sys.stderr, flush=True)
uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                              log_level="critical", access_log=False)).run()
