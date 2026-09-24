#!/usr/bin/env python3
"""Debug 2: how many sequential eng.analyse() calls before the engine dies?"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import tests.test_setup  # noqa: E402
import chess, chess.engine  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

rm = RepertoireManager.get_instance()
eng = rm._get_engine()
b = chess.Board()
MOVES = ["e2e4", "d2d4", "g1f3", "c2c4", "b1c3", "f1c4", "e1g1", "g2g3", "d1d2", "b2b3"]
for u in MOVES:
    mv = chess.Move.from_uci(u)
    if mv not in b.legal_moves:
        print(f"  {u} illegal, skipping")
        continue
    test = b.copy()
    test.push(mv)
    try:
        i = eng.analyse(test, chess.engine.Limit(depth=10))
        rc = eng.transport.get_returncode()
        print(f"  {u}: score={i['score'].white().score()} pv0={i['pv'][0].uci()} returncode={rc}")
    except BaseException as e:
        rc = eng.transport.get_returncode()
        print(f"  {u}: FAIL {type(e).__name__}: {str(e)[:60]} returncode={rc}")
        break
print("final returncode:", eng.transport.get_returncode())
try:
    rm.close()
except Exception as e:
    print("close failed:", e)
