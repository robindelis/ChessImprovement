"""TRAINER probe 02 -- in-session lapse starvation, is_important frontier tie-break,
edge repertoires (empty / single move / no opponent reply / colour mismatch).

Run: /home/robin/miniconda3/bin/python3 audit/scripts/atk2_02_selection_edges.py
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk2_fixture import *  # noqa

# ---------------------------------------------------------------- 1. Tier 1
c = fresh()
hdr("1) TIER 1 in-session lapse re-test -- how many mistakes before it dies?")
add_line(c, "white", "S", ["e4", "e5", "Nf3"])
add_line(c, "white", "S", ["e4", "c5", "Nf3", "d6"])
conn = get_db()
ids = {r["move_san"]: r["id"] for r in conn.execute(
    "SELECT id, move_san FROM repertoire_moves WHERE is_user_move=1")}
conn.close()
print("   user move ids:", ids)

e4, nf3_e5, nf3_c5 = ids["e4"], ids["Nf3"], ids["Nf3"]
# make them 'due' so tiers 2-4 are not empty confounders
now = int(time.time())
conn = get_db()
conn.execute("UPDATE repertoire_moves SET times_practiced=1, times_correct=1, next_review=?",
             (now + 86400 * 30,))   # NOT due
conn.commit(); conn.close()

print("\n   a) ONE lapse, and it is also in recent_ids (the normal frontend state):")
d = c.get(f"/api/repertoire/train/next?color=white&lapses={e4}&recent_ids={e4}").json()
print("      -> reason=%s id=%s san=%s" % (d["drill_reason"], d["id"], d["expected_san"]))

print("\n   b) TWO lapses, both also in recent_ids (after 2 mistakes in one session):")
for _ in range(2):
    d = c.get(f"/api/repertoire/train/next?color=white&lapses={e4},{nf3_e5}&recent_ids={e4},{nf3_e5}").json()
    print("      -> reason=%s id=%s san=%s ply=%s" % (d["drill_reason"], d["id"], d["expected_san"], d["ply_depth"]))
print("      (the two just-missed moves are NEVER re-tested: reason is not session_lapse)")

print("\n   c) SAME two lapses but recent_ids NOT sent:")
d = c.get(f"/api/repertoire/train/next?color=white&lapses={e4},{nf3_e5}").json()
print("      -> reason=%s id=%s" % (d["drill_reason"], d["id"]))

print("\n   d) THREE lapses, one of them not yet drilled:")
d = c.get(f"/api/repertoire/train/next?color=white&lapses={e4},{nf3_e5}&recent_ids={e4}").json()
print("      -> reason=%s id=%s" % (d["drill_reason"], d["id"]))

print("\n   e) bogus / foreign ids in `lapses` are silently dropped:")
d = c.get("/api/repertoire/train/next?color=white&lapses=999999,abc,-5").json()
print("      -> reason=%s id=%s" % (d["drill_reason"], d["id"]))

# ------------------------------------------------------- 2. frontier is_important
hdr("2) TIER 3 frontier -- is_important tie-break at identical ply_depth")
c2 = fresh()
add_line(c2, "white", "S", ["e4", "e5", "Nf3"])                  # depth3 Nf3 (not important)
add_line(c2, "white", "S", ["e4", "c5", "Nf3", "d6"], popularity=5.0)  # depth3 Nf3 (important)
r = c2.post("/api/repertoire/move_meta", json={
    "repertoire": "white", "parent_fen": fens(["e4", "c5"])[2],
    "move_uci": "g1f3", "is_important": True})
print("   move_meta is_important ->", r.status_code, r.json().get("is_important"))
# mark 1.e4 learned so the depth-3 nodes become the frontier
c2.post("/api/repertoire/train/attempt", json={"repertoire": "white",
                                               "current_fen": START, "attempted_uci": "e2e4"})
conn = get_db()
rows = [dict(r) for r in conn.execute(
    "SELECT id, parent_fen, move_uci, is_important FROM repertoire_moves WHERE move_uci='g1f3'")]
conn.close()
for r in rows:
    print("   g1f3 id=%s important=%s parent=%s" % (r["id"], r["is_important"], r["parent_fen"][:34]))
imp_id = [r["id"] for r in rows if r["is_important"]][0]
plain_id = [r["id"] for r in rows if not r["is_important"]][0]
picked = {}
for _ in range(20):
    d = c2.get("/api/repertoire/train/next?color=white&mode=frontier_only").json()
    picked[d["id"]] = picked.get(d["id"], 0) + 1
print("   important id =", imp_id, " plain id =", plain_id)
print("   picks over 20 frontier_only calls:", picked)
print("   -> important move selected %d/20 times" % picked.get(imp_id, 0))

hdr("2b) TIER 1 starvation with DEEP lapses (maintenance would pick ply 1 instead)")
c2b = fresh()
add_line(c2b, "white", "S", ["e4", "e5", "Nf3"])
add_line(c2b, "white", "S", ["e4", "c5", "Nf3", "d6"])
conn = get_db()
rows = [dict(r) for r in conn.execute(
    "SELECT id, move_uci, parent_fen FROM repertoire_moves WHERE is_user_move=1 ORDER BY id")]
conn.close()
e4 = [r["id"] for r in rows if r["move_uci"] == "e2e4"][0]
nf3s = [r["id"] for r in rows if r["move_uci"] == "g1f3"]
print("   e4 id=%s  Nf3 ids=%s" % (e4, nf3s))
d = c2b.get(f"/api/repertoire/train/next?color=white&lapses={nf3s[0]},{nf3s[1]}&recent_ids={nf3s[0]},{nf3s[1]}").json()
print("   2 deep lapses already in recent_ids -> reason=%s id=%s san=%s ply=%s  (lapses were %s)"
      % (d["drill_reason"], d["id"], d["expected_san"], d["ply_depth"], nf3s))
d = c2b.get(f"/api/repertoire/train/next?color=white&lapses={nf3s[0]},{nf3s[1]}").json()
print("   same but recent_ids omitted        -> reason=%s id=%s san=%s ply=%s"
      % (d["drill_reason"], d["id"], d["expected_san"], d["ply_depth"]))

# ---------------------------------------------------------- 3. edge repertoires
hdr("3) EDGE REPERTOIRES")
c3 = fresh()
r = c3.get("/api/repertoire/train/next?color=white")
print("   empty white repertoire      -> HTTP %s %s" % (r.status_code, r.text[:90]))
print("   progress on empty           ->", c3.get("/api/repertoire/train/progress?color=white").json())
print("   openings on empty           ->", c3.get("/api/repertoire/train/openings?color=white").json())

add_line(c3, "white", "Only", ["e4"])   # user move with NO opponent reply at all
r = c3.get("/api/repertoire/train/next?color=white")
print("\n   single-move repertoire      -> HTTP %s san=%s is_line_end_after=%s"
      % (r.status_code, r.json()["expected_san"], c3.post("/api/repertoire/train/attempt", json={
          "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"}).json()["is_line_end"]))
print("   mode=due_only, nothing due  ->", c3.get("/api/repertoire/train/next?color=white&mode=due_only").json())
print("   mode=frontier_only, learned ->", c3.get("/api/repertoire/train/next?color=white&mode=frontier_only").json())

print("\n   garbage colour to /train/next   ->", c3.get("/api/repertoire/train/next?color=purple").status_code)
print("   garbage colour to /train/attempt ->",
      c3.post("/api/repertoire/train/attempt", json={"repertoire": "purple",
                                                     "current_fen": START, "attempted_uci": "e2e4"}).status_code)
print("   black drill while only white moves exist ->",
      c3.get("/api/repertoire/train/next?color=black").status_code,
      c3.get("/api/repertoire/train/next?color=black").text[:80])
