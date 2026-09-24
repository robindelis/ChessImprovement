import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

wipe()
# 1. create_study collapses internal whitespace + zero-width characters
r1 = CLIENT.post("/api/repertoire/studies", json={"name": "Anti  French", "repertoire": "white"})
print("create_study('Anti  French') ->", repr(r1.json().get("study", {}).get("name")))

# 2. POST /api/repertoire/move only .strip()s the study name -> a phantom study
add("white", chess.STARTING_FEN, "e2e4", is_user_move=True, study_name="Anti  French")
print("\nstudy rows now:")
for s in study_rows():
    print("  ", s)

print("\nGET /api/repertoire/studies ->")
for s in CLIENT.get("/api/repertoire/studies", params={"repertoire": "white"}).json():
    print("  ", repr(s["name"]), "total_moves =", s["total_moves"])

# 3. zero-width joiner + U+202E RIGHT-TO-LEFT OVERRIDE
add("white", chess.STARTING_FEN, "d2d4", is_user_move=False, study_name="Anti French‍‮")
print("\nafter a zero-width + RLO study_name via POST /api/repertoire/move:")
for s in study_rows():
    print("  ", repr(s["name"]))

r = CLIENT.post("/api/repertoire/studies", json={"name": "Anti French‍‮", "repertoire": "white"})
print("\ncreate_study with the same zero-width/RLO name ->", r.json().get("status"),
      repr(r.json().get("study", {}).get("name")))

print("\nfinal study list (visually identical entries in the Builder's <select>):")
for s in study_rows():
    print("  ", s["id"], repr(s["name"]))
