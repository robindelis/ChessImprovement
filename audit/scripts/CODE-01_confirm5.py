"""
CODE-01 confirmation probe #5: does seed_default_repertoires(force=True) prune the user's
prepared 3.e5 Anti-French move (the exact scenario AGENTS.md 9.3 warns about)?
Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-01_confirm5.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST backend-adjacent import

import chess
from backend.repertoire import RepertoireManager
from backend.db import get_db

SEP = "=" * 78
rm = RepertoireManager.get_instance()
c = get_db()
c.execute("DELETE FROM repertoire_moves")
c.execute("DELETE FROM study_moves")
c.execute("DELETE FROM repertoire_studies")
c.commit(); c.close()

b = chess.Board()
line = [("e2e4", True), ("e7e6", False), ("d2d4", True), ("d7d5", False), ("e4e5", True), ("c7c5", False), ("g1f3", True)]
for uci, user in line:
    rm.add_repertoire_move("white", b.fen(), uci, is_user_move=user)
    b.push_uci(uci)
rm.update_move_metadata("white", chess.STARTING_FEN, "e2e4", comment="anti-french prep")

def show(tag):
    c = get_db()
    rows = c.execute("SELECT id,parent_fen,move_uci,move_san,is_user_move,comment FROM repertoire_moves ORDER BY id").fetchall()
    print(f"\n[{tag}] {len(rows)} rows")
    for r in rows:
        print("   ", r["id"], r["move_uci"], f"(u{r['is_user_move']})", "parent=", r["parent_fen"][:44], r["comment"])
    c.close()
    return rows

before = show("BEFORE force-seed (user's Anti-French: 1.e4 e6 2.d4 d5 3.e5 c5 4.f3)")

# the exact TOCTOU of the guard: count in one connection, close, then seed
c = get_db(); c.execute("SELECT COUNT(*) FROM repertoire_moves").fetchone(); c.close()
rm.seed_default_repertoires(force=True)
after = show("AFTER seed_default_repertoires(force=True)")

gone = [dict(r) for r in before if r["id"] not in {x["id"] for x in after}]
print("\nrows deleted by force=True:", [(r["move_uci"], r["comment"]) for r in gone])

# now the guard itself
print("\nseed_default_repertoires(force=False) ->", rm.seed_default_repertoires(force=False))
c = get_db()
print("row count unchanged:", c.execute("SELECT COUNT(*) FROM repertoire_moves").fetchone()[0])
c.close()
print("DONE")
