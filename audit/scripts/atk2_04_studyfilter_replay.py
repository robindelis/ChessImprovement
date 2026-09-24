"""TRAINER probe 04 -- study filter is best-effort only, fuzzy + LIKE-wildcard injectable,
and /train/attempt is not bound to the drill that was served (replayable).

Run: /home/robin/miniconda3/bin/python3 audit/scripts/atk2_04_studyfilter_replay.py
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk2_fixture import *  # noqa

c = fresh()
hdr("FIXTURE:  Alpha = {1.e4}   (single move study)   Beta = 1.e4 e5 2.Nf3 Nc6 3.Bb5")
add_line(c, "white", "Alpha", ["e4"])
add_line(c, "white", "Beta", ["e4", "e5", "Nf3", "Nc6", "Bb5"])
print("  studies:", [s["name"] for s in c.get("/api/repertoire/studies?repertoire=white").json()])

hdr("A) drill filtered to Alpha, answer it -> trainer continues into BETA's line")
d = c.get("/api/repertoire/train/next?color=white&opening=Alpha").json()
print("  drill: id=%s san=%s study=%s opening=%s" % (d["id"], d["expected_san"], d["study_name"], d["opening_name"]))
r = c.post("/api/repertoire/train/attempt", json={
    "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4",
    "study_name": "Alpha", "opening_filter": "Alpha"}).json()
print("  attempt(study=Alpha) -> study_name=%s" % r["study_name"])
print("     opponent_reply   = %s   <-- belongs to Beta, not Alpha" % (r["opponent_reply"] or {}).get("san"))
print("     next_expected    = %s (%s)  is_line_end=%s"
      % (r["next_expected_uci"], r["next_expected_san"], r["is_line_end"]))
print("     next_study_name  = %r   next_opening_name = %r  <-- mutually inconsistent labels"
      % (r["next_study_name"], r["next_opening_name"]))
print("  progress for Alpha:", {k: v for k, v in
      c.get("/api/repertoire/train/progress?color=white&study=Alpha").json().items()
      if k in ("total_moves", "mastered_count", "learning_count", "unlearned_count")})
print("  progress for Beta :", {k: v for k, v in
      c.get("/api/repertoire/train/progress?color=white&study=Beta").json().items()
      if k in ("total_moves", "mastered_count", "learning_count", "unlearned_count")})

hdr("B) 'Caro-Kann' and 'Caro-Kann Advance' -- substring filter leaks between studies")
c2 = fresh()
add_line(c2, "white", "Caro-Kann", ["e4", "c6", "d4", "d5", "f3"], popularity=1.0)
add_line(c2, "white", "Caro-Kann Advance", ["e4", "c6", "d4", "d5", "Nd2"], popularity=90.0)
conn = get_db()
conn.execute("UPDATE repertoire_moves SET times_practiced=2, times_correct=2 "
             "WHERE move_uci='e2e4'")
conn.execute("UPDATE repertoire_moves SET times_practiced=2, times_correct=2 "
             "WHERE move_uci='d2d4'")
conn.commit(); conn.close()
seen = set()
for _ in range(6):
    d = c2.get("/api/repertoire/train/next?color=white&opening=Caro-Kann&mode=frontier_only").json()
    seen.add((d["expected_san"], d["study_name"]))
print("  opening=Caro-Kann returned:", seen, " <-- 3.Nd2 only exists in 'Advance'")
pr = c2.get("/api/repertoire/train/progress?color=white&study=Caro-Kann").json()
print("  progress?study=Caro-Kann -> filter_study=%r total_moves=%d" % (pr["filter_study"], pr["total_moves"]))
for s in pr["studies"]:
    print("     ", s)
d = c2.get("/api/repertoire/train/next?color=white&opening=Caro-Kann Advance&mode=frontier_only").json()
print("  opening='Caro-Kann Advance' -> drill san=%s study=%r" % (d["expected_san"], d["study_name"]))

hdr("C) LIKE wildcard injection through the study filter")
for filt in ("%", "_", "Caro%"):
    d = c2.get("/api/repertoire/train/next", params={"color": "white", "opening": filt,
                                                    "mode": "frontier_only"}).json()
    print("  opening=%-6r -> HTTP ok, drill san=%s study=%r" % (filt, d.get("expected_san"), d.get("study_name")))
d = c2.post("/api/repertoire/train/attempt", json={
    "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4",
    "study_name": "%"}).json()
print("  attempt with study_name='%%' -> study_name=%r reply=%s" % (r["study_name"] if False else d.get("study_name"),
                                                                    (d.get("opponent_reply") or {}).get("san")))

hdr("D) /train/attempt is not bound to the served drill -> replay / forgery")
c3 = fresh()
add_line(c3, "white", "Solo", ["e4", "e5", "Nf3", "Nc6", "Bb5"])
conn = get_db()
ids = {r["move_san"]: r["id"] for r in conn.execute(
    "SELECT id, move_san FROM repertoire_moves WHERE is_user_move=1")}
conn.close()
print("  user moves:", ids)
print("\n  d1) never served by /train/next at all - direct attempts on the deepest position:")
deep = fens(["e4", "e5", "Nf3", "Nc6"])[4]
for i in range(3):
    d = c3.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": deep, "attempted_uci": "f1b5"}).json()
    print("     attempt %d -> correct=%s tp=%s tc=%s" % (i + 1, d["correct"],
          row("f1b5")["times_practiced"], row("f1b5")["times_correct"]))
print("     (prerequisites 1.e4/2.Nf3 were never learned - the drill is still credited)")

print("\n  d2) the SAME answer POSTed 5 times (double-click / retry storm):")
for i in range(5):
    c3.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"})
print("     1.e4 times_practiced=%s times_correct=%s interval_days=%s"
      % (row("e2e4")["times_practiced"], row("e2e4")["times_correct"], row("e2e4")["interval_days"]))

print("\n  d3) stale FEN (same position, older move counters / other ep token):")
stale = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1".replace(" w KQkq -", " w KQkq e3")
d = c3.post("/api/repertoire/train/attempt", json={
    "repertoire": "white", "current_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 99 250",
    "attempted_uci": "e2e4"}).json()
print("     halfmove=99 fullmove=250 -> correct=%s (accepted, tp=%s)"
      % (d["correct"], row("e2e4")["times_practiced"]))

print("\n  d4) study_name that does not exist / belongs to the other colour:")
for sn in ("Does Not Exist", "Alpha", "Sicilian Dragon"):
    d = c3.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4", "study_name": sn}).json()
    print("     study_name=%-16r -> correct=%s study_name=%r" % (sn, d["correct"], d.get("study_name")))
