"""
CODE-01 confirmation probe #2: perf / duplication / seed-guard evidence.
Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-01_confirm2.py
Isolated temp DB (tests.test_setup imported first). No network to the live server.
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

# ---------------------------------------------------------------- A
hdr("A) get_repertoire_tree() calls get_candidate_moves_2000() TWICE per opponent position")
calls = {"n": 0, "fens": []}
orig = RepertoireManager.get_candidate_moves_2000
def counting(self, fen, ratings="2000,2200,2500", color="white"):
    calls["n"] += 1
    calls["fens"].append((fen[:28], color))
    return {"moves": [], "total_games": 0}
RepertoireManager.get_candidate_moves_2000 = counting
b = chess.Board()
for u in ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5"]:
    rm.add_repertoire_move("white", b.fen(), u, is_user_move=(b.turn == chess.WHITE))
    b.push_uci(u)
tree = rm.get_repertoire_tree("white")
RepertoireManager.get_candidate_moves_2000 = orig
print("  repertoire rows:", tree["total_moves"], "-> candidate-moves calls:", calls["n"])
for f in calls["fens"]:
    print("   ", f)
print("  NOTE the `color` argument is never forwarded from the repertoire colour.")

# ---------------------------------------------------------------- B
hdr("B) user-stats cache: one shared _user_stats_time for BOTH colours -> thrash")
rm._user_stats_cache = {}; rm._user_stats_time = 0
c = get_db()
for i in range(3):
    for col, res in (("white", "white"), ("black", "black")):
        c.execute("INSERT OR REPLACE INTO games (id,speed,rated,played_at,white_player,black_player,user_color,result,moves_san,analyzed_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,0)", (f"g{col}{i}", "rapid", 1, 0, "RobiDeli" if col == "white" else "Opp",
                                                   "Opp" if col == "white" else "RobiDeli", col, res, "e4 e5 Nf3 Nc6 Bc4 Nf3 d3 d6"))
c.commit(); c.close()

exec_count = {"n": 0}
real_get_db = R.get_db
def traced_get_db():
    conn = real_get_db()
    conn.set_trace_callback(lambda sql: exec_count.__setitem__("n", exec_count["n"] + 1))
    return conn
R.get_db = traced_get_db
rm._user_stats_cache = {}; rm._user_stats_time = 0
t0 = time.time(); rm.get_user_position_stats("white"); t_white = time.time() - t0
e_after_white = exec_count["n"]
rm.get_user_position_stats("black")                 # resets the single shared timestamp
exec_count["n"] = 0
t0 = time.time(); rm.get_user_position_stats("white"); t_white2 = time.time() - t0
R.get_db = real_get_db
print(f"  first  white call : {e_after_white} SQL statements, {t_white*1000:.1f} ms")
print(f"  white again right after a black call: {exec_count['n']} SQL statements, {t_white2*1000:.1f} ms  <-- CACHE MISS")
print("  cache keys:", list(rm._user_stats_cache.keys()), " shared timestamp:", rm._user_stats_time)

# ---------------------------------------------------------------- C
hdr("C) get_candidate_moves_2000() personal stats use color='white' even for a BLACK repertoire")
rm._user_stats_cache = {}; rm._user_stats_time = 0
RepertoireManager.get_candidate_moves_2000 = lambda self, fen, ratings="2000,2200,2500", color="white": {
    "moves": [], "total_games": 0}
src = open(os.path.join(os.path.dirname(R.__file__), "repertoire.py")).read()
import inspect
sig = inspect.getsource(RepertoireManager.get_repertoire_tree)
print("  occurrences of get_candidate_moves_2000 inside get_repertoire_tree:",
      sig.count("get_candidate_moves_2000"))
print("  every call site passes only `pos_fen` -> color defaults to 'white' (personal stats are the")
print("  WHITE player's games even for the black repertoire, and ratings default to '2000,2200,2500').")

# ---------------------------------------------------------------- D
hdr("D) seed_default_repertoires(force=True) vs. a prepared Anti-French 3.e5 (AGENTS.md 9.3)")
c = get_db()
c.execute("DELETE FROM repertoire_moves"); c.execute("DELETE FROM study_moves"); c.execute("DELETE FROM repertoire_studies")
c.commit(); c.close()
b = chess.Board()
for uci, user in [("e2e4", True), ("e7e6", False), ("d2d4", True), ("d7d5", False), ("e4e5", True)]:
    rm.add_repertoire_move("white", b.fen(), uci, is_user_move=user)
    b.push_uci(uci)
rm.update_move_metadata("white", chess.STARTING_FEN, "e2e4", comment="my anti-french prep", is_important=True)
c = get_db()
def snapshot(tag):
    rows = c.execute("SELECT id, move_uci, is_user_move, comment, study_name FROM repertoire_moves ORDER BY id").fetchall()
    print(f"  [{tag}] {len(rows)} rows: " + ", ".join(f"{r['move_uci']}(u{r['is_user_move']})" for r in rows))
    return [dict(r) for r in rows]
before = snapshot("before force-seed")
print("  seed(force=False) ->", rm.seed_default_repertoires(force=False))
after_guard = snapshot("after guard (no force)")
rm.seed_default_repertoires(force=True)
after = snapshot("after force=True")
print("  rows destroyed by force=True:",
      sorted({r['move_uci'] for r in before} - {r['move_uci'] for r in after}))
c.close()

# ---------------------------------------------------------------- E
hdr("E) seed_default_repertoires() return contract")
c = get_db(); c.execute("DELETE FROM repertoire_moves"); c.commit(); c.close()
rv = rm.seed_default_repertoires(force=True)
print("  seeding path returns:", repr(rv))
rv2 = rm.seed_default_repertoires(force=False)
print("  guard path returns  :", repr(rv2))

# ---------------------------------------------------------------- F
hdr("F) build_repertoire_tree_graph: 'm not in list' dedup cost + queue.pop(0)")
src = inspect.getsource(RepertoireManager.get_repertoire_tree)
print("  get_repertoire_tree contains `if m not in tree_by_parent[p3]`:",
      "if m not in tree_by_parent[p3]" in src)
g = inspect.getsource(RepertoireManager.build_repertoire_tree_graph)
print("  build_repertoire_tree_graph uses queue.pop(0):", "queue.pop(0)" in g)
print("  BFS copies path_san/path_moves per edge:",
      "new_path_san = path_san + [m[\"move_san\"]]" in g)
print("\nDONE")
