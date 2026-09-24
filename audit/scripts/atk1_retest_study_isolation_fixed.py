"""BUILDER-02 probe: study isolation + delete_study(delete_moves=True) clobbering shared moves."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

print("=" * 78)
print("SETUP: two white studies that share the prefix 1.e4 e5 2.Nf3 Nc6 3.Bb5")
print("  AF   : e4 e5 Nf3 Nc6 Bb5 a6 Ba4")
print("  Ruy  : e4 e5 Nf3 Nc6 Bb5 Nf6 O-O")
print("=" * 78)
wipe()
add_line("white", ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4"], study_name="AF")
add_line("white", ["e4", "e5", "Nf3", "Nc6", "Bb5", "Nf6", "O-O"], study_name="Ruy Lopez")
show_tree("white", "after build")
show_studies()
print("study_moves:")
for r in raw("SELECT * FROM study_moves ORDER BY study_id, parent_fen"):
    print("   study=%s %-6s %s" % (r["study_id"], r["move_uci"], " ".join(r["parent_fen"].split()[:2])))

print("\n" + "=" * 78)
print("STEP 1: DELETE study 'AF' with delete_moves=true")
print("=" * 78)
af = one("SELECT id FROM repertoire_studies WHERE name='AF'")["id"]
r = CLIENT.delete(f"/api/repertoire/studies/{af}", params={"delete_moves": "true"})
print(r.status_code, r.json())
show_tree("white", "after deleting study AF")
show_studies("after deleting study AF")
print("dangling study_moves (surviving study 'Ruy Lopez' still references deleted moves):")
for d in dangling_study_moves():
    print("   ", d)
print("GET /api/repertoire/studies ->", CLIENT.get("/api/repertoire/studies").json())

print("\n" + "=" * 78)
print("STEP 2: simulate APP RESTART (init_db -> migration step 1 restores missing study moves)")
print("=" * 78)
restart_app_db()
show_tree("white", "after restart")
print("\nuser moves at the position after 1.e4 e5 2.Nf3 Nc6 3.Bb5:")
P = parent_of(["e4", "e5", "Nf3", "Nc6", "Bb5"])
for row in raw("SELECT move_uci, move_san, is_user_move, study_name FROM repertoire_moves "
               "WHERE parent_fen=?", (normalize_fen(P),)):
    print("   ", dict(row))
print("\nGET /api/repertoire/studies ->")
for s in CLIENT.get("/api/repertoire/studies").json():
    print("   ", s)
