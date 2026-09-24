#!/usr/bin/env python3
"""
ATK5-05 repro: resource exhaustion / algorithmic complexity.

 A) huge `fen` query param -> is_valid_fen / chess.Board cost
 B) huge JSON list on /api/scout/check_tree and /api/scout/import_repertoire
    (unbounded List[Dict[str, Any]])
 C) deeply nested JSON -> RecursionError
 D) unbounded request body (no size limit middleware)
 E) /api/repertoire/eval_stream concurrency: N simultaneous SSE streams on the
    single-stockfish async lock; can one request monopolise the engine?
 F) regex cost of scout's ReDoS-prone patterns on a crafted video description

Idempotent, isolated temp DB, network stubbed. Stockfish IS used (bounded).
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import

import requests


class _R:
    status_code = 503
    text = "stub"

    def json(self):
        return {}

    def iter_lines(self, decode_unicode=False):
        return iter(())


requests.get = lambda *a, **k: _R()
requests.post = lambda *a, **k: _R()
import yt_dlp


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

from backend.api import app, is_valid_fen  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
J = {"Content-Type": "application/json"}

print("=" * 78)
print("A) is_valid_fen() cost vs FEN length  (parts[0].count('/') must == 7)")
print("=" * 78)
import chess  # noqa: E402
for n in (1, 100, 1_000, 10_000, 100_000, 400_000):
    big = "P" * n + "/" * 7 + " w - - 0 1"
    t0 = time.time()
    try:
        r = is_valid_fen(big)
    except Exception as e:
        r = f"EXC {type(e).__name__}"
    dt = time.time() - t0
    print(f"  pieces-field len={n:<8} is_valid_fen={r!s:<6} {dt*1000:9.2f} ms")

# a legal-looking but very long rank that python-chess must actually parse
print()
for n in (1_000, 10_000, 100_000):
    row = ("p" * n) if n <= 8 else ("P" * n)
    big = row + "/" * 7 + " w - - 0 1"
    t0 = time.time()
    try:
        b = chess.Board(big)
        ok = b.is_valid()
    except Exception as e:
        ok = f"EXC {type(e).__name__}: {e}"
    print(f"  full Board() parse len={n:<8} -> {ok!s:<40} {(time.time()-t0)*1000:9.2f} ms")

print()
print("  via HTTP (GET /api/repertoire/explore?fen=...):")
import urllib.parse  # noqa: E402
for n in (1_000, 50_000, 200_000):
    big = "P" * n + "/" * 7 + " w - - 0 1"
    url = "/api/repertoire/explore?fen=" + urllib.parse.quote(big)
    t0 = time.time()
    try:
        r = client.get(url)
        print(f"    fen len={n:<8} -> {r.status_code} {(time.time()-t0)*1000:9.2f} ms {r.text[:60]!r}")
    except Exception as e:
        print(f"    fen len={n:<8} -> EXC {type(e).__name__} {(time.time()-t0)*1000:9.2f} ms {str(e)[:80]}")

print()
print("=" * 78)
print("B) unbounded List[Dict[str, Any]] -> /api/scout/check_tree (moves list)")
print("=" * 78)
big_lines = [{"repertoire": "white", "moves": [{"fen_before": FEN, "uci": "e2e4", "san": "e4"}]} for _ in range(2000)]
payload = {"repertoire": "white", "lines": big_lines}
raw = __import__("json").dumps(payload).encode()
print("  request body bytes:", len(raw))
t0 = time.time()
try:
    r = client.post("/api/scout/check_tree", content=raw, headers=J)
    print(f"  -> {r.status_code} in {(time.time()-t0):.2f}s  resp={r.text[:100]!r}")
except Exception as e:
    print(f"  -> EXC {type(e).__name__} in {(time.time()-t0):.2f}s: {str(e)[:120]}")

print()
print("=" * 78)
print("C) deeply nested JSON body -> RecursionError?")
print("=" * 78)
for depth in (100, 1000, 5000, 50_000):
    body = ('{"repertoire":"white","moves":' + "[" * depth + "]" * depth + "}")
    t0 = time.time()
    try:
        r = client.post("/api/scout/check_line", content=body.encode(), headers=J)
        print(f"  depth={depth:<8} -> {r.status_code} {(time.time()-t0)*1000:8.2f} ms  {r.text[:90]!r}")
    except Exception as e:
        print(f"  depth={depth:<8} -> EXC {type(e).__name__} {(time.time()-t0)*1000:8.2f} ms {str(e)[:110]}")

print()
print("=" * 78)
print("D) unbounded request body (no Body-size middleware)")
print("=" * 78)
for mb in (1, 8, 32):
    body = ('{"repertoire":"white","moves":[{"fen_before":"' + "A" * (mb * 1024 * 1024) + '"}]}')
    t0 = time.time()
    try:
        r = client.post("/api/scout/check_line", content=body.encode(), headers=J)
        print(f"  body~{mb}MB -> {r.status_code} {(time.time()-t0)*1000:9.2f} ms {r.text[:80]!r}")
    except Exception as e:
        print(f"  body~{mb}MB -> EXC {type(e).__name__} {(time.time()-t0)*1000:9.2f} ms {str(e)[:110]}")

print()
print("=" * 78)
print("E) /api/scout/analyze with a huge raw PGN (pathological CPU)")
print("=" * 78)
pgn = "[Event \"x\"]\n\n1. e4 " * 20000
r = client.post("/api/scout/analyze", content=('{"source":' + __import__("json").dumps(pgn) + '}').encode(), headers=J)
print("  20k-move PGN ->", r.status_code, r.text[:150])

print()
print("=" * 78)
print("F) scout regex cost on a crafted 'description'-like string")
print("=" * 78)
from backend.scout import ContentScout, normalize_chess_speech  # noqa: E402
cs = ContentScout.get_instance()
pat = r'(?:(\d{1,2}:\d{2})\s+)?(?:[Vv]ariation|[Ll]ine)?\s*([A-Za-z0-9\s-]+)?[:\-]?\s*((?:1\.\s*[e4d4c4Nf3][^\n\r]+))'
for n in (1_000, 10_000, 50_000, 200_000):
    s = "a" * n
    t0 = time.time()
    len(re.findall(pat, s))
    print(f"  regex finditer len={n:<8} {(time.time()-t0)*1000:9.2f} ms")
for n in (1_000, 10_000, 50_000):
    s = ("a " * n) + "!"
    t0 = time.time()
    normalize_chess_speech(s)
    print(f"  normalize_chess_speech len={n*2:<8} {(time.time()-t0)*1000:9.2f} ms")

print()
print("=" * 78)
print("G) eval_stream: 8 concurrent SSE streams on the single engine lock")
print("=" * 78)
import threading  # noqa: E402
results = {}


def grab(i):
    t0 = time.time()
    try:
        with client.stream("GET", f"/api/repertoire/eval_stream?fen={urllib.parse.quote(FEN)}&depth=12") as s:
            n = 0
            for _ in s.iter_lines():
                n += 1
                if n > 200:
                    break
        results[i] = (n, time.time() - t0)
    except Exception as e:
        results[i] = (f"EXC {type(e).__name__}: {str(e)[:80]}", time.time() - t0)


ts = [threading.Thread(target=grab, args=(i,)) for i in range(8)]
t0 = time.time()
[t.start() for t in ts]
[t.join() for t in ts]
print(f"  wall clock for 8 concurrent depth-12 streams: {time.time()-t0:.2f}s")
for i, (n, dt) in sorted(results.items(), key=lambda kv: str(kv[0])):
    print(f"    stream {i}: events={n} elapsed={dt:.2f}s")

print()
print("DONE")
