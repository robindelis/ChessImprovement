"""
Pre-fix proof harness.

Copies the ORIGINAL (pre-patch) backend/db.py into a scratch package tree, points
`backend.db` at it, and runs the NEW regression test module against it. The
production repo and the production database are never touched: the scratch tree
lives entirely under /tmp and the tests themselves use their own throwaway DB.

Usage:  /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-P0_prefix_proof.py
"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFIX_DB = os.environ.get("PREFIX_DB", "/tmp/opencode/prefix_db.py")
TEST_MODULE = "tests.test_migration_safety"

SCRATCH = tempfile.mkdtemp(prefix="fixer_p0_prefix_")

# Build a scratch package: real repo's backend/ + tests/, but with db.py replaced
# by the pre-patch copy. Symlinks keep it cheap and guarantee we never write to
# the repo.
shutil.copytree(os.path.join(REPO, "backend"), os.path.join(SCRATCH, "backend"),
                ignore=shutil.ignore_patterns("__pycache__"))
shutil.copytree(os.path.join(REPO, "tests"), os.path.join(SCRATCH, "tests"),
                ignore=shutil.ignore_patterns("__pycache__"))
shutil.copy(PREFIX_DB, os.path.join(SCRATCH, "backend", "db.py"))

# The pre-patch module has no FORBIDDEN_AUTO_STUDY_NAMES / find_unreachable_*
# names, so without this shim the test module would fail at IMPORT time and the
# data-loss assertions would never run. The shim adds ONLY the new *names*, as
# pure read-only helpers. init_db() and migrate_studies_and_cleanup_blunders()
# are left byte-for-byte as they were pre-patch, so every failure reported below
# is a genuine behavioural regression, not an ImportError.
with open(os.path.join(SCRATCH, "backend", "db.py"), "a") as f:
    f.write('''

# --- FIXER-P0 proof shim: names only, no behaviour change -------------------
FORBIDDEN_AUTO_STUDY_NAMES = ("Italian Game", "Sicilian Dragon", "King's Indian Defense")


def find_unreachable_repertoire_moves(conn):
    """Read-only copy of the pre-patch reachability BFS, for diagnostic use only."""
    result = {}
    start_fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
    start_3 = ' '.join(start_fen.split()[:3])
    for rep in ('white', 'black'):
        cursor = conn.cursor()
        cursor.execute("SELECT id, parent_fen, fen FROM repertoire_moves WHERE repertoire = ?", (rep,))
        rows = cursor.fetchall()
        if not rows:
            continue
        parent_map, dest_map = {}, {}
        for r in rows:
            parent_map.setdefault(' '.join(r["parent_fen"].split()[:3]), []).append(r["id"])
            dest_map[r["id"]] = ' '.join(r["fen"].split()[:3])
        queue, visited, reachable = [start_3], set(), set()
        while queue:
            curr = queue.pop(0)
            if curr in visited:
                continue
            visited.add(curr)
            for m_id in parent_map.get(curr, ()):
                reachable.add(m_id)
                dest = dest_map.get(m_id)
                if dest and dest not in visited:
                    queue.append(dest)
        orphans = sorted(r["id"] for r in rows if r["id"] not in reachable)
        if orphans:
            result[rep] = orphans
    return result
# --- end shim --------------------------------------------------------------
''')

open(os.path.join(SCRATCH, "backend", "__init__.py"), "a").close()
open(os.path.join(SCRATCH, "tests", "__init__.py"), "a").close()
# tests/test_setup.py copies the production DB if present; make sure the scratch
# tree can never find it.
if os.path.exists(os.path.join(SCRATCH, "chess_improvement.db")):
    os.unlink(os.path.join(SCRATCH, "chess_improvement.db"))

os.chdir(SCRATCH)
sys.path.insert(0, SCRATCH)

print(f"scratch tree : {SCRATCH}")
print(f"backend/db.py: {PREFIX_DB} (pre-patch, copied in)")
assert "purge_orphans" not in open(os.path.join(SCRATCH, "backend", "db.py")).read(), \
    "prefix_db.py does not look like the pre-patch file"
print("confirmed: scratch db.py has no purge_orphans parameter (pre-patch)\n")

suite = unittest.TestLoader().loadTestsFromNames([TEST_MODULE])
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(f"\nPRE-FIX RESULT: run={result.testsRun} failures={len(result.failures)} "
      f"errors={len(result.errors)}")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(0 if result.wasSuccessful() else 1)
