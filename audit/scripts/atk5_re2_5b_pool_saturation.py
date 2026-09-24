#!/usr/bin/env python3
"""ATK-5 re2-5b: the SYNC pool's bounded wait -- is it actually bounded?

FIXER-C gave `SyncEnginePool.acquire()` a bounded wait that raises `EngineBusy`
instead of queueing forever.  `SYNC_POOL_WAIT_TIMEOUT` is the number that decides
whether a saturated pool produces a fast, retryable 503 or a request that hangs
for minutes while holding an anyio threadpool slot.

This measures, with NO real Stockfish (the checkout is replaced by a blocking
fake, so the semaphore is genuinely held):
  A. the configured wait_timeout and pool cap;
  B. how long a caller actually waits when the pool is saturated, and what it
     gets back;
  C. whether N concurrent callers are all serialised behind the cap (a
     head-of-line / thundering-herd measurement);
  D. whether the wait is bounded at all, or effectively unbounded;
  E. what an async route sharing the anyio threadpool experiences meanwhile.

Isolated temp DB, idempotent, no real engine process.
"""
import os
import sys
import time
import threading
import statistics

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402
from backend.evaluator import (  # noqa: E402
    EngineBusy, EngineUnavailable, SyncEnginePool,
    SYNC_POOL_MAX_SIZE, SYNC_POOL_WAIT_TIMEOUT, ASYNC_POOL_MAX_SIZE,
    ASYNC_POOL_WAIT_TIMEOUT,
)
import backend.evaluator as ev  # noqa: E402

client = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")

print("=" * 78)
print("ATK-5 re2-5b  sync pool bounded wait")
print("=" * 78)
print(f"    SYNC_POOL_MAX_SIZE        = {SYNC_POOL_MAX_SIZE}")
print(f"    SYNC_POOL_WAIT_TIMEOUT    = {SYNC_POOL_WAIT_TIMEOUT} s")
print("    env vars: CHESS_ENGINE_MAX_PROCS / CHESS_ENGINE_WAIT_TIMEOUT /")
print("               CHESS_ENGINE_ASYNC_MAX / CHESS_ENGINE_ASYNC_WAIT_TIMEOUT")
print(f"    ASYNC_POOL_MAX_SIZE       = {ASYNC_POOL_MAX_SIZE}")
print(f"    ASYNC_POOL_WAIT_TIMEOUT   = {ASYNC_POOL_WAIT_TIMEOUT} s")

# env overrides the caps?
print("\n    env override honoured?")
os.environ["CHESS_ENGINE_MAX_PROCS"] = "7"
os.environ["CHESS_ENGINE_ASYNC_MAX"] = "9"
p = SyncEnginePool(max_size=None, wait_timeout=0.5)
print(f"      with CHESS_ENGINE_MAX_PROCS=7 -> max_size={p.max_size}")
del os.environ["CHESS_ENGINE_MAX_PROCS"]
p2 = SyncEnginePool(max_size=None, wait_timeout=0.5)
print(f"      without it                  -> max_size={p2.max_size}")
os.environ.pop("CHESS_ENGINE_ASYNC_MAX", None)

# ------------------------------------------------- a blocking fake "engine"
HOLD = threading.Event()
SPAWN = {"n": 0}


class FakeEngine:
    def __init__(self):
        SPAWN["n"] += 1

    def analyse(self, *a, **k):
        HOLD.wait(120)
        class R:
            score = None
        return R()

    def configure(self, *a, **k):
        return None

    def quit(self):
        return None

    def close(self):
        return None


print("\n" + "=" * 78)
print("[B/C] N concurrent acquire() calls against a cap-2 pool")
print("=" * 78)
CAP = 2
WAIT = 3.0          # the probe's own bounded wait, so the probe terminates
pool = SyncEnginePool(max_size=CAP, wait_timeout=WAIT, idle_seconds=999)
pool._checkout = lambda: FakeEngine()      # no real process
N = 10
outcomes = []
lat = []
bar = threading.Barrier(N)


def worker(i):
    bar.wait()
    t0 = time.time()
    try:
        e = pool.acquire()
        outcomes.append(("acquired", None))
        try:
            HOLD.wait(120)
        finally:
            pool.release(e)
    except EngineBusy as exc:
        outcomes.append(("EngineBusy", str(exc)[:70]))
    except Exception as exc:
        outcomes.append((f"OTHER:{type(exc).__name__}", str(exc)[:70]))
    lat.append(time.time() - t0)


SPAWN["n"] = 0
ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
t0 = time.time()
for t in ts:
    t.start()
time.sleep(0.5)
print(f"    cap={CAP}, {N} concurrent callers, wait_timeout={WAIT}s")
print(f"    in_use right now          : {pool.in_use}  live: {pool.live}")
time.sleep(0.6)
HOLD.set()                        # let the holders finish -> the rest proceed
for t in ts:
    t.join(180)
wall = time.time() - t0

from collections import Counter  # noqa: E402
c = Counter(o[0] for o in outcomes)
print(f"    outcomes                  : {dict(c)}")
print(f"    engine objects created    : {SPAWN['n']} (cap {CAP})")
lat.sort()
print(f"    wait min/med/max          : {lat[0]:.2f} / {statistics.median(lat):.2f} / {lat[-1]:.2f}s")
print(f"    total wall                : {wall:.2f}s for {N} callers")
for o in outcomes:
    if o[0] != "acquired":
        print(f"        {o[0]}: {o[1]}")

print("\n" + "=" * 78)
print("[D] is the wait REALLY bounded?  (measure a caller that arrives to a full pool)")
print("=" * 78)
HOLD.clear()
held = [pool.acquire() for _ in range(CAP)]
print(f"    pool now holds {pool.in_use}/{CAP}; a new caller must wait wait_timeout")
t0 = time.time()
try:
    e = pool.acquire()
    print(f"    UNEXPECTED: acquired after {time.time() - t0:.2f}s")
    pool.release(e)
except EngineBusy as exc:
    print(f"    EngineBusy after {time.time() - t0:.2f}s "
          f"(wait_timeout={WAIT}s): {str(exc)[:80]}")
except Exception as exc:
    print(f"    {type(exc).__name__} after {time.time() - t0:.2f}s: {str(exc)[:80]}")

print("\n    and the SHIPPED default (this is the number that matters):")
print(f"      SYNC_POOL_WAIT_TIMEOUT = {SYNC_POOL_WAIT_TIMEOUT}s, cap = {SYNC_POOL_MAX_SIZE}")
print(f"      -> a saturated pool makes a caller block for up to "
      f"{SYNC_POOL_WAIT_TIMEOUT}s per acquire(),")
print(f"         inside a `def` route, i.e. holding an anyio worker thread the whole time.")

for e in held:
    pool.release(e)
HOLD.set()

print("\n" + "=" * 78)
print("[E] what that does to OTHER routes sharing the anyio threadpool")
print("=" * 78)
print("    anyio's default thread limiter is 40 tokens.  Every `def` route in this")
print("    app (all of /api/blunders, /api/dashboard, /api/games/recent,")
print("    /api/repertoire/tree, ...) runs in that pool.")
import anyio.to_thread  # noqa: E402
import asyncio  # noqa: E402


async def _lim():
    return anyio.to_thread.current_default_thread_limiter().total_tokens


print(f"    anyio default thread limiter total_tokens = {asyncio.run(_lim())}")

# Measure: hold the pool, then saturate N sync routes, and time a cheap one.
print("\n    real measurement over the API: hold the engine pool, then flood a")
print("    sync route that acquires it, and time an unrelated cheap sync route.")
import backend.repertoire as rep  # noqa: E402

orig_acq = None
state = {"hold": True}


class Gate:
    """A pool stand-in that blocks the way a saturated engine pool does."""

    def __init__(self, inner):
        self.inner = inner

    def acquire(self, *a, **k):
        time.sleep(2.0)          # the bounded wait, in miniature
        raise EngineBusy("engine pool saturated (probe)")

    def release_slot(self, *a, **k):
        return None

    def close_all(self):
        return None


REAL_POOL = ev.get_sync_engine_pool()
ev._sync_pool = Gate(REAL_POOL)
try:
    N2 = 40
    res = {}

    def flood(i):
        t0 = time.time()
        r = client.get("/api/repertoire/explore?fen=rnbqkbnr%2Fpppppppp%2F8%2F8%2F8%2F8%2FPPPPPPPP%2FRNBQKBNR%20w%20KQkq%20-%200%201")
        res[i] = (time.time() - t0, r.status_code)

    ts2 = [threading.Thread(target=flood, args=(i,)) for i in range(N2)]
    for t in ts2:
        t.start()
    time.sleep(0.3)
    t0 = time.time()
    cheap = client.get("/api/dashboard")
    cheap_dt = time.time() - t0
    for t in ts2:
        t.join(120)
    st = sorted({v[1] for v in res.values()})
    print(f"    {N2} concurrent /api/repertoire/explore (each 'waits' 2s then 503-ish)")
    print(f"    statuses                   : {st}")
    print(f"    /api/dashboard during it   : {cheap_dt:.2f}s HTTP {cheap.status_code}")
    lats = sorted(v[0] for v in res.values())
    print(f"    explore latency max        : {lats[-1]:.2f}s")
finally:
    ev._sync_pool = REAL_POOL

print("\ndone.")
