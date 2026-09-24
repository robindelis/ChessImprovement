#!/usr/bin/env python3
"""Debug 3: engine liveness across TestClient requests."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import tests.test_setup  # noqa: E402
import requests  # noqa: E402

FAKE = {"white": 1000, "draws": 500, "black": 1000, "total": 2500,
        "moves": [{"uci": u, "san": s, "white": 400, "draws": 200, "black": 200,
                   "averageRating": 2100, "whiteWin": 80, "draws15": 60, "blackWin": 60}
                  for u, s in [("e2e4", "e4"), ("d2d4", "d4"), ("g1f3", "Nf3"),
                               ("c2c4", "c4"), ("b1c3", "Nc3")]]}


class _R:
    status_code = 200
    text = "stub"

    def json(self):
        return FAKE

    def iter_lines(self, decode_unicode=False):
        return iter(())


requests.get = lambda *a, **k: _R()
requests.post = lambda *a, **k: _R()
import yt_dlp  # noqa: E402


class _Y:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, *a, **k):
        return None


yt_dlp.YoutubeDL = _Y

import urllib.parse, subprocess  # noqa: E402
from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.repertoire import RepertoireManager as RM  # noqa: E402

FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
c = TestClient(app, raise_server_exceptions=False)
for i in range(3):
    import backend.db as dbm
    cn = dbm.get_db(); cn.execute("DELETE FROM explorer_cache"); cn.commit(); cn.close()
    r = c.get("/api/repertoire/explore?fen=" + urllib.parse.quote(FEN))
    e = RM._instance._engine
    pid = e.transport.get_pid()
    rc = e.transport.get_returncode()
    evs = [m.get("eval") for m in r.json().get("moves", [])] if r.status_code == 200 else r.text[:60]
    ps = subprocess.run(["ps", "-o", "pid,stat,etime,rss,comm", "-p", str(pid)],
                        capture_output=True, text=True).stdout.strip()
    print(f"req {i}: status={r.status_code} evals={evs}")
    print(f"        engine pid={pid} returncode={rc}  ps: {ps}")
    print(f"        n stockfish procs: {subprocess.run(['pgrep','-fc','stockfish'],capture_output=True,text=True).stdout.strip()}")
