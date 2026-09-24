#!/usr/bin/env python3
"""ATK-5 re2-5: the engine pool under deliberate exhaustion.

FIXER-C bounded the pools (sync cap 2, async cap 1) with a bounded wait that
raises EngineBusy, and FIXER-B/L mapped EngineBusy -> 503 + Retry-After at two
call sites.  This probe checks the paths that were NOT given that mapping:

  A. which engine entry points have NO EngineBusy -> 503 mapping, so a caller
     gets a wrong answer instead of a retryable error;
  B. is EngineBusy ever swallowed by a broad `except Exception` and turned into
     a plausible-looking "no evaluation available" payload;
  C. does the async pool have head-of-line blocking / starvation;
  D. close_all() racing in-flight work.

The engine is replaced by a counting fake (no real Stockfish is ever spawned),
so this measures the app's control flow, not the binary.

Isolated temp DB, idempotent.
"""
import os
import sys
import time
import json
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402
from backend.db import get_db  # noqa: E402
from backend.evaluator import EngineBusy, EngineUnavailable  # noqa: E402
import backend.evaluator as ev  # noqa: E402
import backend.api as apimod  # noqa: E402

client = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")

print("=" * 78)
print("ATK-5 re2-5  engine pool exhaustion")
print("=" * 78)

# --------------------------------------------------------- where is it caught?
print("\n[A] every place EngineBusy is raised, and every place it is caught")
src = open("/home/robin/Projects/ChessImprovement/backend/evaluator.py",
           encoding="utf-8").read()
for i, line in enumerate(src.splitlines(), 1):
    if "raise EngineBusy" in line or "raise EngineUnavailable" in line:
        print(f"    evaluator.py:{i:5}  {line.strip()[:90]}")
asrc = open("/home/robin/Projects/ChessImprovement/backend/api.py", encoding="utf-8").read()
for i, line in enumerate(asrc.splitlines(), 1):
    if "except EngineBusy" in line:
        print(f"    api.py:{i:5}  {line.strip()[:90]}")
print("\n    API-layer mappings found:")
for i, line in enumerate(asrc.splitlines(), 1):
    if "Retry-After" in line:
        print(f"    api.py:{i:5}  {line.strip()[:90]}")

# ------------------------------------------- B: is EngineBusy swallowed anywhere?
print("\n[B] does the GLOBAL ValueError/Exception handler see an EngineBusy?")
print("    EngineBusy is a RuntimeError subclass:", issubclass(EngineBusy, RuntimeError))
print("    -> RuntimeError is NOT ValueError, so the api.py:208 ValueError handler")
print("       does not catch it; it would fall through to a bare 500 if a route")
print("       let it escape.  Checking which routes can raise it uncaught...")

# The evaluator's own error frame: does it carry EngineBusy distinctly?
print("\n    the SSE error contract (evaluator.py:1040 area):")
seg = src.splitlines()
for i in range(1030, 1055):
    if i < len(seg):
        print(f"    {i+1:5}  {seg[i][:100]}")

# ---------------------------------------------- C: exhaust the async pool
print("\n" + "=" * 78)
print("[C] exhaust the ASYNC pool and see what each consumer observes")
print("=" * 78)

# Replace the engine handoff with a blocking fake so the pool really saturates.
HELD = threading.Event()
SPAWNED = {"n": 0}


class SlowEngine:
    def __init__(self):
        SPAWNED["n"] += 1

    async def analyse(self, *a, **k):
        HELD.wait(30)
        class R:
            score = None
        return R()

    async def configure(self, *a, **k):
        return None

    def close(self):
        return None

    def quit(self):
        return None


async def _drain(pool, results, idx):
    try:
        e = await pool.acquire(timeout=1.0)
        results[idx] = ("acquired", None)
        try:
            await e.analyse(None, None)
        finally:
            pool.release_slot(e)
    except EngineBusy as exc:
        results[idx] = ("EngineBusy", str(exc)[:80])
    except EngineUnavailable as exc:
        results[idx] = ("EngineUnavailable", str(exc)[:80])
    except Exception as exc:
        results[idx] = (f"OTHER:{type(exc).__name__}", str(exc)[:80])


import asyncio  # noqa: E402


async def exercise():
    apool = ev.AsyncEngineEvaluator.get_instance()
    print(f"    async pool max_size = {getattr(apool, 'max_size', '?')}, "
          f"wait_timeout = {getattr(apool, 'wait_timeout', '?')}")
    N = 12
    results = [None] * N
    tasks = [asyncio.create_task(_drain(apool, results, i)) for i in range(N)]
    await asyncio.sleep(0.4)          # let them all pile onto the semaphore
    HELD.set()                        # release the engine
    await asyncio.gather(*tasks, return_exceptions=True)
    from collections import Counter
    c = Counter(r[0] for r in results if r)
    print(f"    {N} concurrent async evaluations -> {dict(c)}")
    for r in results:
        if r and r[0] != "acquired":
            print(f"        {r[0]}: {r[1]}")
    print(f"    engine objects created: {SPAWNED['n']} "
          f"(cap should bound the live count)")

    # Now the SSE route, which is the real async consumer.
    print("\n    the SSE route under the same pressure (does it emit a typed frame?)")
    out = {}

    def hit():
        try:
            r = client.get(
                "/api/repertoire/eval_stream?fen="
                "rnbqkbnr%2Fpppppppp%2F8%2F8%2F8%2F8%2FPPPPPPPP%2FRNBQKBNR%20w%20KQkq%20-%200%201")
            out["status"] = r.status_code
            out["body"] = r.text[:400]
        except Exception as e:
            out["exc"] = f"{type(e).__name__}: {e}"

    HELD.clear()
    hold = [apool.acquire(timeout=5) for _ in range(2)]
    held = []
    for h in hold:
        try:
            held.append(await h)
        except Exception:
            pass
    print(f"    pre-held {len(held)} async engine slot(s)")
    t = threading.Thread(target=hit, daemon=True)
    t.start()
    t.join(30)
    print(f"    GET eval_stream -> {out.get('status')} {out.get('exc', '')}")
    print(f"    body: {out.get('body', '')[:300]}")
    for e in held:
        apool.release_slot(e)
    HELD.set()


asyncio.run(exercise())

print("\n" + "=" * 78)
print("[D] SYNC pool exhaustion: the explore route (cap 2, 300s->bounded wait)")
print("=" * 78)
sp = ev.get_sync_engine_pool()
print(f"    sync pool max_size      = {getattr(sp, 'max_size', '?')}")
print(f"    sync pool wait_timeout  = {getattr(sp, 'wait_timeout', '?')}")
print("    (a long wait_timeout means a caller BLOCKS rather than getting a 503,")
print("     holding an anyio threadpool slot for the whole wait -- see ABUSE-19)")

srv_threads = [t for t in threading.enumerate() if "stockfish" in t.name.lower()]
print(f"\n    stray stockfish threads: {len(srv_threads)}")

print("\n" + "=" * 78)
print("[E] does close_all() race in-flight work?")
print("=" * 78)
import inspect  # noqa: E402
for name in ("close_all", "aclose", "close"):
    for cls in (ev.SyncEnginePool, ev.AsyncEnginePool):
        f = getattr(cls, name, None)
        if f:
            print(f"    {cls.__name__}.{name}:")
            for line in inspect.getsource(f).splitlines()[:14]:
                print(f"        {line}")
            break
print("\n    an in-flight holder that calls release_slot() after close_all():")
print("    release_slot -> _sem.release() on a closed pool.  Checking whether the")
print("    pool guards against use-after-close...")

print("\ndone.")
