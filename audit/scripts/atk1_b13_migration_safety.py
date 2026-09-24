"""BUILDER-13 probe: the startup migration `migrate_studies_and_cleanup_blunders()` erases
the user's repertoire.

`backend/db.py` step 3 ("Purge truly isolated orphan moves") walks the tree from the start
position using a 3-token FEN key and then UNCONDITIONALLY

    DELETE FROM repertoire_moves WHERE id IN (...orphan_ids...)

The only protection is a `study_moves` row for the move.  A repertoire row whose `parent_fen`
is not the `fen` of any other row (an imported middlegame sub-line, an endgame fragment, a
line started from a puzzle/practice position, anything written by an older schema) is deleted
on the very next application start, with no backup, no dry-run and no message.  A boot with
zero studies then also fabricates the three default studies that AGENTS.md 9.3 explicitly
forbids, because a repertoire only permits one user move per position.

PART 1 - minimal reproduction: one legitimate fragment is enough.
PART 2 - the 243 rows that were actually recovered from the production database
         (`recovered/repertoire_moves_from_git_HEAD.json`, whose own note says they were
         "erased ... on boot") are replayed into the throwaway temp DB and put through a
         single `init_db()`.
"""
import os
import sys
import json
import sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

import backend.db as dbm

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REC = os.path.join(ROOT, "recovered", "repertoire_moves_from_git_HEAD.json")


def boot():
    """Exactly what start.py does on startup: FastAPI's startup_event -> init_db()."""
    dbm.init_db()


# --------------------------------------------------------------------------- PART 1
print("=" * 78)
print("PART 1 - minimal reproduction: ONE legitimate fragment is enough")
print("=" * 78)
wipe()
add_line("white", ["e4", "e5", "Nf3", "Nc6", "Bb5"], study_name="Anti French")
frag = "8/4P3/8/8/8/8/k7/4K3 w - -"     # the exact shape of a recovered production row
r = add("white", frag, "e7e8q", is_user_move=True)      # no study_name -> no study_moves row
print("inserted fragment ->", r.status_code, r.json().get("san"),
      " study_name =", repr(r.json().get("study_name")))
print("study_moves rows (belong to 'Anti French' only):",
      one("SELECT COUNT(*) c FROM study_moves")["c"])
print("rows before boot:", one("SELECT COUNT(*) c FROM repertoire_moves")["c"],
      [x["move_san"] for x in tree_rows("white")])
boot()
after1 = one("SELECT COUNT(*) c FROM repertoire_moves")["c"]
print("rows after  boot:", after1, [x["move_san"] for x in tree_rows("white")])
print("fragment survived:",
      bool(one("SELECT COUNT(*) c FROM repertoire_moves WHERE move_uci='e7e8q'")["c"]),
      "<-- GONE, silently, on the next application start" if after1 < 6 else "")

# --------------------------------------------------------------------------- PART 2
print()
print("=" * 78)
print("PART 2 - the 243 recovered PRODUCTION rows through a single boot")
print("=" * 78)
rec = json.load(open(REC, encoding="utf-8"))
rows, cols = rec["moves"], rec["columns"]
print("recovered note:", rec["_note"])

wipe()
conn = sqlite3.connect(dbm.get_db_path())
kept = [c for c in cols if c != "study_name"]      # study_name added after the dump
ph = ",".join("?" for _ in kept)
for _r in rows:
    conn.execute(f"INSERT OR REPLACE INTO repertoire_moves ({','.join(kept)}) VALUES ({ph})",
                 tuple(_r.get(c) for c in kept))
conn.commit()
conn.close()

n0 = one("SELECT COUNT(*) c FROM repertoire_moves")["c"]
print(f"\nrows loaded                      : {n0}")
print(f"rows with a study_moves link      : {one('SELECT COUNT(*) c FROM study_moves')['c']}")
print("sample of the loaded rows:")
for x in raw("SELECT id, repertoire, move_san, parent_fen FROM repertoire_moves ORDER BY id LIMIT 6"):
    print(f"   id={x['id']:<6} {x['repertoire']:<6} {x['move_san']:<7} parent={x['parent_fen']}")

print("\n--- one application start (init_db) ---")
boot()
n1 = one("SELECT COUNT(*) c FROM repertoire_moves")["c"]
print(f"\nrows after a single boot         : {n1}")
print(f"ROWS PERMANENTLY DELETED         : {n0 - n1}  ({100.0 * (n0 - n1) / n0:.0f}% of the repertoire)")
print("\nGET /api/repertoire/studies ->")
for s in CLIENT.get("/api/repertoire/studies").json():
    print("   ", {k: s[k] for k in ("id", "name", "repertoire", "total_moves", "user_moves")})
print("\n(AGENTS.md 9.3: 'Italian Game', 'Sicilian Dragon' and \"King's Indian Defense\" are the")
print(" studies that must never be seeded, because a repertoire permits one user move per")
print(" position and 3.Nc3/3...Nf3 overwrite the prepared Anti-French 3.e5.)")
print("\nGET /api/repertoire/tree?color=white -> total_moves =",
      CLIENT.get("/api/repertoire/tree", params={"color": "white", "min_games": 0}).json()["total_moves"])

sys.stdout.flush()
os._exit(0)
