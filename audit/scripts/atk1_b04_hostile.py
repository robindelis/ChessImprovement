"""BUILDER-04 probe: hostile / malformed input sweep across the Repertoire Builder endpoints."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

wipe()
add_line("white", ["e4", "e5", "Nf3"], study_name="AF")
STUDY = one("SELECT id FROM repertoire_studies")["id"]
GOOD = fen_after(["e4", "e5"])

def show(label, r, limit=220):
    body = r.text
    if len(body) > limit:
        body = body[:limit] + "..."
    print(f"  [{label}] -> {r.status_code} {body}")


print("=" * 78)
print("A. /api/repertoire/move  (POST)")
print("=" * 78)
cases = [
    ("repertoire='WHITE'", {"repertoire": "WHITE", "parent_fen": GOOD, "move_uci": "d2d4"}),
    ("repertoire='both'", {"repertoire": "both", "parent_fen": GOOD, "move_uci": "d2d4"}),
    ("repertoire=null", {"repertoire": None, "parent_fen": GOOD, "move_uci": "d2d4"}),
    ("repertoire=sql", {"repertoire": "' OR 1=1 --", "parent_fen": GOOD, "move_uci": "d2d4"}),
    ("repertoire=unicode-RLO", {"repertoire": "white‮", "parent_fen": GOOD, "move_uci": "d2d4"}),
    ("move_uci=''", {"repertoire": "white", "parent_fen": GOOD, "move_uci": ""}),
    ("move_uci=' e2e4 ' (spaces)", {"repertoire": "white", "parent_fen": GOOD, "move_uci": " d2d4 "}),
    ("move_uci='d2d4d4' (6 chars)", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4d4"}),
    ("move_uci='d2d4q' (promote q)", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4q"}),
    ("move_uci='0000' (null move)", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "0000"}),
    ("move_uci='e2e2'", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "e2e2"}),
    ("move_uci int (not str)", {"repertoire": "white", "parent_fen": GOOD, "move_uci": 1234}),
    ("move_uci list", {"repertoire": "white", "parent_fen": GOOD, "move_uci": ["d2", "d4"]}),
    ("parent_fen 4 fields no ep", {"repertoire": "white", "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq", "move_uci": "e2e4"}),
    ("parent_fen non-POSIX castling", {"repertoire": "white",
                                       "parent_fen": "r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w AHah - 0 1",
                                       "move_uci": "e1g1"}),
    ("parent_fen 4 tokens garbage castling", {"repertoire": "white",
                                              "parent_fen": "8/8/8/8/8/8/8/K6k w Zz - 0 1", "move_uci": "a1a2"}),
    ("comment 2MB", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4", "comment": "A" * 2_000_000}),
    ("comment XSS", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4",
                     "comment": "<img src=x onerror=alert(1)>"}),
    ("arrows junk", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4", "arrows": "]]]{{{"}),
    ("study_name whitespace only", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4", "study_name": "   "}),
    ("study_name 1MB", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4", "study_name": "S" * 1_000_000}),
    ("is_user_move str", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4", "is_user_move": "yes"}),
    ("eval_score huge", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4", "eval_score": 10**12}),
    ("popularity_pct NaN", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4", "popularity_pct": "NaN"}),
    ("extra unknown key", {"repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4", "__proto__": "x"}),
]
for label, body in cases:
    show(label, CLIENT.post("/api/repertoire/move", json=body))

print("\n  illegal-move check: did ANY illegal move get stored?")
print("  rows:", raw("SELECT move_uci, move_san, is_user_move, length(comment) clen FROM repertoire_moves"))

print("\n" + "=" * 78)
print("B. /api/repertoire/move_meta (POST)")
print("=" * 78)
show("nonexistent move (creates?)", CLIENT.post("/api/repertoire/move_meta", json={
    "repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4", "comment": "meta-created"}))
show("illegal move", CLIENT.post("/api/repertoire/move_meta", json={
    "repertoire": "white", "parent_fen": GOOD, "move_uci": "a1a8", "comment": "x"}))
show("comment 1MB on existing", CLIENT.post("/api/repertoire/move_meta", json={
    "repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4", "comment": "B" * 1_000_000}))
show("arrows 1MB", CLIENT.post("/api/repertoire/move_meta", json={
    "repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4", "arrows": "C" * 1_000_000}))
show("is_important on opponent move", CLIENT.post("/api/repertoire/move_meta", json={
    "repertoire": "white", "parent_fen": fen_after(["e4"]), "move_uci": "e7e5", "is_important": True}))
print("  rows:", raw("SELECT id, move_uci, move_san, is_user_move, is_important, length(comment) clen, length(arrows) alen, study_name FROM repertoire_moves"))

print("\n" + "=" * 78)
print("C. DELETE /api/repertoire/move")
print("=" * 78)
show("nonexistent", CLIENT.request("DELETE", "/api/repertoire/move", json={
    "repertoire": "white", "parent_fen": GOOD, "move_uci": "a1a8"}))
show("nonexistent fen", CLIENT.request("DELETE", "/api/repertoire/move", json={
    "repertoire": "white", "parent_fen": "8/8/8/8/8/8/8/8 w - - 0 1", "move_uci": "a1a2"}))
show("double delete", CLIENT.request("DELETE", "/api/repertoire/move", json={
    "repertoire": "white", "parent_fen": GOOD, "move_uci": "d2d4"}))
print("  rows after:", raw("SELECT move_uci FROM repertoire_moves"))

print("\n" + "=" * 78)
print("D. /api/repertoire/studies")
print("=" * 78)
show("create empty name", CLIENT.post("/api/repertoire/studies", json={"name": "", "repertoire": "white"}))
show("create whitespace name", CLIENT.post("/api/repertoire/studies", json={"name": "    ", "repertoire": "white"}))
show("create name 1MB", CLIENT.post("/api/repertoire/studies", json={"name": "N" * 1_000_000, "repertoire": "white"}))
show("create name RTL-override", CLIENT.post("/api/repertoire/studies", json={"name": "Anti‮French‬", "repertoire": "white"}))
show("create dup", CLIENT.post("/api/repertoire/studies", json={"name": "AF", "repertoire": "white"}))
show("create desc 1MB", CLIENT.post("/api/repertoire/studies", json={"name": "D", "repertoire": "white", "description": "D" * 1_000_000}))
show("delete id=-1", CLIENT.delete("/api/repertoire/studies/-1"))
show("delete id=99999", CLIENT.delete("/api/repertoire/studies/99999"))
show("delete id=abc", CLIENT.delete("/api/repertoire/studies/abc"))
show("delete id=1e9", CLIENT.delete("/api/repertoire/studies/1000000000"))
print("  studies:", study_rows())

print("\n" + "=" * 78)
print("E. /api/repertoire/tree + /explore query params")
print("=" * 78)
show("tree color=WHITE", CLIENT.get("/api/repertoire/tree", params={"color": "WHITE"}))
show("tree threshold=NaN", CLIENT.get("/api/repertoire/tree", params={"threshold": "NaN"}))
show("tree threshold=-1", CLIENT.get("/api/repertoire/tree", params={"threshold": "-1"}))
show("tree min_games=-5", CLIENT.get("/api/repertoire/tree", params={"min_games": "-5"}))
show("explore missing fen", CLIENT.get("/api/repertoire/explore"))
# NOTE: httpx itself refuses a 1 MB query component ("URL component 'query' too long"),
# so the oversized-FEN-via-query case can only be exercised at a size the client accepts.
show("explore fen + 4KB junk", CLIENT.get("/api/repertoire/explore", params={"fen": chess.STARTING_FEN + " x" * 4000}))
show("explore ratings injection", CLIENT.get("/api/repertoire/explore", params={"fen": chess.STARTING_FEN, "ratings": "2000&foo=bar"}))
print("\nDONE")
sys.stdout.flush()
os._exit(0)
show("clear bad color", CLIENT.post("/api/repertoire/clear", params={"repertoire": "purple"}))
