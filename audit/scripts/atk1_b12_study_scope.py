"""BUILDER-12 probe: the Builder's study selector does not scope the tree, the coverage
percentage or the gap list -- `refreshTree()` never sends the study and
`/api/repertoire/tree` has no study parameter at all.
"""
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JS = os.path.join(ROOT, "frontend", "repertoire_builder.js")
API = os.path.join(ROOT, "backend", "api.py")

print("=" * 78)
print("1. what the Builder actually requests")
print("=" * 78)
src = open(JS, encoding="utf-8").read()
m = re.search(r"async refreshTree\(\)\s*\{(.*?)\n    \}", src, re.S)
print("  refreshTree():")
for line in m.group(1).strip().splitlines():
    print("   ", line.strip())
print("\n  onStudyChange():")
m2 = re.search(r"async onStudyChange\([^)]*\)\s*\{(.*?)\n    \}", src, re.S)
for line in m2.group(1).strip().splitlines():
    print("   ", line.strip())

api = open(API, encoding="utf-8").read()
m3 = re.search(r'@app\.get\("/api/repertoire/tree"\)\ndef get_repertoire_tree\((.*?)\):', api, re.S)
print("\n  GET /api/repertoire/tree parameters:")
print("   ", " ".join(m3.group(1).split()))
print("    -> no study / study_name parameter exists on the endpoint")

print("\n" + "=" * 78)
print("2. two studies, disjoint lines - what the tree/coverage/gaps report")
print("=" * 78)
wipe()
add_line("white", ["e4", "e6", "d4", "d5", "e5", "Nf6"], study_name="Anti French")
add_line("white", ["e4", "c6", "d4", "d5", "Nc3", "dxe4"], study_name="Caro-Kann")
show_studies()
print("\nGET /api/repertoire/studies ->")
for s in CLIENT.get("/api/repertoire/studies", params={"repertoire": "white"}).json():
    print("   ", {k: s[k] for k in ("id", "name", "total_moves", "user_moves")})

import backend.repertoire as R


class NoNet:
    @staticmethod
    def get(url, params=None, headers=None, timeout=None, **kw):
        raise OSError("offline (probe)")


real = R.requests
R.requests = NoNet
t = R.RepertoireManager.get_instance().get_repertoire_tree("white", min_games=0)
R.requests = real
print("\nGET /api/repertoire/tree?color=white (offline) ->")
print("   total_moves :", t["total_moves"], "  (union of BOTH studies)")
print("   coverage_pct:", t["coverage_pct"])
print("   gaps        :", len(t["gaps"]), "entries, none of them attributed to a study")
print("   gap[0]      :", t["gaps"][0] if t["gaps"] else None)
print("\n   per-study move counts shown in the dropdown:",
      [(s['name'], s['total_moves']) for s in CLIENT.get('/api/repertoire/studies').json()])
print("   -> the study dropdown reports per-study counts while the coverage % and the")
print("      gap list are computed over the whole repertoire, so neither number can be")
print("      interpreted as 'how complete is the study I am currently working on'.")

print("\n" + "=" * 78)
print("3. what a user move added while a study is selected does")
print("=" * 78)
print("   saveMove() posts study_name = this.currentStudy, so a move added while")
print("   'Caro-Kann' is selected is filed under Caro-Kann, yet it shows up in the")
print("   tree for BOTH studies (the tree is not partitioned) and counts towards the")
print("   shared coverage %.")


# The process keeps an event-loop/thread alive at exit in this probe environment;
# flush and hard-exit so the script is reliably re-runnable.
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)
