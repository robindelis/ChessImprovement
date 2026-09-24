"""BUILDER-01 probe: one-user-move invariant + study_moves orphaning on user-move replacement."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

print("=" * 78)
print("SETUP: build Anti-French for white as a study: 1.e4 e6 2.d4 d5 3.e5 Nf6 4.Bb5")
print("=" * 78)
wipe()
for t in add_line("white", ["e4", "e6", "d4", "d5", "e5", "Nf6", "Bb5"], study_name="AF"):
    print("  ", t[0], t[1])
show_tree("white", "after build")
show_studies()

P3 = parent_of(["e4", "e6", "d4", "d5", "Nc3"])
print("\nparent position for move 3:", P3)
print("rows stored at that parent:", raw(
    "SELECT id, move_uci, move_san, is_user_move FROM repertoire_moves WHERE parent_fen=?",
    (normalize_fen(P3),)))

print("\n" + "=" * 78)
print("STEP 1: replace user move 3.e5 with 3.Nc3  (AGENTS.md 9.3 hazard)")
print("=" * 78)
r = add("white", P3, "b1c3", is_user_move=True, study_name="AF")
print("POST /api/repertoire/move ->", r.status_code, r.json())
show_tree("white", "after replace")
show_studies("after replace")
print("dangling study_moves (rows whose repertoire_moves row is gone):", dangling_study_moves())

print("\n" + "=" * 78)
print("STEP 2: simulate an APP RESTART (init_db -> migrate_studies_and_cleanup_blunders)")
print("=" * 78)
restart_app_db()
show_tree("white", "after restart")
print("\nUSER moves now at the 3rd-move parent position:")
for row in raw("SELECT move_uci, move_san, is_user_move FROM repertoire_moves "
               "WHERE repertoire='white' AND parent_fen=?", (normalize_fen(P3),)):
    print("  ", dict(row))
print("\nGET /api/repertoire/studies?repertoire=white ->")
for s in CLIENT.get("/api/repertoire/studies", params={"repertoire": "white"}).json():
    print("  ", s)
