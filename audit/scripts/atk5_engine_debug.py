#!/usr/bin/env python3
"""Debug: does RepertoireManager's shared SimpleEngine survive a second analyse()?"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import tests.test_setup  # noqa: E402
import chess, chess.engine  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

rm = RepertoireManager.get_instance()
eng = rm._get_engine()
print("pid", eng.transport.get_pid(), "returncode", eng.returncode.result(0) if eng.returncode.done() else "n/a")

FENS = ["rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
        "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2"]

for rnd in range(2):
    for f in FENS:
        b = chess.Board(f)
        mv = next(iter(b.legal_moves))
        b.push(mv)
        try:
            i = eng.analyse(b, chess.engine.Limit(depth=6))
            print(f"  round{rnd} {f[:28]:<30} OK  score={i['score'].white().score()} pv0={i['pv'][0].uci()}")
        except BaseException as e:
            print(f"  round{rnd} {f[:28]:<30} FAIL {type(e).__module__}.{type(e).__name__}: {str(e)[:70]}")
print("returncode after:", eng.returncode.result(0) if eng.returncode.done() else "n/a")
rm.close()
