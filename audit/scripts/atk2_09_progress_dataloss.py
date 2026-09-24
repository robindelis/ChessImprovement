"""TRAINER probe 09 -- data loss: re-adding an existing repertoire move wipes its
spaced-repetition progress; plus FEN reconstruction and progress-percentage rounding.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/atk2_09_progress_dataloss.py
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk2_fixture import *  # noqa

c = fresh()
hdr("A) practice a card to 'mastered', then re-add the SAME move (scout re-import /")
hdr("   builder re-save / import_game_opening all funnel into add_repertoire_move)")
add_line(c, "white", "Anti French", ["e4", "e6", "d4", "d5", "e5"])
for i in range(3):
    c.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"})
r = row("e2e4")
print("   after 3 correct: tp=%s tc=%s interval=%s next_review=%s" %
      (r["times_practiced"], r["times_correct"], r["interval_days"],
       r["next_review"] - int(time.time())))
print("   progress:", {k: v for k, v in c.get(
    "/api/repertoire/train/progress?color=white").json().items() if k.endswith("count")})

print("\n   POST /api/repertoire/move with the identical (repertoire, parent_fen, move_uci):")
resp = c.post("/api/repertoire/move", json={
    "repertoire": "white", "parent_fen": START, "move_uci": "e2e4",
    "is_user_move": True, "study_name": "Anti French"})
print("   ->", resp.status_code, resp.json().get("status"))
r2 = row("e2e4")
print("   after re-add : tp=%s tc=%s interval=%s next_review=%s" %
      (r2["times_practiced"], r2["times_correct"], r2["interval_days"], r2["next_review"]))
print("   progress:", {k: v for k, v in c.get(
    "/api/repertoire/train/progress?color=white").json().items() if k.endswith("count")})
print("   drill for 1.e4 is now:", c.get(
    "/api/repertoire/train/next?color=white&mode=frontier_only").json()["drill_reason"])

print("\n   same via POST /api/repertoire/move_meta (comment/arrows/study only):")
c.post("/api/repertoire/move_meta", json={"repertoire": "white", "parent_fen": START,
                                          "move_uci": "e2e4", "comment": "note"})
print("   tp=%s (untouched by move_meta)" % row("e2e4")["times_practiced"])

hdr("B) drill parent_fen is rebuilt with a hard-coded '0 1' clock")
c2 = fresh()
add_line(c2, "white", "Deep", ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7"])
conn = get_db()   # mark the ancestors learned so the ply-9 castling move is on the frontier
conn.execute("UPDATE repertoire_moves SET times_practiced=2, times_correct=2, next_review=? "
             "WHERE is_user_move=1 AND move_uci <> 'e1g1'", (int(time.time()) + 99999,))
conn.commit()
deep_parent = conn.execute(
    "SELECT parent_fen FROM repertoire_moves WHERE move_uci='e1g1'").fetchone()[0]
conn.close()
d = c2.get("/api/repertoire/train/next?color=white&mode=frontier_only").json()
print("   ply_depth=%s  drill_reason=%s  expected=%s" % (d["ply_depth"], d["drill_reason"], d["expected_san"]))
print("   stored parent_fen  :", deep_parent)
print("   drill parent_fen   :", d["parent_fen"])
print("   -> halfmove clock forced to 0 and fullmove number forced to 1")
print("   path_formatted=%r" % d["path_formatted"])

hdr("C) progress percentages: rounding makes the three segments disagree with the counts")
c3 = fresh()
add_line(c3, "white", "S", ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7"])
add_line(c3, "white", "S", ["e4", "c5", "Nf3", "d6", "d4"])
conn = get_db()
ids = [r["id"] for r in conn.execute("SELECT id FROM repertoire_moves WHERE is_user_move=1 ORDER BY id")]
now = int(time.time())
for i, mid in enumerate(ids):
    if i < 2:      # mastered
        conn.execute("UPDATE repertoire_moves SET times_practiced=2, times_correct=2, next_review=? WHERE id=?", (now + 99999, mid))
    elif i < 5:    # learning
        conn.execute("UPDATE repertoire_moves SET times_practiced=3, times_correct=2, next_review=? WHERE id=?", (now + 99999, mid))
    # else unlearned
conn.commit(); conn.close()
pr = c3.get("/api/repertoire/train/progress?color=white").json()
print("   total=%s mastered=%s learning=%s unlearned=%s" %
      (pr["total_moves"], pr["mastered_count"], pr["learning_count"], pr["unlearned_count"]))
print("   pcts: mastered=%s learning=%s unlearned=%s  (sum=%s)" %
      (pr["mastered_pct"], pr["learning_pct"], pr["unlearned_pct"],
       round(pr["mastered_pct"] + pr["learning_pct"] + pr["unlearned_pct"], 4)))
print("   true value for unlearned 2/7 =", round(2 / 7 * 100, 1))
print("   -> the three progress bars the UI renders do not match the legend counts")

hdr("D) /train/openings move_count for a study that only contains opponent moves")
c4 = fresh()
b = chess.Board(START)
add(c4, "white", b.fen(), "e2e4", is_user=True, study="WhiteOnly")
b.push_san("e4")
add(c4, "white", b.fen(), "e7e5", is_user=False, study="WhiteOnly")
print("   ", c4.get("/api/repertoire/train/openings?color=white").json()["openings"])
print("   (a study whose only tree node is an opponent move still reports a move count)")
