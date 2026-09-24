"""
CODE-01 confirmation probe #3: cost quantification + remaining SQL observations.
Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-01_confirm3.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST backend-adjacent import

import time
import chess
import backend.repertoire as R
from backend.repertoire import RepertoireManager
from backend.db import get_db

SEP = "=" * 78
def hdr(t): print("\n" + SEP + "\n" + t + "\n" + SEP)

rm = RepertoireManager.get_instance()
c = get_db()

# ------------------------------------------------- G
hdr("G) cost of one full get_user_position_stats() rebuild (cache miss)")
c.execute("DELETE FROM games")
base = "e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6 d3 d6 O-O O-O b1d2 a7a6 f1e1 c8e6 c4b3 f8e8 d2f1 e5d4 f3d4 b8c6".split()
rows = []
for i in range(200):
    gid = f"perf{i:04d}"
    rows.append((gid, "rapid", 1, 0, "RobiDeli", "Opp", "white", "white", " ".join(base), 1))
c.executemany("INSERT OR REPLACE INTO games (id,speed,rated,played_at,white_player,black_player,user_color,result,moves_san,analyzed_at) "
              "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
c.commit()
plies = 200 * len(base)
rm._user_stats_cache = {}; rm._user_stats_time = 0
t0 = time.time(); st = rm.get_user_position_stats("white"); dt = time.time() - t0
print(f"  {plies} plies over 200 games -> {dt*1000:.0f} ms, {len(st)} distinct (fen,uci) keys")
print(f"  => ~{dt/plies*1e6:.0f} us per ply; a 3000-game library (~250k plies) costs ~{dt/plies*250000:.1f} s per rebuild")

hdr("G2) shared _user_stats_time lets a stale per-colour entry be served as fresh")
rm._user_stats_cache = {"white": {"SENTINEL": True}}
rm._user_stats_time = time.time() - 3600          # white was computed 1 h ago
rm._user_stats_time = time.time()                  # ... then BLACK recomputed just now
out = rm.get_user_position_stats("white")
print("  get_user_position_stats('white') ->", out, "  (1-hour-old entry served; 0 SQL executed)")

# ------------------------------------------------- H
hdr("H) get_game_gaps(): one NEW connection + one COMMIT per NULL-opening_games row")
c.execute("DELETE FROM blunders")
bl = []
for i in range(25):
    gid = "perf0001"
    bl.append((gid, 3 + (i % 5), 5 + i * 2, "white",
               "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -", "x",
               "e2e4", "e4", "d2d4", "d4", 0, -300, 200, "Test", "A00", None))
c.executemany("INSERT INTO blunders (game_id,move_number,ply,color,fen_before,fen_after,played_move,played_san,best_move,best_san,"
              "eval_before,eval_after,cpl_loss,opening_name,eco,opening_games) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", bl)
c.commit()

opened = {"conns": 0}
real_get_db = R.get_db
def counting_get_db():
    opened["conns"] += 1
    return real_get_db()
explorer_calls = {"n": 0}
real_count = RepertoireManager.get_explorer_game_count
def fake_count(self, fen, ratings="2000,2200,2500"):
    explorer_calls["n"] += 1
    return 0
RepertoireManager.get_explorer_game_count = fake_count
R.get_db = counting_get_db
opened["conns"] = 0
t0 = time.time()
g = rm.get_game_gaps("white", min_games=30)
dt = time.time() - t0
R.get_db = real_get_db
RepertoireManager.get_explorer_game_count = real_count
print(f"  25 NULL rows -> get_db() called {opened['conns']} times, explorer lookups {explorer_calls['n']}, {dt*1000:.1f} ms")
print("  after the backfill, opening_games is 0 for every row, so the >= min_games filter hides them forever:")
print("   min_games=30 ->", len(g["gaps"]), "gaps | min_games=0 ->",
      len(rm.get_game_gaps("white", min_games=0)["gaps"]), "gaps (silent data loss)")

# ------------------------------------------------- I
hdr("I) get_game_gaps(): duplicated unpatched counters + aliasing of all_blunders")
c.execute("UPDATE blunders SET opening_games = 5000")
c.commit()
g = rm.get_game_gaps("white", min_games=0, include_patched=True)
print("  unpatched_gaps =", g["unpatched_gaps"], " unpatched_count =", g["unpatched_count"],
      " (identical expressions, each re-normalising every FEN)")
items = [x for x in g["gaps"] if x["game_id"] == "perf0001"]
print("  gaps entries for game perf0001:", len(items),
      "| all() on the first item's list:", len(items[0]["all_blunders"]),
      "| gaps count:", len(g["gaps"]), "patched_gaps count:", len(g["patched_gaps"]))
print("  -> every gap of a game re-serialises the game's whole blunder list (payload blow-up)")

# ------------------------------------------------- J
hdr("J) import_game_opening(): one connection + commit per ply")
c.execute("DELETE FROM repertoire_moves")
c.commit()
opened["conns"] = 0
R.get_db = counting_get_db
c2 = get_db()
c2.execute("INSERT OR REPLACE INTO games (id,speed,rated,played_at,white_player,black_player,user_color,result,moves_san,analyzed_at) "
           "VALUES ('gJ','rapid',1,0,'RobiDeli','Opp','white','white','e4 e5 Nf3 Nc6 Bc4 Bc5 O-O O-O Re1',0)")
c2.commit(); c2.close()
res = rm.import_game_opening("gJ", "white", max_ply=16)
R.get_db = real_get_db
print("  import result:", res["imported_moves"], "moves ->", opened["conns"], "separate DB connections opened")

# ------------------------------------------------- K
hdr("K) the two 'same DELETE' statements in add_repertoire_move + the games INSERT x4")
import inspect
src = inspect.getsource(RepertoireManager.add_repertoire_move)
print("  occurrences of the user-move DELETE:", src.count("DELETE FROM repertoire_moves"))
full = open(R.__file__).read()
print("  occurrences of 'INSERT OR REPLACE INTO games' in repertoire.py:", full.count("INSERT OR REPLACE INTO games"))
print("  occurrences of \"COALESCE((SELECT analyzed_at FROM games WHERE id = ?), 0)\":",
      full.count("COALESCE((SELECT analyzed_at FROM games WHERE id = ?), 0)"))
print("  occurrences of 'g.get(\"createdAt\", int(time.time() * 1000))':", full.count("int(time.time() * 1000)"))
print("  occurrences of 'g.get(\"createdAt\", int(time.time()))':", full.count("int(time.time())"))
print("\nDONE")
