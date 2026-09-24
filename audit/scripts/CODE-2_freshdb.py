"""
CODE-2 follow-up: prove the `blunders.best_pv` schema gap on a *fresh* database
(i.e. any new install / new machine), and the startup-migration reachability purge.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-2_freshdb.py
"""
import os
import sys
import json
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST

# --- Build a BRAND NEW empty database using only the repo's own init_db() ---
import backend.db as db_mod
fd, fresh = tempfile.mkstemp(prefix="chess_fresh_", suffix=".db")
os.close(fd)
db_mod.set_db_path(fresh)
db_mod.DB_PATH = fresh
os.environ["CHESS_DB_PATH"] = fresh
db_mod.init_db()

conn = sqlite3.connect(fresh)
conn.row_factory = sqlite3.Row
cols = [r["name"] for r in conn.execute("PRAGMA table_info(blunders)")]
print("FRESH DB blunders columns:", cols)
print("  best_pv present on fresh DB:", "best_pv" in cols)

sql = ("INSERT INTO blunders (game_id, move_number, ply, color, fen_before, fen_after,"
       " played_move, played_san, best_move, best_san, eval_before, eval_after,"
       " cpl_loss, opening_name, eco, is_book_move, refutation_pv, best_pv, explanation,"
       " strategic_idea, category, opening_games, created_at) "
       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
try:
    conn.execute(sql, ("g", 1, 1, "white", "f", "f", "e2e4", "e4", "d2d4", "d4",
                       0, 0, 0, "n", "A00", 0, "[]", "[]", "x", "y", "Inaccuracy", None, 1))
    conn.commit()
    print("  INSERT with best_pv -> OK")
except Exception as e:
    print("  INSERT with best_pv -> %s: %s" % (type(e).__name__, e))
conn.close()
os.unlink(fresh)
print("\n-- live server's production DB is a COPY; check it in a read-only manner --")
