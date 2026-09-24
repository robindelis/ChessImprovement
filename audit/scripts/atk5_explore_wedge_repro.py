#!/usr/bin/env python3
"""
ATK5-09b repro: the shared Stockfish engine of RepertoireManager is PERMANENTLY
wedged by a single concurrent pair of /api/repertoire/explore requests.

  mode=seq   : 6 sequential explore calls   -> evals present?
  mode=par   : 1 parallel pair, then 6 sequential explore calls -> evals present?

Lichess explorer responses are STUBBED. Temp DB only.
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import

MODE = (sys.argv[1] if len(sys.argv) > 1 else "seq").lower()

import requests  # noqa: E402

FAKE = {"white": 1000, "draws": 500, "black": 1000, "total": 2500,
        "moves": [{"uci": u, "san": s, "white": 400, "draws": 200, "black": 200,
                   "averageRating": 2100, "whiteWin": 80, "draws15": 60, "blackWin": 60}
                  for u, s in [("e2e4", "e4"), ("d2d4", "d4"), ("g1f3", "Nf3"),
                               ("c2c4", "c4"), ("b1c3", "Nc3"), ("f1c4", "Bc4"),
                               ("e1g1", "O-O"), ("b1c3", "Nc3")]]}


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

import urllib.parse  # noqa: E402
from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import backend.db as dbm  # noqa: E402

FENS = [
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
    "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2",
    "r2q1rk1/pp1nbppp/2p1pn2/3p4/2PP4/2N1PN2/PPQ1BPPP/R1B2RK1 w - - 0 9",
    "r1bq1rk1/pp2ppbp/2np1np1/8/2BNP3/2N1B3/PPP2PPP/R2Q1RK1 w - - 0 10",
    "2rq1rk1/pp1bppbp/2np1np1/8/2BNP3/2N1B3/PPP2PPP/R2Q1RK1 w - - 0 10",
]
c = TestClient(app, raise_server_exceptions=False)


def wipe():
    cn = dbm.get_db()
    cn.execute("DELETE FROM explorer_cache")
    cn.commit()
    cn.close()


def call(fen, client=None):
    client = client or c
    r = client.get("/api/repertoire/explore?fen=" + urllib.parse.quote(fen))
    if r.status_code != 200:
        return r.status_code, r.text[:80]
    return r.status_code, [m.get("eval") for m in r.json().get("moves", [])]


print(f"### MODE={MODE}")
wipe()
if MODE == "par":
    clients = [TestClient(app, raise_server_exceptions=False) for _ in range(2)]
    bar = threading.Barrier(2)
    res = [None, None]

    def go(i):
        bar.wait()
        res[i] = call(FENS[i], clients[i])

    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    print("  injected concurrent pair:", res)

print("  --- 6 SEQUENTIAL explore calls ---")
from backend.repertoire import RepertoireManager as _RM  # noqa: E402
ok = 0
for f in FENS:
    st, ev = call(f)
    n = sum(1 for e in ev if e is not None) if isinstance(ev, list) else 0
    tot = len(ev) if isinstance(ev, list) else 0
    ok += n
    _e = _RM._instance._engine
    alive = (_e is not None and _e.transport.get_returncode() is None) if _e else None
    print(f"   status={st} evals_present={n}/{tot}  engine_pid="
          f"{getattr(_e.transport, 'get_pid', lambda: '?')() if _e else None} alive={alive}"
          f"  {ev if not isinstance(ev, list) else ''}")
print(f"  ### MODE={MODE}: {ok} usable engine evaluations out of the sequential calls")
