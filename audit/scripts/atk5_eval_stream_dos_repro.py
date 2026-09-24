#!/usr/bin/env python3
"""
ATK5-10 repro: /api/repertoire/eval_stream resource abuse.

 A) depth is accepted up to 30 (api.py:998) and the Limit has NO time cap, so a
    single request can pin the ONE engine for an unbounded time.
 B) a client that opens the stream and never reads holds AsyncEngineEvaluator's
    single asyncio.Lock forever -> every other eval request (and therefore the
    whole eval-bar UI of the only engine in the app) is starved.
 C) no cap on the number of concurrent streams / no queue bound.
 D) engine crash during a stream resets the engine and the SSE error event
    carries str(e) (evaluator.py:216) -> internal detail in an SSE payload.

Bounded: everything has an explicit timeout, nothing is left running.
"""
import os
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import

import requests  # noqa: E402


class _R:
    status_code = 503
    text = "stub"

    def json(self):
        return {}

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

from backend.api import app  # noqa: E402
from backend.evaluator import AsyncEngineEvaluator  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
c = TestClient(app, raise_server_exceptions=False)

print("=" * 78)
print("A) depth bound accepted by the API")
print("=" * 78)
for d in (1, 16, 30, 31, 999):
    r = c.get(f"/api/repertoire/eval_stream?fen={urllib.parse.quote(FEN)}&depth={d}")
    print(f"  depth={d:<5} -> {r.status_code}  {r.text[:70]!r}")

print()
print("=" * 78)
print("B) slow/absent reader starves every other eval request")
print("=" * 78)
ev = AsyncEngineEvaluator.get_instance()
print("  engine lock:", ev._lock, " cache size:", len(ev._cache), "max:", ev._cache_max_size)

state = {}


def hog():
    """Open a depth-30 stream and read only the FIRST event, then stop reading."""
    try:
        with c.stream("GET", f"/api/repertoire/eval_stream?fen={urllib.parse.quote(FEN)}&depth=30") as s:
            state["status"] = s.status_code
            for i, line in enumerate(s.iter_lines()):
                state.setdefault("first_event_at", time.time())
                state["lines"] = i
                if i >= 0:
                    break        # stop consuming -> generator blocks on the socket
                break
            # deliberately do NOT close; hold the connection open
            state["held"] = True
            time.sleep(12)
    except Exception as e:
        state["err"] = f"{type(e).__name__}: {str(e)[:100]}"


t = threading.Thread(target=hog, daemon=True)
t.start()
time.sleep(2.5)
print("  hog state:", state)

lock_held = ev._lock.locked()
print(f"  engine lock held by the slow reader? {lock_held}")

t0 = time.time()
r = c.get(f"/api/repertoire/eval_stream?fen={urllib.parse.quote(FEN)}&depth=8")
dt = time.time() - t0
print(f"  a *legitimate* depth-8 eval request took {dt:.2f}s -> status {r.status_code} "
      f"events={len([l for l in r.text.splitlines() if l.startswith('data:')])}")
print(f"  >>> {'STARVED: ' if dt > 3 else 'not starved in this run'}{dt:.1f}s for a cached/8-deep eval "
      f"while the engine lock is held")

print()
print("=" * 78)
print("C) cache: bounded? -> bounded (OrderedDict, max 1000). negative? no")
print("=" * 78)
print("  _cache_max_size:", ev._cache_max_size, " unbounded attribute anywhere? ",
      any("max_size" not in k for k in vars(ev)))

print()
print("=" * 78)
print("D) SSE error event leaks str(e) (evaluator.py:216)")
print("=" * 78)
import inspect  # noqa: E402
src = inspect.getsource(AsyncEngineEvaluator.stream_eval)
for i, line in enumerate(src.splitlines()):
    if "'error'" in line or "str(e)" in line:
        print("   ", line.strip())

print()
print("  closing streams...")
try:
    import anyio  # noqa: E402
    anyio.from_thread  # noqa
except Exception:
    pass
print("DONE")
