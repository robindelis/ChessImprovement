"""TRAINER probe 05 -- answer normalisation, opponent-branch coverage, is_line_end
consistency, and the pre-attempt hint payload.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/atk2_05_answer_lineend_hint.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk2_fixture import *  # noqa
import chess

c = fresh()
hdr("FIXTURE: 1.e4 with TWO opponent replies: 1...e5 (has 2.Nf3 follow-up, pop=1)")
hdr("                                1...c5 (DEAD END leaf,        pop=100)")
add_line(c, "white", "T", ["e4", "e5", "Nf3", "Nc6"], popularity=1.0)
add_line(c, "white", "T", ["e4", "c5"], popularity=100.0)

hdr("A) 20 correct answers -- which opponent reply is ever played?")
replies = {}
for _ in range(20):
    r = c.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"}).json()
    replies[(r["opponent_reply"] or {}).get("san")] = replies.get((r["opponent_reply"] or {}).get("san"), 0) + 1
    # consistency of the advertised continuation
    assert r["is_line_end"] is False
print("   opponent replies played:", replies)
print("   -> 1...c5 (pop=100, the line the repertoire actually prepares for) NEVER played")

hdr("B) line-end consistency: is_line_end vs next_expected_* vs opponent fen")
r = c.post("/api/repertoire/train/attempt", json={
    "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"}).json()
b = chess.Board(r["after_fen"])
opp = chess.Board(r["opponent_reply"]["fen"])
m = chess.Move.from_uci(r["opponent_reply"]["uci"])
print("   after_fen legal, opp reply legal in it :", m in b.legal_moves)
nxt = chess.Move.from_uci(r["next_expected_uci"])
print("   next expected legal in opponent fen    :", nxt in opp.legal_moves)
print("   is_line_end                            :", r["is_line_end"])

hdr("C) answer normalisation / injection")
c2 = fresh()
add_line(c2, "white", "P", ["e4", "e5"])
cases = ["e2e4", "E2E4", "  e2e4  ", "e2e4\n", "e2e4\t", "e2e4q", "e2e4Q", "e2e4xyz",
         "e2e", "e2 e4", "0000", "e2e4e4", "z9z9", "", "e2e4; DROP TABLE x"]
for u in cases:
    r = c2.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": u}).json()
    print("   %-18r -> correct=%-5s %s" % (u, r.get("correct"), r.get("message", "")[:60]))

hdr("C2) illegal-but-well-formed and legal-but-wrong")
for u in ["d2d4", "e7e5", "e1g1"]:
    r = c2.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": START, "attempted_uci": u}).json()
    print("   %-6s -> correct=%-5s %s" % (u, r.get("correct"), r.get("message", "")[:70]))

hdr("D) promotion position: does the 5-char sanitizer mis-grade promotions?")
c3 = fresh()
pf = "8/P6k/8/8/8/8/8/K7 w - - 0 1"
add(c3, "white", pf, "a7a8q", is_user=True, study="Promo")
for u in ["a7a8q", "a7a8", "a7a8Q", "A7A8Q", "a7a8r", "a7a8n", "a7a8b", "a7a8qq", "a7a8q5"]:
    r = c3.post("/api/repertoire/train/attempt", json={
        "repertoire": "white", "current_fen": pf, "attempted_uci": u}).json()
    print("   %-8s -> correct=%-5s %s" % (u, r.get("correct"), r.get("message", "")[:64]))

hdr("E) hint / answer payload returned by /train/next BEFORE any attempt")
c4 = fresh()
f = fens(["e4"])[0]
add(c4, "white", START, "e2e4", is_user=True, study="H",
    is_important=True, comment="king pawn punch", arrows=json.dumps([{"from": "e2", "to": "e4", "color": "#22c55e"}]))
d = c4.get("/api/repertoire/train/next?color=white").json()
print(json.dumps({k: d[k] for k in ("id", "expected_uci", "expected_san", "comment",
                                    "arrows", "is_important", "times_practiced",
                                    "times_correct", "path_formatted", "drill_reason")}, indent=2))
print("   arrows describe the answer move itself:",
      json.loads(d["arrows"])[0]["from"] + json.loads(d["arrows"])[0]["to"] == d["expected_uci"])
print("   frontend renders these arrows immediately (repertoire_trainer.js:504-506) and")
print("   the Hint button draws the same from->to (repertoire_trainer.js:598-602).")
