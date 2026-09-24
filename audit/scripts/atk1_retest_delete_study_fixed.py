"""BUILDER-03 probe: study_name last-writer-wins -> deleting study A destroys study B's shared moves."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

print("=" * 78)
print("SETUP: create 'Ruy Lopez' FIRST, then 'AF' (which shares the 1.e4 e5 2.Nf3 Nc6 3.Bb5 prefix)")
print("=" * 78)
wipe()
r = CLIENT.post("/api/repertoire/studies", json={"name": "Ruy Lopez", "repertoire": "white"})
print("create study Ruy Lopez ->", r.status_code, r.json())
add_line("white", ["e4", "e5", "Nf3", "Nc6", "Bb5", "Nf6", "O-O"], study_name="Ruy Lopez")
add_line("white", ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4"], study_name="AF")
show_tree("white", "after build (note study_name on the shared prefix = 'AF')")
show_studies()

print("\n" + "=" * 78)
print("STEP 1: DELETE study 'AF' with delete_moves=true  (the *newer* study)")
print("=" * 78)
af = one("SELECT id FROM repertoire_studies WHERE name='AF'")["id"]
r = CLIENT.delete(f"/api/repertoire/studies/{af}", params={"delete_moves": "true"})
print(r.status_code, r.json())
show_tree("white", "after delete AF")
print("remaining study rows:")
show_studies("after delete AF")
print("DANGLING study_moves (study 'Ruy Lopez' references moves that no longer exist):")
for d in dangling_study_moves():
    print("   ", d)
print("GET /api/repertoire/studies ->", CLIENT.get("/api/repertoire/studies").json())

print("\n" + "=" * 78)
print("STEP 2: APP RESTART (init_db -> migration restores moves missing from study_moves)")
print("=" * 78)
restart_app_db()
show_tree("white", "after restart")
print("dangling after restart:", dangling_study_moves())
print("GET /api/repertoire/studies ->")
for s in CLIENT.get("/api/repertoire/studies").json():
    print("   ", s)
