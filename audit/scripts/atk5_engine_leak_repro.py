#!/usr/bin/env python3
"""
ATK5-13 repro: RepertoireManager.analyze_user_games() (POST /api/repertoire/sync_games)
calls analyzer.close() WITHOUT a finally -> an exception while analysing a game
orphans the Stockfish child process (64 MB hash, 2 threads) for the rest of the
server's life. Its siblings (api.py /api/sync, stream_analyze_user_games) were
hardened; this path was not.

Lichess fetch is stubbed. Isolated temp DB.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import

from backend.repertoire import RepertoireManager  # noqa: E402


def count():
    return int(subprocess.run(["pgrep", "-fc", "stockfish"], capture_output=True, text=True).stdout.strip() or 0)


rm = RepertoireManager.get_instance()
rm.lichess_client.fetch_user_games = lambda *a, **k: [
    {"id": "atk5leak1", "createdAt": 0, "rated": True, "speed": "blitz",
     "moves": "e4 e5 Nf3 Nc6 Bb5", "opening": {"name": "Ruy Lopez", "eco": "C60"},
     "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 2000},
                 "black": {"user": {"name": "Someone"}, "rating": 2000}},
     "winner": "black"},
]

# Simulate the real failure mode: the analyser blows up on game N.
import backend.analyzer as an_mod  # noqa: E402

import chess  # noqa: E402

an_mod.GameAnalyzer.analyze_game_openings = lambda self, g: (
    self._get_engine(),                       # <-- spawns the stockfish child
    [{"game_id": "atk5leak1", "move_number": 1, "ply": 1, "color": "white",
      "fen_before": chess.STARTING_FEN, "fen_after": chess.STARTING_FEN,
      "played_move": "e2e4", "played_san": "e4", "best_move": "e2e4", "best_san": "e4",
      "eval_before": 0, "eval_after": 0, "cpl_loss": 300, "opening_name": "x", "eco": "A00",
      "is_book_move": 0, "refutation_pv": "[]", "explanation": "x", "strategic_idea": "x",
      "category": "Blunder", "created_at": 0}])[1]


def _boom(self, blunders):
    raise RuntimeError("simulated failure AFTER the engine was spawned")


an_mod.GameAnalyzer.save_blunders_to_db = _boom
# analyze_user_games imports GameAnalyzer locally: `from backend.analyzer import GameAnalyzer`
# so patching the class attribute is enough.

n_before = count()
mine_before = set(subprocess.run(["pgrep", "-f", "stockfish"], capture_output=True,
                                 text=True).stdout.split())
try:
    rm.analyze_user_games(max_games=1, max_ply=10)
    print("  (no exception raised - path not exercised)")
except Exception as e:
    print(f"  analyze_user_games raised {type(e).__name__}: {str(e)[:70]}")

time.sleep(0.5)
n_after = count()
print(f"  stockfish processes before: {n_before}  after: {n_after}")
print("  leftover stockfish pids:", subprocess.run(["pgrep", "-f", "stockfish"],
                                                  capture_output=True, text=True).stdout.split())
print()
print("  (backend/repertoire.py:1889 `analyzer.close()` is NOT in a finally:)")
import inspect  # noqa: E402
src = inspect.getsource(RepertoireManager.analyze_user_games)
lines = src.splitlines()
for i, l in enumerate(lines):
    if "analyzer = GameAnalyzer()" in l or "analyzer.close()" in l or "finally" in l:
        print(f"    {i:>4}: {l.rstrip()}")
for p in subprocess.run(["pgrep", "-f", "stockfish"], capture_output=True, text=True).stdout.split():
    if p not in {q for q in mine_before}:
        continue
    subprocess.run(["kill", "-9", p], capture_output=True)
print("  (cleaned up only the engines this probe spawned)")
