"""BUILDER-06 probe: /api/repertoire/eval_stream (SSE) behaviour and engine handling."""
import os
import sys
import json
import time
import threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

from backend.evaluator import AsyncEngineEvaluator

FEN = chess.STARTING_FEN
MATED = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"   # fool's mate
STALEMATE = "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"


def stream(fen, depth=8, label=""):
    t0 = time.time()
    events = []
    try:
        with CLIENT.stream("GET", "/api/repertoire/eval_stream", params={"fen": fen, "depth": depth}) as r:
            status = r.status_code
            ctype = r.headers.get("content-type")
            for line in r.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
    except Exception as e:
        print(f"  [{label}] EXC {type(e).__name__}: {e}")
        return None
    print(f"  [{label}] {status} {ctype} {len(events)} events in {time.time()-t0:.1f}s")
    for e in events:
        print("      ", json.dumps(e))
    return events


print("=" * 78)
print("A. parameter validation")
print("=" * 78)
for label, params in [
    ("no fen", {"depth": 8}),
    ("garbage fen", {"fen": "garbage"}),
    ("depth=0", {"fen": FEN, "depth": 0}),
    ("depth=31", {"fen": FEN, "depth": 31}),
    ("depth=abc", {"fen": FEN, "depth": "abc"}),
]:
    r = CLIENT.get("/api/repertoire/eval_stream", params=params)
    print(f"  [{label}] -> {r.status_code} {r.text[:110]}")

print("\n" + "=" * 78)
print("B. normal progressive stream (depth 8)")
print("=" * 78)
ev = stream(FEN, 8, "startpos d8")
if ev:
    depths = [e.get("depth") for e in ev]
    print("   depths:", depths, " final flags:", [e.get("final") for e in ev])
    print("   monotonically non-decreasing:", all(depths[i] <= depths[i+1] for i in range(len(depths)-1)))
    print("   exactly one final=True:", sum(1 for e in ev if e.get("final")) == 1)

print("\n" + "=" * 78)
print("C. terminal positions (no engine search expected)")
print("=" * 78)
stream(MATED, 8, "checkmate")
stream(STALEMATE, 8, "stalemate")

print("\n" + "=" * 78)
print("D. second visit to the same FEN: served from the in-process cache?")
print("=" * 78)
t0 = time.time()
ev2 = stream(FEN, 8, "startpos d8 (2nd)")
print("   elapsed %.2fs" % (time.time() - t0))
print("   cached flags:", [e.get("cached") for e in (ev2 or [])])

print("\n" + "=" * 78)
print("E. cache key ignores halfmove/fullmove counters (4-token normalize_fen)")
print("=" * 78)
e = AsyncEngineEvaluator.get_instance()
e._cache.clear()
alt = FEN.replace(" 0 1", " 37 99")          # same position, different clocks
stream(alt, 8, "alt clocks")
print("   cache keys after the alt request:", list(e._cache.keys()))
print("   -> the eval for the 0/1 FEN is reused for the 37/99 FEN:", FEN.split(' ')[:4] == alt.split(' ')[:4])

print("\n" + "=" * 78)
print("F. concurrency: 4 simultaneous streams (engine is behind an asyncio.Lock)")
print("=" * 78)
fens = [
    FEN,
    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/2P5/8/PP1PPPPP/RNBQKBNR b KQkq - 0 1",
]
res = {}
bar = threading.Barrier(len(fens))


def w(i, f):
    bar.wait()
    t = time.time()
    try:
        with CLIENT.stream("GET", "/api/repertoire/eval_stream", params={"fen": f, "depth": 10}) as r:
            n = sum(1 for line in r.iter_lines() if line.startswith("data: "))
        res[i] = (r.status_code, n, round(time.time() - t, 1))
    except Exception as ex:
        res[i] = ("EXC", f"{type(ex).__name__}: {ex}", round(time.time() - t, 1))


ths = [threading.Thread(target=w, args=(i, f)) for i, f in enumerate(fens)]
t0 = time.time()
for t in ths:
    t.start()
for t in ths:
    t.join(180)
print("   total wall %.1fs" % (time.time() - t0))
for i in sorted(res):
    print("   stream", i, res[i])
