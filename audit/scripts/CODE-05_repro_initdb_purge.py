"""
CODE-05 repro #8: startup robustness / data loss.

The git-committed chess_improvement.db (git HEAD) holds 243 real repertoire_moves
belonging to RobiDeli, and its schema has NO repertoire_studies / study_moves tables.
start.py:19 calls init_db() unconditionally on every boot, and init_db() calls
migrate_studies_and_cleanup_blunders() (backend/db.py:237) which DELETEs rows from
repertoire_moves (backend/db.py:364).

This script copies the committed DB to /tmp and runs init_db() on the COPY only.
The production file is opened strictly read-only (mode=ro&immutable=1) and never
written. The output is compared against the real worktree DB, which now has
repertoire_moves = 0.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-05_repro_initdb_purge.py
"""
import os
import shutil
import sqlite3
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = "/home/robin/miniconda3/bin/python3"
WORK = "/tmp/opencode/code05_initdb"
os.makedirs(WORK, exist_ok=True)

# 1. materialise the committed DB from git into /tmp (git cat-file is read-only)
committed = os.path.join(WORK, "committed.db")
with open(committed, "wb") as fh:
    subprocess.run(["git", "-C", PROJECT_ROOT, "show", "HEAD:chess_improvement.db"],
                   stdout=fh, check=True)
os.chmod(committed, 0o644)

victim = os.path.join(WORK, "victim.db")
shutil.copy2(committed, victim)


def counts(p):
    con = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
    out = {}
    for t in ("repertoire_moves", "repertoire_studies", "study_moves", "games", "blunders"):
        try:
            out[t] = con.execute(f"select count(*) from {t}").fetchone()[0]
        except Exception as e:
            out[t] = f"ABSENT"
    con.close()
    return out


print("committed DB (git HEAD)      :", counts(committed))
print("worktree production DB       :", counts(os.path.join(PROJECT_ROOT, "chess_improvement.db")))

# 2. run the REAL init_db() against the /tmp copy only
os.environ["CHESS_DB_PATH"] = victim
sys.path.insert(0, PROJECT_ROOT)
from backend.db import init_db, get_db_path  # noqa: E402

print("\nget_db_path() now points at  :", get_db_path())
assert victim in get_db_path(), "REFUSING: would touch production!"

init_db()

after = counts(victim)
print("\nafter a single init_db() boot:", after)
print()
if after["repertoire_moves"] == 0:
    print(">>> DATA LOSS REPRODUCED: init_db() purged all "
          f"{counts(committed)['repertoire_moves']} personal repertoire moves on first boot.")
    con = sqlite3.connect(victim)
    print(">>> studies auto-created by the migration:",
          con.execute("select name, repertoire from repertoire_studies").fetchall())
    con.close()
else:
    print(">>> no purge; repertoire_moves =", after["repertoire_moves"])
