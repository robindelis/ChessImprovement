"""
CODE-2 static-audit repro harness (read-only against production; temp DB only).

Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-2_repro.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST -- points CHESS_DB_PATH at a throwaway temp DB

import json
import sqlite3
import os

from backend.db import get_db
from fastapi.testclient import TestClient
from backend.api import app

client = TestClient(app, raise_server_exceptions=False)


def hdr(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


# ---------------------------------------------------------------- PROBE 1
hdr("PROBE 1  blunders.best_pv column does not exist in schema (db.py)")
conn = get_db()
cols = [r["name"] for r in conn.execute("PRAGMA table_info(blunders)").fetchall()]
print("blunders columns:", cols)
print("best_pv present :", "best_pv" in cols)
sql = ("INSERT INTO blunders (game_id, move_number, ply, color, fen_before, fen_after,"
       " played_move, played_san, best_move, best_san, eval_before, eval_after,"
       " cpl_loss, opening_name, eco, is_book_move, refutation_pv, best_pv, explanation,"
       " strategic_idea, category, opening_games, created_at) "
       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
try:
    conn.execute(sql, ("gX", 1, 1, "white", "f", "f", "e2e4", "e4", "d2d4", "d4",
                       0, 0, 0, "n", "A00", 0, "[]", "[]", "x", "y", "Inaccuracy",
                       None, 1))
    print("INSERT ... best_pv -> OK (unexpected)")
except Exception as e:
    print("INSERT ... best_pv -> %s: %s" % (type(e).__name__, e))
conn.close()


# ---------------------------------------------------------------- PROBE 2
hdr("PROBE 2  GameAnalyzer.save_blunders_to_db against real schema")
from backend.analyzer import GameAnalyzer
a = GameAnalyzer()
a._engine = object()  # skip engine spawn
b = [{
    "game_id": "gY", "move_number": 1, "ply": 1, "color": "white",
    "fen_before": "f", "fen_after": "f", "played_move": "e2e4", "played_san": "e4",
    "best_move": "d2d4", "best_san": "d4", "eval_before": 0, "eval_after": -200,
    "cpl_loss": 200, "opening_name": "n", "eco": "A00", "is_book_move": 0,
    "refutation_pv": "[]", "best_pv": "[]", "explanation": "x", "strategic_idea": "y",
    "category": "Mistake", "created_at": 0,
}]
try:
    a.save_blunders_to_db(b)
    print("save_blunders_to_db -> OK (unexpected)")
except Exception as e:
    print("save_blunders_to_db -> %s: %s" % (type(e).__name__, e))


# ---------------------------------------------------------------- PROBE 3
hdr("PROBE 3  /api/blunders?min_loss=0  (falsy-check drops the filter)")
conn = get_db()
conn.execute("INSERT INTO games (id, user_color, result) VALUES ('g1','white','white')")
conn.execute("INSERT INTO blunders (game_id, move_number, ply, color, cpl_loss, eco, created_at)"
             " VALUES ('g1',1,1,'white',50,'B20',1)")
conn.execute("INSERT INTO blunders (game_id, move_number, ply, color, cpl_loss, eco, created_at)"
             " VALUES ('g1',2,3,'white',300,'B20',1)")
conn.commit()
conn.close()
r = client.get("/api/blunders?min_loss=0")
print("status", r.status_code, "-> cpl_loss values returned:",
      [x["cpl_loss"] for x in r.json()])
r = client.get("/api/blunders?min_loss=100")
print("status", r.status_code, "-> cpl_loss values returned:",
      [x["cpl_loss"] for x in r.json()])


# ---------------------------------------------------------------- PROBE 4
hdr("PROBE 4  OpeningBook.get_strategic_ideas ignores its `eco` argument")
from backend.openings import OpeningBook
book = OpeningBook.get_instance()
print("eco='B70' name=''  ->", book.get_strategic_ideas("B70", "")["name"])
print("eco='B20' name=''  ->", book.get_strategic_ideas("B20", "")["name"])
print("eco=''    name='Sicilian Defense' ->",
      book.get_strategic_ideas("", "Sicilian Defense")["name"])


# ---------------------------------------------------------------- PROBE 5
hdr("PROBE 5  init_db() startup migration query plan (study_moves join)")
conn = get_db()
conn.execute("CREATE TABLE IF NOT EXISTS study_moves_dummy AS SELECT * FROM study_moves WHERE 0")
for row in conn.execute("PRAGMA index_list(study_moves)").fetchall():
    print("index on study_moves:", dict(row))
print("\n-- plan for the migration LEFT JOIN (db.py:330) --")
for row in conn.execute("""EXPLAIN QUERY PLAN
    SELECT rm.id, rm.parent_fen, rm.fen, sm.study_id
    FROM repertoire_moves rm
    LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire
        AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
    WHERE rm.repertoire = 'white'""").fetchall():
    print("   ", row["detail"])
conn.close()


# ---------------------------------------------------------------- PROBE 6
hdr("PROBE 6  /api/sync response shape on the no-games / failure path")
from backend import lichess_client
orig = lichess_client.LichessClient.fetch_user_games
lichess_client.LichessClient.fetch_user_games = lambda self, *a, **k: []
r = client.post("/api/sync?max_games=3")
print("HTTP", r.status_code, r.json())
lichess_client.LichessClient.fetch_user_games = orig


# ---------------------------------------------------------------- PROBE 7
hdr("PROBE 7  unmatched /api/* path is swallowed by the StaticFiles('/') mount")
r = client.get("/api/this_endpoint_does_not_exist")
print("HTTP", r.status_code, "content-type:", r.headers.get("content-type"))
print("body[:120]:", r.text[:120].replace("\n", " "))


# ---------------------------------------------------------------- PROBE 8
hdr("PROBE 8  /api/repertoire/train/recent_opening with no name -> HTTP 200 'error'")
r = client.post("/api/repertoire/train/recent_opening", json={"name": "  "})
print("HTTP", r.status_code, r.json())
r2 = client.post("/api/repertoire/train/recent_opening", json={"name": "Sicilian"})
print("HTTP", r2.status_code, r2.json())


# ---------------------------------------------------------------- PROBE 9
hdr("PROBE 9  get_strategic_ideas / ValueError handler leaks raw exception strings")
r = client.get("/api/repertoire/tree?color=white&threshold=99.9")
print("threshold out of range -> HTTP", r.status_code, r.text[:200])
r = client.get("/api/repertoire/explore?fen=not-a-fen")
print("garbage fen           -> HTTP", r.status_code, r.text[:200])

print("\nDONE")
