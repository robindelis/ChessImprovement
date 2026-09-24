#!/usr/bin/env python3
"""
ATK5-09 repro: end-to-end HTTP 500 from concurrent /api/repertoire/explore.

The Lichess explorer response is STUBBED (no external network). The shared
Stockfish SimpleEngine of the RepertoireManager singleton is the only real
resource. 4 parallel requests -> python-chess cancels the in-flight analysis and
raises concurrent.futures.CancelledError, a **BaseException**, which the
`except Exception:` at backend/repertoire.py:322 does not catch -> FastAPI 500.

Idempotent, isolated temp DB.
"""
import os
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import

import requests  # noqa: E402
import concurrent.futures  # noqa: E402

print("CancelledError is subclass of Exception? ->",
      issubclass(concurrent.futures.CancelledError, Exception),
      "| BaseException?", issubclass(concurrent.futures.CancelledError, BaseException))

FAKE_EXPLORER = {
    "white": 1000, "draws": 500, "black": 1000, "total": 2500,
    "moves": [
        {"uci": "e2e4", "san": "e4", "white": 500, "draws": 250, "black": 250,
         "averageRating": 2200, "whiteWin": 100, "draws15": 80, "blackWin": 70},
        {"uci": "d2d4", "san": "d4", "white": 400, "draws": 200, "black": 200,
         "averageRating": 2150, "whiteWin": 90, "draws15": 70, "blackWin": 65},
        {"uci": "g1f3", "san": "Nf3", "white": 380, "draws": 190, "black": 190,
         "averageRating": 2100, "whiteWin": 80, "draws15": 60, "blackWin": 60},
        {"uci": "c2c4", "san": "c4", "white": 300, "draws": 150, "black": 150,
         "averageRating": 2050, "whiteWin": 70, "draws15": 55, "blackWin": 55},
    ],
}


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.text = "stub"

    def json(self):
        return self._p

    def iter_lines(self, decode_unicode=False):
        return iter(())


def fake_get(url, *a, **k):
    if "explorer" in url:
        return _Resp(FAKE_EXPLORER)
    return _Resp({})


requests.get = fake_get
requests.post = fake_get
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

from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import urllib.parse  # noqa: E402

FENS = [
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
    "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2",
    "r2q1rk1/pp1nbppp/2p1pn2/3p4/2PP4/2N1PN2/PPQ1BPPP/R1B2RK1 w - - 0 9",
    "r1bq1rk1/pp2ppbp/2np1np1/8/2BNP3/2N1B3/PPP2PPP/R2Q1RK1 w - - 0 10",
    "2rq1rk1/pp1bppbp/2np1np1/8/2BNP3/2N1B3/PPP2PPP/R2Q1RK1 w - - 0 10",
]
clients = [TestClient(app, raise_server_exceptions=False) for _ in range(6)]

print()
print("=" * 78)
print("A) SEQUENTIAL baseline (no contention)")
print("=" * 78)
# make sure every FEN is a cache MISS so the engine is actually used
import backend.db as _dbm  # noqa: E402
_c = _dbm.get_db()
_c.execute("DELETE FROM explorer_cache")
_c.commit()
_c.close()

for f in FENS[:2]:
    r = clients[0].get("/api/repertoire/explore?fen=" + urllib.parse.quote(f))
    evals = [m.get("eval") for m in r.json().get("moves", [])] if r.status_code == 200 else r.text[:60]
    print(f"  {r.status_code}  evals={evals}")

print()
print("=" * 78)
print(f"B) {len(FENS)} PARALLEL /api/repertoire/explore requests")
print("=" * 78)
out = [None] * len(FENS)
bar = threading.Barrier(len(FENS))


def go(i):
    c = clients[i % len(clients)]
    bar.wait()
    t0 = time.time()
    try:
        r = c.get("/api/repertoire/explore?fen=" + urllib.parse.quote(FENS[i]))
        out[i] = (r.status_code, round(time.time() - t0, 2), r.text[:100])
    except Exception as e:
        out[i] = ("EXC", round(time.time() - t0, 2), f"{type(e).__name__}: {str(e)[:80]}")


ts = [threading.Thread(target=go, args=(i,)) for i in range(len(FENS))]
[t.start() for t in ts]
[t.join() for t in ts]
for i, (s, dt, body) in enumerate(out):
    print(f"  req {i}: status={s} {dt}s  {body!r}")
print("  histogram:", Counter(s for s, _, _ in out))
fails = [o for o in out if o[0] != 200]
print(f"  >>> {len(fails)}/{len(FENS)} concurrent explorer requests FAILED "
      f"(shared SimpleEngine + no lock).")

print()
print("=" * 78)
print("C) REPEATED 2..4-way parallel bursts, 6 rounds, watching for 500s / lost evals")
print("=" * 78)
allfail = 0
nulleval = 0
total = 0
for trial in range(6):
    n = 2 + (trial % 3)
    _c = _dbm.get_db()
    _c.execute("DELETE FROM explorer_cache")
    _c.commit()
    _c.close()
    bar2 = threading.Barrier(n)
    res = [None] * n

    def go2(i, n=n, bar2=bar2, res=res, trial=trial):
        c = clients[i]
        bar2.wait()
        r = c.get("/api/repertoire/explore?fen=" + urllib.parse.quote(FENS[i]))
        if r.status_code == 200:
            evs = [m.get("eval") for m in r.json().get("moves", [])]
            res[i] = (r.status_code, evs)
        else:
            res[i] = (r.status_code, r.text[:80])

    ts = [threading.Thread(target=go2, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    for s, evs in res:
        total += 1
        if s != 200:
            allfail += 1
        elif isinstance(evs, list) and any(e is None for e in evs):
            nulleval += 1
    print(f"  trial {trial} (n={n}): {[(s, evs if s != 200 else evs) for s, evs in res]}")
print(f"  >>> non-200 responses: {allfail}/{total}; responses with missing evals: {nulleval}/{total}")

print()
print("DONE")
