"""
CODE-2 repro for CODE-23: the SSE payload key emitted by
backend/evaluator.py vs the key read by frontend/scout.js.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-2_sse_contract.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST

from fastapi.testclient import TestClient
from backend.api import app

client = TestClient(app, raise_server_exceptions=False)

FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

print("=" * 72)
print("Frames actually emitted by GET /api/repertoire/eval_stream?depth=1")
print("=" * 72)
keys = set()
with client.stream("GET", "/api/repertoire/eval_stream?depth=1&fen=" +
                   FEN.replace(" ", "%20")) as r:
    for chunk in r.iter_raw():
        for line in chunk.decode(errors="replace").splitlines():
            if line.startswith("data: "):
                d = json.loads(line[6:])
                keys |= set(d.keys())
                print("  ", line[:160])

print("\nunion of keys emitted by the backend :", sorted(keys))
print("key read by frontend/scout.js:1950    : 'eval'  -> present?", "eval" in keys)
print("key read by frontend/repertoire_builder.js:1778 : 'score_cp' -> present?",
      "score_cp" in keys)

print("\n" + "=" * 72)
print("Simulating scout.js _updateEvalBar(data.eval, data.mate) for a real frame")
print("=" * 72)
with client.stream("GET", "/api/repertoire/eval_stream?depth=1&fen=" +
                   FEN.replace(" ", "%20")) as r:
    for chunk in r.iter_raw():
        for line in chunk.decode(errors="replace").splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                scoreCp, mate = data.get("eval"), data.get("mate")
                evalText, pct = '+0.0', 50
                label = "(unchanged)"
                if mate is not None:
                    evalText, pct = f"M{abs(mate)}", (100 if mate > 0 else 0)
                    label = "white-winning" if mate > 0 else "black-winning"
                elif scoreCp is not None:
                    pct = 50 + 50 * (2 / (1 + __import__("math").exp(-0.004 * scoreCp)) - 1)
                    evalText = f"{scoreCp/100:+.1f}"
                print("  frame          :", line[:120])
                print("  data.eval      :", scoreCp)
                print("  rendered bar   : text=%r  height=%d%%  label=%s"
                      % (evalText, round(pct), label))
                break
        break

print("\nDONE")
