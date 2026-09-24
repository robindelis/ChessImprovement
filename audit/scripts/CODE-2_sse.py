"""
CODE-2 follow-up: SSE-through-BaseHTTPMiddleware behaviour + value_error handler
leak + duplicate route registration inventory.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-2_sse.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST

from fastapi.testclient import TestClient
from backend.api import app

client = TestClient(app, raise_server_exceptions=False)


def hdr(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


hdr("A) Does the global BaseHTTPMiddleware buffer SSE chunks?")
t0 = time.time()
with client.stream("GET", "/api/repertoire/eval_stream?depth=1&fen=" +
                   "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR%20w%20KQkq%20-%200%201") as r:
    for chunk in r.iter_raw():
        if chunk.strip():
            print("  t=+%.2fs  %r" % (time.time() - t0, chunk[:110]))
        if time.time() - t0 > 25:
            break

hdr("B) Route inventory: duplicate registrations & orphans")
for route in app.routes:
    p = getattr(route, "path", None)
    m = getattr(route, "methods", None)
    if p and p.startswith("/api"):
        print("  %-6s %s" % (",".join(sorted(m or [])), p))

hdr("C) Global ValueError handler converts internal errors into 400 + raw text")
r = client.get("/api/repertoire/clear?repertoire=purple")
print("  /api/repertoire/clear?repertoire=purple ->", r.status_code, r.text[:160])

hdr("D) evaluate_training_move spawns a brand-new Stockfish per alternative move")
import inspect
from backend import api as api_mod
src = inspect.getsource(api_mod.evaluate_training_move)
print("  GameAnalyzer() constructed inside the request handler:",
      "analyzer = GameAnalyzer()" in src)
print("  (no engine pooling / module-level singleton)")

print("\nDONE")
