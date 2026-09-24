#!/usr/bin/env python3
"""ATK-5-24: thundering-herd / cache-stampede on the Lichess 2000+ Explorer.

`get_candidate_moves_2000` is read-cache -> fetch -> write-cache with no lock and
no in-flight coalescing, and it caches NOTHING on failure. N concurrent requests
for the same cold position therefore become N simultaneous outbound calls to a
shared third-party public API, and a failing call is re-fetched forever.

Measured against an isolated temp DB with the network replaced by a counting,
latency-simulating stub, so the numbers are the app's own behaviour.

Isolated temp DB, idempotent.
"""
import os
import sys
import json
import time
import threading
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
import backend.repertoire as rep  # noqa: E402
from backend.db import get_db  # noqa: E402

print("=" * 78)
print("ATK-5-24  explorer cache stampede / missing negative cache")
print("=" * 78)

CALLS = collections.Counter()
LOCK = threading.Lock()
LATENCY = float(os.environ.get("ATK5_LAT", "0.35"))


def fake_requests_get(url, params=None, headers=None, timeout=None, **kw):
    fen = (params or {}).get("fen", "")
    with LOCK:
        CALLS[fen] += 1
    time.sleep(LATENCY)                      # simulate a real outbound round trip
    return _Resp()


class _Resp:
    status_code = 200

    @staticmethod
    def json():
        return {
            "white": 400, "draws": 100, "black": 300,
            "moves": [
                {"san": "e4", "uci": "e2e4", "white": 200, "draws": 40, "black": 160, "averageRating": 2200},
                {"san": "d4", "uci": "d2d4", "white": 150, "draws": 40, "black": 110, "averageRating": 2100},
            ],
        }


rep.requests.get = fake_requests_get

# No engine: the eval of the top-8 moves is not what we are measuring.
class _NoEngine:
    def analyse(self, *a, **k):
        class _R:
            score = None
        return _R()

    def configure(self, *a, **k):
        return None

    def quit(self):
        return None


rep.RepertoireManager._evaluate_candidate_moves = lambda self, b, m: None

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
COLD = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"

rm = rep.RepertoireManager.get_instance()

print("\n[A] cold cache, 24 concurrent requests for the SAME position")
CALLS.clear()
with LOCK:
    pass
# make sure it is cold
c = get_db()
c.execute("DELETE FROM explorer_cache")
c.commit()
c.close()

CALLS.clear()
N = 24
results = []
barrier = threading.Barrier(N)


def worker():
    barrier.wait()
    try:
        rm.get_candidate_moves_2000(COLD, color="white")
    except Exception as e:
        results.append(f"EXC {type(e).__name__}")


t0 = time.time()
ts = [threading.Thread(target=worker) for _ in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join(60)
dt = time.time() - t0
total = sum(CALLS.values())
print(f"    {N} concurrent requests, one cold position")
print(f"    outbound explorer calls actually made : {total}")
print(f"    wall clock                            : {dt:.2f} s "
      f"(one call costs ~{LATENCY:.2f}s; a coalesced fetch would be ~{LATENCY:.2f}s)")
print(f"    amplification                         : {total}x")
print(f"    per-position call counts              : {dict(CALLS)}")
if results:
    print(f"    errors                                : {results[:3]}")

print("\n[B] the same 24 requests AFTER the cache is warm (the control)")
CALLS.clear()
ts = [threading.Thread(target=worker) for _ in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join(60)
print(f"    outbound calls with a warm cache      : {sum(CALLS.values())}  "
      f"(should be 0)")

print("\n[C] does a FAILED fetch get cached? (negative caching)")
# A *legal* position (both kings present) so we get past the FEN guard.
FAIL = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"


def failing_get(url, params=None, headers=None, timeout=None, **kw):
    with LOCK:
        CALLS["fail:" + str((params or {}).get("fen", ""))] += 1
    time.sleep(0.05)

    class R:
        status_code = 503

        @staticmethod
        def json():
            return {}
    return R()


rep.requests.get = failing_get
CALLS.clear()
rm.get_candidate_moves_2000(FAIL, color="white")
r1 = rm.get_candidate_moves_2000(FAIL, color="white")
rm.get_candidate_moves_2000(FAIL, color="white")
nfail = sum(v for k, v in CALLS.items() if k.startswith("fail:"))
print(f"    3 sequential calls on a FAILING position -> {nfail} outbound calls "
      f"(a negative cache would make this 1)")
print(f"    payload returned to the caller          : "
      f"total_games={r1.get('total_games')} moves={len(r1.get('moves', []))}")

c = get_db()
row = c.execute("SELECT COUNT(*) FROM explorer_cache WHERE fen = ?",
                (" ".join(FAIL.split()[:4]),)).fetchone()
print(f"    rows cached for the failing position    : {row[0]}  (0 = never remembered)")
c.close()

print("\n[C2] same on a *reachable but empty* position (total_games == 0)")
EMPTY = "4k3/8/8/8/8/8/8/4K3 b - - 0 1"


def empty_get(url, params=None, headers=None, timeout=None, **kw):
    with LOCK:
        CALLS["empty:" + str((params or {}).get("fen", ""))] += 1
    time.sleep(0.05)

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"white": 0, "draws": 0, "black": 0, "moves": []}
    return R()


rep.requests.get = empty_get
CALLS.clear()
rm.get_candidate_moves_2000(EMPTY, color="black")
rm.get_candidate_moves_2000(EMPTY, color="black")
nempty = sum(v for k, v in CALLS.items() if k.startswith("empty:"))
print(f"    2 sequential calls on a 0-game position -> {nempty} outbound calls")
c = get_db()
row = c.execute("SELECT COUNT(*) FROM explorer_cache WHERE fen = ?",
                (" ".join(EMPTY.split()[:4]),)).fetchone()
print(f"    rows cached                              : {row[0]}")
c.close()

print("\n[D] how many DISTINCT positions can one /api/repertoire/tree fan out to?")
src = open("backend/repertoire.py", encoding="utf-8").read()
import re
for m in re.finditer(r"max_explorer_lookups\s*=\s*([A-Za-z0-9_]+)", src):
    print(f"    max_explorer_lookups symbol found: {m.group(1)}")
for m in re.finditer(r"(\w+)\s*=\s*min\((\w+),\s*(\d+)\)", src):
    if "lookup" in m.group(1).lower() or "explorer" in m.group(1).lower():
        print(f"    clamp: {m.group(0)}")
print("    (see also: ABUSE/CODE-6 hand-off in audit/STATE.md -- get_game_gaps()"
      "\n     burning up to 40 live explorer calls per /api/repertoire/tree)")

print("\n[E] can the fan-out be driven remotely? what does the explore route expose?")
print("    GET /api/repertoire/explore?fen=<any legal FEN>  -> one uncached call per")
print("    *distinct* FEN, and there is no per-caller or per-process outbound budget,")
print("    so an unauthenticated (loopback-origin) client can walk arbitrary FENs.")
print("done.")
