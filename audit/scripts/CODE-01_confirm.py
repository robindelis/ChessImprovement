"""
CODE-01 confirmation probe. Static-review confirmations for backend/repertoire.py.
Run:  /home/robin/miniconda3/bin/python3 audit/scripts/CODE-01_confirm.py

STRICTLY isolated: imports tests.test_setup FIRST (temp DB copy, atexit cleanup).
Never touches chess_improvement.db and never speaks HTTP to 127.0.0.1:38281.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST backend-adjacent import (BRIEF hard rule 2)

import sqlite3
import os
import time
import chess
from backend.db import get_db
from backend.repertoire import RepertoireManager, normalize_fen

SEP = "=" * 78


def hdr(t):
    print("\n" + SEP + "\n" + t + "\n" + SEP)


rm = RepertoireManager.get_instance()

# ---------------------------------------------------------------- 1
hdr("1) add_repertoire_move() on an ALREADY-PRESENT move wipes metadata + SR state")
b = chess.Board()
p_fen = b.fen()
print("add  :", rm.add_repertoire_move("white", p_fen, "e2e4", is_user_move=True)["status"])
print("meta :", rm.update_move_metadata("white", p_fen, "e2e4", is_important=True,
                                        comment="my precious note", arrows='[{"from":"e2","to":"e4"}]',
                                        study_name="Anti French"))
# practice it twice so spaced-repetition state is non-default
rm.evaluate_trainer_move("white", p_fen, "e2e4")
rm.evaluate_trainer_move("white", p_fen, "e2e4")

def dump(tag):
    c = get_db()
    r = c.execute("""SELECT id, is_important, comment, arrows, study_name,
                             times_practiced, times_correct, last_practiced,
                             next_review, interval_days, created_at
                      FROM repertoire_moves WHERE move_uci='e2e4'""").fetchone()
    c.close()
    print(f"  [{tag}] " + str(dict(r)))

dump("before re-add")
res = rm.add_repertoire_move("white", p_fen, "e2e4", is_user_move=True, study_name="Anti French")
print("  re-add status:", res["status"], "-> returned is_important:", res["is_important"],
      "comment:", repr(res["comment"]), "arrows:", repr(res["arrows"]), "study:", repr(res["study_name"]))
dump("after  re-add")
prog = rm.get_trainer_progress("white")
print("  trainer progress after re-add:", {k: prog[k] for k in ("total_moves", "mastered_count", "learning_count", "unlearned_count")})

# ---------------------------------------------------------------- 2
hdr("2) import_game_opening() on an already-imported game wipes the same state")
c = get_db()
c.execute("DELETE FROM repertoire_moves")
c.execute("INSERT INTO games (id, speed, rated, played_at, white_player, black_player, user_color, result, moves_san, analyzed_at) "
          "VALUES ('gTEST01','rapid',1,0,'RobiDeli','Opponent','white','white','e4 e5 Nf3 Nc6 Bc4', 0)")
c.commit(); c.close()
print("import #1:", rm.import_game_opening("gTEST01", "white"))
rm.update_move_metadata("white", p_fen, "e2e4", is_important=True, comment="keep me")
rm.evaluate_trainer_move("white", p_fen, "e2e4")
rm.evaluate_trainer_move("white", p_fen, "e2e4")
dump("before import #2")
print("import #2:", rm.import_game_opening("gTEST01", "white"))
dump("after  import #2")

# ---------------------------------------------------------------- 3
hdr("3) INSERT OR REPLACE ... COALESCE((SELECT analyzed_at FROM games WHERE id=?))")
c = get_db()
c.execute("INSERT OR REPLACE INTO games (id, speed, rated, played_at, white_player, black_player, user_color, result, moves_san, analyzed_at) "
          "VALUES ('gTEST01','rapid',1,0,'a','b','white','white','e4', 12345)")
c.commit()
c.execute("""INSERT OR REPLACE INTO games (
        id, speed, rated, played_at, white_player, black_player,
        user_color, result, opening_name, eco, moves_san, analyzed_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT analyzed_at FROM games WHERE id = ?), 0))
    """, ('gTEST01', 'rapid', 1, 0, 'a', 'b', 'white', 'white', 'X', 'A20', 'e4 e5', 'gTEST01'))
row = c.execute("SELECT analyzed_at FROM games WHERE id='gTEST01'").fetchone()
c.close()
print("  analyzed_at AFTER the exact sync_recent_games INSERT:", row[0], "(expected 12345 if the subquery sees the old row)")

# ---------------------------------------------------------------- 4
hdr("4) python-chess: does Board.push() reject an illegal-but-parseable UCI?")
b = chess.Board()
try:
    b.push(chess.Move.from_uci("e2e5"))  # illegal from start
    print("  push('e2e5') ACCEPTED -> fen:", b.fen())
    print("  legal_moves now:", len(list(b.legal_moves)))
except Exception as e:
    print("  push raised:", type(e).__name__, e)

# ---------------------------------------------------------------- 5
hdr("5) normalize_fen(): what does the 4th token do to the 3-token LIKE fallback?")
print("  normalize_fen(STARTING_FEN) =", repr(normalize_fen(chess.STARTING_FEN)))
b = chess.Board(); b.push_uci("e2e4"); b.push_uci("e7e5"); b.push_uci("g1f3"); b.push_uci("d7d5")
print("  after 1.e4 e5 2.Nf3 d5 (ep square set):", repr(normalize_fen(b.fen())))
p3 = " ".join(normalize_fen(b.fen()).split()[:3])
c = get_db()
c.execute("INSERT OR REPLACE INTO repertoire_moves (repertoire,parent_fen,fen,move_uci,move_san,is_user_move) VALUES (?,?,?,?,?,0)",
          ("white", normalize_fen(b.fen()), normalize_fen(b.fen()), "b8c6", "Nc6"))
c.commit()
hit = c.execute("SELECT COUNT(*) FROM repertoire_moves WHERE parent_fen LIKE ?", (f"{p3} %",)).fetchone()[0]
c.close()
print(f"  rows matching  parent_fen LIKE '{p3} %'  ->  {hit}   (4-token stored FEN, LIKE has 3 tokens + ' %')")

# ---------------------------------------------------------------- 6
hdr("6) evaluate_trainer_move(): LIMIT 1 with no ORDER BY over an OR/LIKE match")
c = get_db()
c.execute("DELETE FROM repertoire_moves")
c.commit(); c.close()
# position whose 4-token FEN ends in an ep square, plus a DIFFERENT position with the
# identical 3-token prefix (ep '-' vs ep 'e6')
b1 = chess.Board(); b1.push_uci("e2e4"); b1.push_uci("c7c5"); b1.push_uci("g1f3"); b1.push_uci("d7d6"); b1.push_uci("d2d4")
ep_fen = b1.fen()                      # 4th token = an ep square
b2 = chess.Board(); b2.push_uci("d2d4")
other_fen = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR w KQkq -"   # same 3-token prefix as ep_fen
print("  ep_fen   :", ep_fen)
print("  other_fen:", other_fen)
print("  3-token :", " ".join(ep_fen.split()[:3]))
rm.add_repertoire_move("white", ep_fen, "c5d4", is_user_move=True, comment="EP-position move")
c = get_db()
c.execute("""INSERT OR REPLACE INTO repertoire_moves
             (repertoire,parent_fen,fen,move_uci,move_san,is_user_move,comment)
             VALUES ('white',?,?,?,?,1,'unrelated d4 position')""",
          (normalize_fen(other_fen), normalize_fen(other_fen), "g8f6", "Nf6"))
c.commit(); c.close()
r = rm.evaluate_trainer_move("white", ep_fen, "c5d4")
print("  played the CORRECT ep-position move 'c5d4' ->", {k: r.get(k) for k in ("correct", "expected_san", "comment")})

print("\nDONE")
