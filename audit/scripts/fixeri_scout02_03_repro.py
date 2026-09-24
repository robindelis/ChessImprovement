"""FIXER-I — evidence for the SCOUT-02 / SCOUT-03 hand-off (no code change here).

    PYTHONPATH=. /home/robin/miniconda3/bin/python3 audit/scripts/fixeri_scout02_03_repro.py

Read-only w.r.t. backend code: this only *demonstrates* that the destructive
behaviour still lives in backend/repertoire.py (FIXER-A's file), so the
hand-off in audit/patches/FIXER-I-01.md §6 is precise.
"""
import tests.test_setup  # FIRST  (BRIEF hard rule 2)

import chess
from backend.db import get_db
from backend.repertoire import RepertoireManager, normalize_fen

rm = RepertoireManager.get_instance()


def moves_for(sans):
    board = chess.Board()
    out = []
    for san in sans:
        out.append({"fen_before": normalize_fen(board.fen()), "san": san,
                    "uci": board.parse_san(san).uci()})
        board.push_san(san)
    return out


def dump(label):
    db = get_db()
    rm_rows = [(r["parent_fen"], r["move_uci"], r["study_name"])
               for r in db.execute("SELECT parent_fen, move_uci, study_name FROM repertoire_moves ORDER BY id")]
    sm_rows = [(r["study_id"], r["parent_fen"], r["move_uci"])
               for r in db.execute("SELECT study_id, parent_fen, move_uci FROM study_moves ORDER BY study_id")]
    studies = [(r["id"], r["name"]) for r in db.execute("SELECT id, name FROM repertoire_studies")]
    print(f"  [{label}]")
    print(f"    repertoire_moves ({len(rm_rows)}): " + ", ".join(f"{u}{'@' + s if s else ''}" for _, u, s in rm_rows))
    print(f"    study_moves      ({len(sm_rows)}): " + ", ".join(f"study{u.split('@')[0] if False else si}:{u}" for si, _, u in sm_rows))
    print(f"    repertoire_studies: {studies}")
    # Dangling study_moves = a link whose move is no longer in repertoire_moves.
    rm_keys = {(p, u) for p, u, _ in rm_rows}
    dangling = [(si, p, u) for si, p, u in sm_rows if (p, u) not in rm_keys]
    if dangling:
        print(f"    !! DANGLING study_moves rows: {dangling}")
    empty = [sid for sid, name in studies
             if not any(si == sid for si, _, _ in sm_rows)]
    if empty:
        print(f"    !! EMPTY repertoire_studies rows: {empty}")
    return dangling, empty


db = get_db()
for t in ("repertoire_moves", "study_moves", "repertoire_studies"):
    db.execute(f"DELETE FROM {t}")
db.commit()

print("SCOUT-02: a divergent import deletes the prepared user move and its subtree")
line = moves_for(["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "d3"])
for idx, m in enumerate(line):
    rm.add_repertoire_move("white", m["fen_before"], m["uci"],
                           is_user_move=(idx % 2 == 0), study_name="My Italian")
dump("after preparing 1.e4 e5 2.Nf3 Nc6 3.Bc4 Bc5 4.d3 under 'My Italian'")

print()
print("  now import a scouted line that plays 1.d4 instead of the prepared 1.e4 ...")
db.execute("DELETE FROM repertoire_moves WHERE repertoire='white' AND move_uci='d2d4'")
db.commit()
rm.add_repertoire_move("white", normalize_fen(chess.STARTING_FEN), "d2d4", is_user_move=True, study_name="Scouted")
after = dump("after the conflicting import")
db.execute("DELETE FROM repertoire_moves WHERE repertoire='white' AND move_uci='d2d4'")
db.commit()

print()
print("SCOUT-03: an overlapping import hijacks study_name")
db = get_db()
for t in ("repertoire_moves", "study_moves", "repertoire_studies"):
    db.execute(f"DELETE FROM {t}")
db.commit()
m0 = moves_for(["e4"])[0]
rm.add_repertoire_move("white", m0["fen_before"], m0["uci"], study_name="Study A")
dump("after filing 1.e4 under 'Study A'")
rm.add_repertoire_move("white", m0["fen_before"], m0["uci"], study_name="Study B")
dump("after re-importing the SAME move with study_name='Study B'")
