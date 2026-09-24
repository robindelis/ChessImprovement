"""
CODE-01 confirmation probe #4 (v3): the 3-token/LIKE fallback + `LIMIT 1` (no ORDER BY)
conflates two repertoire rows that differ only in the en-passant token.
Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-01_confirm4.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST backend-adjacent import

import chess
from backend.repertoire import RepertoireManager, normalize_fen
from backend.db import get_db

SEP = "=" * 78
rm = RepertoireManager.get_instance()
c = get_db()
c.execute("DELETE FROM repertoire_moves")
c.commit()

# Same piece placement / side to move / castling, differing only in the ep token.
#   P_EP   : 1.e4 e6 2.e5 d5   (genuine d7-d5 double push -> exd6 e.p. exists)
#   P_NOEP : 1.e4 d6 2.e5 Be6 3...d5  (d6-d5 single push -> no e.p.)
def play(us):
    b = chess.Board()
    for u in us:
        b.push_uci(u)
    return b
a = play(["e2e4", "e7e6", "e4e5", "d7d5"])
# identical pieces/turn/castling, ep token '-' (e.g. reached when the d-pawn arrives by d6-d5)
c_ = chess.Board(" ".join(a.fen().split()[:3] + ["-"] + a.fen().split()[4:]))
P_EP, P_NOEP = a.fen(), c_.fen()
print("P_EP   :", P_EP, "| valid", a.is_valid(), "| exd6 e.p. legal:",
      chess.Move.from_uci("e5d6") in a.legal_moves)
print("P_NOEP :", P_NOEP, "| valid", c_.is_valid(), "| exd6 e.p. legal:",
      chess.Move.from_uci("e5d6") in c_.legal_moves)
print("identical 3-token prefix:", a.fen().split()[:3] == c_.fen().split()[:3],
      a.fen().split()[:3])
print("normalize_fen() keeps the 4th token (test_repertoire.py::test_normalize_fen_four_tokens):",
      normalize_fen(P_EP) != normalize_fen(P_NOEP))

# row 1 (LOWER rowid): a prep that lives in the *no-e.p.* position
c.execute("""INSERT INTO repertoire_moves (repertoire,parent_fen,fen,move_uci,move_san,is_user_move,comment)
             VALUES ('white',?,?,?,?,1,'belongs to the OTHER position')""",
          (normalize_fen(P_NOEP), normalize_fen(P_NOEP), "b1c3", "Nc3"))
c.commit()
# row 2: the real prep for P_EP
print("\nadd g1f3 for P_EP ->", rm.add_repertoire_move("white", P_EP, "g1f3", is_user_move=True,
                                                     comment="correct prep")["status"])
c = get_db()
print("\nrows now in the white repertoire:")
for r in c.execute("SELECT id,parent_fen,move_uci,comment FROM repertoire_moves ORDER BY id"):
    print("  ", dict(r))

norm = normalize_fen(P_EP)
n3 = " ".join(norm.split()[:3])
print("\nEXACT SQL from repertoire.py:2514-2520 (evaluate_trainer_move 'prepared' lookup):")
rows = c.execute("""
    SELECT rm.id, rm.parent_fen, rm.move_uci, rm.move_san
    FROM repertoire_moves rm
    WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?)
      AND rm.is_user_move = 1
    LIMIT 1
    """, ("white", norm, n3, f"{n3} %")).fetchall()
print("  rows matched:", [dict(r) for r in rows], "  <-- LIMIT 1 silently picks the first")
n_all = c.execute("""SELECT COUNT(*) FROM repertoire_moves rm
    WHERE rm.repertoire='white' AND (rm.parent_fen=? OR rm.parent_fen=? OR rm.parent_fen LIKE ?) AND rm.is_user_move=1""",
    (norm, n3, f"{n3} %")).fetchone()[0]
print("  total rows the OR/LIKE predicate actually matches:", n_all)
print("\nEXACT SQL from repertoire.py:479-482 (the 'ONE user move per position' guard):")
g = c.execute("""SELECT fen, move_uci FROM repertoire_moves
                 WHERE repertoire=? AND parent_fen=? AND is_user_move=1""",
              ("white", norm)).fetchall()
print("  rows matched:", [dict(r) for r in g],
      " <-- 4-token equality only, so the second user move for the same position was NOT pruned")
c.close()
print("DONE")
