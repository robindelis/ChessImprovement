#!/usr/bin/env python3
"""ATK-5-25: does an upstream explorer FAILURE present to the user as "this
position has no games in the 2000+ database"?

`get_candidate_moves_2000` initialises `data = {"white":0,"draws":0,"black":0,
"moves":[]}` and only sets `req_success = True` on HTTP 200. On any failure
(total_games == 0, moves == []) the payload it returns is indistinguishable from
a genuine, successfully-queried empty position -- and it is not cached, so the
next request pays the full timeout again.

Isolated temp DB, idempotent.
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
import backend.repertoire as rep  # noqa: E402
from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")
rep.RepertoireManager._evaluate_candidate_moves = lambda self, b, m: None

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
COLD = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"

n = {"fail": 0, "ok": 0}


def mk(status, payload):
    def _get(url, params=None, headers=None, timeout=None, **kw):
        time.sleep(0.05)
        if status != 200:
            n["fail"] += 1

            class R:
                status_code = status

                @staticmethod
                def json():
                    return {}
            return R()
        n["ok"] += 1

        class R:
            status_code = 200

            @staticmethod
            def json():
                return payload
        return R()
    return _get


EMPTY_OK = {"white": 0, "draws": 0, "black": 0, "moves": []}
RICH = {
    "white": 400, "draws": 100, "black": 300,
    "moves": [{"san": "c5", "uci": "c7c5", "white": 300, "draws": 60, "black": 240,
               "averageRating": 2300}],
}

print("=" * 78)
print("ATK-5-25  upstream failure is reported as 'no games'")
print("=" * 78)

print("\n[A] HTTP 503 from the explorer, twice")
rep.requests.get = mk(503, None)
a1 = client.get("/api/repertoire/explore", params={"fen": COLD, "color": "white"}).json()
a2 = client.get("/api/repertoire/explore", params={"fen": COLD, "color": "white"}).json()
print(f"    upstream calls made : {n['fail']}  (2 requests, 2 calls -> nothing cached)")
print(f"    response 1          : {json.dumps(a1)[:300]}")
print(f"    response 2          : {json.dumps(a2)[:300]}")

print("\n[B] the SAME position with a healthy explorer returning a genuine 0-game reply")
rep.requests.get = mk(200, EMPTY_OK)
EMPTY_FEN = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"
b1 = client.get("/api/repertoire/explore", params={"fen": EMPTY_FEN, "color": "white"}).json()
print(f"    upstream calls made : {n['ok']}")
print(f"    response            : {json.dumps(b1)[:300]}")

print("\n    -> are the two responses the same shape?  (this is the wrong-result claim)")
keys_a = set(a1) if isinstance(a1, dict) else set()
keys_b = set(b1) if isinstance(b1, dict) else set()
print(f"       failure keys : {sorted(keys_a)}")
print(f"       empty    keys : {sorted(keys_b)}")
print(f"       identical key set: {keys_a == keys_b}")
for k in ("total_games", "moves", "error", "cached"):
    print(f"       {k:12} failure={a1.get(k)!r:12} genuine-empty={b1.get(k)!r}")

print("\n[C] a rich, healthy position, for contrast")
rep.requests.get = mk(200, RICH)
c1 = client.get("/api/repertoire/explore", params={"fen": COLD, "color": "white"}).json()
print(f"    response            : {json.dumps(c1)[:260]}")

print("\n[D] connection error (no HTTP status at all) -- the most common real failure")
calls = {"n": 0}


def boom(url, params=None, headers=None, timeout=None, **kw):
    calls["n"] += 1
    raise requests_ConnectionError("HTTPSConnectionPool(host='explorer.lichess.org', port=443): "
                                   "Max retries exceeded with url: /lichess?fen=...")


import requests as _rq
requests_ConnectionError = _rq.exceptions.ConnectionError
rep.requests.get = boom
d1 = client.get("/api/repertoire/explore", params={"fen": "4k3/8/8/8/8/8/8/4K3 b - - 0 1",
                                                  "color": "white"}).json()
print(f"    upstream attempts   : {calls['n']}")
print(f"    response            : {json.dumps(d1)[:300]}")
print(f"    -> the caller cannot tell this apart from 'the position has 0 master games'.")

print("\n[E] how long does the user wait, and is it retried every time?")
lat = {"n": 0}


def slow(url, params=None, headers=None, timeout=None, **kw):
    lat["n"] += 1
    time.sleep(1.0)
    raise _rq.exceptions.ReadTimeout("timed out")


rep.requests.get = slow
t0 = time.time()
client.get("/api/repertoire/explore",
           params={"fen": "4k3/8/8/8/8/8/8/4K3 b - - 0 1", "color": "white"})
one = time.time() - t0
t0 = time.time()
client.get("/api/repertoire/explore",
           params={"fen": "4k3/8/8/8/8/8/8/4K3 b - - 0 1", "color": "white"})
two = time.time() - t0
print(f"    1st request {one:.2f}s   2nd request {two:.2f}s   "
      f"(upstream attempts so far: {lat['n']})")
print("    -> the timeout is paid again on every single request; nothing is remembered.")
print("done.")
