"""TRAINER probe 01 -- drill selection tie-breaks + scheduling integrity.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/atk2_01_selection_scheduling.py
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk2_fixture import *  # noqa

c = fresh()
hdr("FIXTURE: white study 'S'  1.e4 e5 2.Nf3 Nc6 3.Bb5 (3 user moves)")

# two opponent replies after 1.e4 so we get two depth-3 user moves
add_line(c, "white", "S", ["e4", "e5", "Nf3"])
add_line(c, "white", "S", ["e4", "c5", "Nf3", "d6"], popularity=2.0)
c.post("/api/repertoire/move_meta", json={
    "repertoire": "white", "parent_fen": fens(["e4", "c5"])[2],
    "move_uci": "g1f3", "is_important": True})
c.post("/api/repertoire/move_meta", json={
    "repertoire": "white", "parent_fen": fens(["e4"])[1],
    "move_uci": "g1f3", "is_important": False})
print("Nf3 rows:")
for r in c.get("/api/repertoire/tree?color=white").json().get("moves", []):
    pass
conn = get_db()
for r in conn.execute("SELECT id,parent_fen,move_uci,is_important,study_name FROM repertoire_moves WHERE move_uci='g1f3' ORDER BY id"):
    print("   ", dict(r))
conn.close()

hdr("A) TIER 3 frontier: is_important tie-break at identical ply_depth")
picks = []
for _ in range(12):
    d = c.get("/api/repertoire/train/next?color=white&mode=frontier_only").json()
    picks.append((d["parent_fen"].split()[0][:20], d["expected_san"], d["ply_depth"]))
for p in picks:
    print("   frontier pick:", p)

hdr("B) TIER 2 due_review: is_important tie-break at identical urgency")
now = int(time.time())
conn = get_db()
conn.execute("""UPDATE repertoire_moves SET times_practiced=1, times_correct=1,
                interval_days=1.0, next_review=? WHERE move_uci='g1f3'""", (now - 86400,))
conn.commit(); conn.close()
seen = {}
for _ in range(12):
    d = c.get("/api/repertoire/train/next?color=white&mode=due_only").json()
    key = d["parent_fen"]
    seen[key] = seen.get(key, 0) + 1
    print("   due pick: imp=%s san=%s reason=%s" % (d["is_important"], d["expected_san"], d["drill_reason"]))
print("distinct positions drilled:", len(seen), "->", list(seen.values()))

hdr("C) SCHEDULING: hammer /attempt on one card and watch the counters")
c2 = fresh()
add_line(c2, "white", "Solo", ["e4"])
seen_rows = []
for i in range(6):
    r = c2.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"}).json()
    rowd = row("e2e4")
    print("   attempt %d -> correct=%s tp=%s tc=%s interval=%s next_review-now=%s  mastery=%s"
          % (i + 1, r["correct"], rowd["times_practiced"], rowd["times_correct"],
             rowd["interval_days"], rowd["next_review"] - int(time.time()), r["mastery_status"]))
    seen_rows.append((rowd["times_practiced"], rowd["times_correct"]))
print("   monotonic non-decreasing tp:", all(seen_rows[i][0] <= seen_rows[i+1][0] for i in range(len(seen_rows)-1)))

hdr("D) SCHEDULING: wrong answer demotion")
for i in range(3):
    c2.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": "d2d4"})
    rowd = row("e2e4")
    print("   wrong #%d -> tp=%s tc=%s interval=%s next_review-now=%s"
          % (i + 1, rowd["times_practiced"], rowd["times_correct"],
             rowd["interval_days"], rowd["next_review"] - int(time.time())))
r = c2.post("/api/repertoire/train/attempt", json={
    "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"}).json()
rowd = row("e2e4")
print("   correct after 3 wrongs -> tp=%s tc=%s interval=%s mastery=%s"
      % (rowd["times_practiced"], rowd["times_correct"], rowd["interval_days"], r["mastery_status"]))
print("   drill payload for this card:", c2.get("/api/repertoire/train/progress?color=white").json())
