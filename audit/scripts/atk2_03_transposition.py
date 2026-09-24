"""TRAINER probe 03 -- transpositions break the "one user move per position" invariant
and produce a drill that can never be graded correct.

Root cause: add_repertoire_move() enforces the single-user-move rule with an EXACT
4-token parent_fen match, but normalize_fen() keeps the en-passant token, so two move
orders that transpose into the same position store *textually different* parent_fen
strings.  Every trainer lookup identifies positions by the 3-token prefix
(placement+turn+castling) -> two different prepared user moves end up at one position
and evaluate_trainer_move() grades with `... LIMIT 1` (no ORDER BY).

Run: /home/robin/miniconda3/bin/python3 audit/scripts/atk2_03_transposition.py
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk2_fixture import *  # noqa

c = fresh()
hdr("BUILD two transposing white studies that reach the SAME position")

# P : 1.d4 d5 2.e4 a6 3.e5 Nf6   -> parent "… w KQkq -"   (last move was ...Nf6)
# Q : 1.d4 Nf6 2.e4 a6 3.e5 d5   -> parent "… w KQkq d6"  (e5xd6 e.p. is legal)
P = ["d4", "d5", "e4", "a6", "e5", "Nf6"]
Q = ["d4", "Nf6", "e4", "a6", "e5", "d5"]
fp, fq = fens(P), fens(Q)
print("P parent of 4th move:", fp[6])
print("Q parent of 4th move:", fq[6])
print("same 3-token prefix  :", " ".join(fp[6].split()[:3]) == " ".join(fq[6].split()[:3]))

add_line(c, "white", "StudyP", P + ["Nf3"], popularity=1.0)   # low popularity
add_line(c, "white", "StudyQ", Q + ["Bb5"], popularity=90.0)  # high popularity -> drill picks this

conn = get_db()
rows = [dict(r) for r in conn.execute(
    """SELECT id, parent_fen, move_uci, move_san, popularity_pct, study_name
       FROM repertoire_moves WHERE is_user_move=1 ORDER BY id""")]
conn.close()
target3 = " ".join(fp[6].split()[:3])
hdr("RESULT: prepared user moves stored at ONE logical position")
for r in rows:
    if " ".join(r["parent_fen"].split()[:3]) == target3:
        print("  id=%-3s %-5s pop=%-5s study=%-7s parent_fen=%s"
              % (r["id"], r["move_san"], r["popularity_pct"], r["study_name"], r["parent_fen"]))

hdr("CONSEQUENCE 1: the grader always resolves to the FIRST row (LIMIT 1)")
d = c.post("/api/repertoire/train/attempt", json={
    "repertoire": "white", "current_fen": fq[6], "attempted_uci": "f1b5"}).json()
print("  client is StudyQ, plays its prepared move f1b5 -> correct=%s" % d.get("correct"))
print("  message: %r" % d.get("message"))
r_bb5, r_nf3 = row("f1b5"), row("g1f3")
print("  counters: Bb5 row tp=%s tc=%s | Nf3 row tp=%s tc=%s  (user never played Nf3)"
      % (r_bb5["times_practiced"], r_bb5["times_correct"],
         r_nf3["times_practiced"], r_nf3["times_correct"]))
d = c.post("/api/repertoire/train/attempt", json={
    "repertoire": "white", "current_fen": fp[6], "attempted_uci": "g1f3"}).json()
print("  client is StudyP, plays g1f3 -> correct=%s" % d.get("correct"))

hdr("CONSEQUENCE 2: train/next serves Bb5, but Bb5 can never be graded correct")
# fixture prep: make the StudyQ ancestors look learned so the transposed node is a frontier
conn = get_db()
conn.execute("UPDATE repertoire_moves SET times_practiced=2, times_correct=2, next_review=? "
             "WHERE is_user_move=1 AND id NOT IN (7,14)", (int(time.time()) + 86400 * 30,))
conn.commit(); conn.close()
drills = {}
for _ in range(8):
    d = c.get("/api/repertoire/train/next?color=white&mode=frontier_only").json()
    if d.get("parent_fen") and " ".join(d["parent_fen"].split()[:3]) == target3:
        drills[d["expected_san"]] = drills.get(d["expected_san"], 0) + 1
print("  drills served at the transposed position:", drills)
for i in range(3):
    d = c.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": fq[6], "attempted_uci": "f1b5"}).json()
    print("  replay %d: user plays the drilled move f1b5 -> correct=%s (%s)"
          % (i + 1, d.get("correct"), d.get("message")))
print("  Bb5 ends at times_correct=%s / times_practiced=%s -> permanently 'unlearned'"
      % (row("f1b5")["times_correct"], row("f1b5")["times_practiced"]))

hdr("CONSEQUENCE 3: progress counts the same position twice")
pr = c.get("/api/repertoire/train/progress?color=white").json()
print(" ", {k: pr[k] for k in ("total_moves", "mastered_count", "learning_count", "unlearned_count")})
print("  (StudyP has 4 user moves + StudyQ has 4 user moves, but only 7 distinct positions)")
for s in pr["studies"]:
    print("   ", s)
