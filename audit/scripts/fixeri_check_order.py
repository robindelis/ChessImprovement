"""FIXER-I: does test_repertoire leave the Italian 4-ply line in the White repertoire?

Determines whether SCOUT-11's "no write when already_present" fix would flip the
status of an existing test in tests/test_scout.py. Read-only against the temp DB.

    PYTHONPATH=. /home/robin/miniconda3/bin/python3 audit/scripts/fixeri_check_order.py
"""
import unittest

import tests.test_setup  # FIRST

import chess
from backend.repertoire import RepertoireManager

loader = unittest.TestLoader()
suite = loader.loadTestsFromNames(["tests.test_repertoire"])
unittest.TextTestRunner(verbosity=0, stream=open("/dev/null", "w")).run(suite)

rm = RepertoireManager.get_instance()
board = chess.Board()
moves = []
for san in ["e4", "e5", "Nf3", "Nc6"]:
    moves.append({"fen_before": board.fen(), "san": san, "uci": board.parse_san(san).uci()})
    board.push_san(san)
ov = rm.check_line_overlap("white", moves)
print("italian 4-ply overlap after test_repertoire:", ov["status"],
      ov["matched_plies"], "/", ov["total_plies"], flush=True)
