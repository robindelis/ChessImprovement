"""TRAINER probe 06 -- concurrency: concurrent /train/attempt on the same card.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/atk2_06_concurrency.py
"""
import sys, os, time, threading
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk2_fixture import *  # noqa

c = fresh()
add_line(c, "white", "Solo", ["e4", "e5", "Nf3"])
N = 24

hdr("A) %d CONCURRENT correct attempts on the SAME card (single user answer)" % N)
barrier = threading.Barrier(N)
results = []
lock = threading.Lock()




def run_correct(_):
    from fastapi.testclient import TestClient
    from backend.api import app
    tc = TestClient(app, raise_server_exceptions=False)
    barrier.wait()
    r = tc.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"})
    with lock:
        results.append((r.status_code, r.json().get("correct")))


t0 = time.time()
with ThreadPoolExecutor(max_workers=N) as ex:
    list(ex.map(run_correct, range(N)))
el = time.time() - t0
rowd = row("e2e4")
print("   %d requests, %.2fs, HTTP codes=%s correct=%s"
      % (N, el, sorted({r[0] for r in results}), sum(1 for r in results if r[1])))
print("   times_practiced=%s  times_correct=%s  interval_days=%s"
      % (rowd["times_practiced"], rowd["times_correct"], rowd["interval_days"]))
print("   LOST UPDATES: %d of %d accepted answers were thrown away (tp should be %d)"
      % (N - rowd["times_practiced"], N, N))
print("   every response claimed:",
      sorted({(r[1]) for r in results}))

hdr("B) %d CONCURRENT *wrong* attempts -> interval clobbering" % N)
c2 = fresh()
add_line(c2, "white", "Solo", ["e4"])
barrier2 = threading.Barrier(N)
res2 = []


def run_wrong(_):
    from fastapi.testclient import TestClient
    from backend.api import app
    tc = TestClient(app, raise_server_exceptions=False)
    barrier2.wait()
    r = tc.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": "d2d4"})
    with lock:
        res2.append((r.status_code, r.json().get("correct"), r.json().get("times_practiced")))


with ThreadPoolExecutor(max_workers=N) as ex:
    list(ex.map(run_wrong, range(N)))
rowd = row("e2e4")
print("   codes=%s  final times_practiced=%s (expected %d)" % (sorted({r[0] for r in res2}), rowd["times_practiced"], N))
print("   responses each reported times_practiced =", sorted({r[2] for r in res2}))

hdr("C) mixed correct/wrong concurrently, then read the DB")
c3 = fresh()
add_line(c3, "white", "Solo", ["e4", "e5"])
barrier3 = threading.Barrier(12)
res3 = []


def run_mixed(i):
    from fastapi.testclient import TestClient
    from backend.api import app
    tc = TestClient(app, raise_server_exceptions=False)
    barrier3.wait()
    uci = "e2e4" if i % 2 == 0 else "d2d4"
    r = tc.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": uci})
    with lock:
        res3.append(r.json().get("correct"))


with ThreadPoolExecutor(max_workers=12) as ex:
    list(ex.map(run_mixed, range(12)))
rowd = row("e2e4")
print("   correct answers accepted: %d / 6" % sum(1 for x in res3 if x))
print("   DB: times_practiced=%s times_correct=%s interval_days=%s next_review-now=%s"
      % (rowd["times_practiced"], rowd["times_correct"], rowd["interval_days"],
         rowd["next_review"] - int(time.time())))
print("   invariant tc<=tp holds:", rowd["times_correct"] <= rowd["times_practiced"])
