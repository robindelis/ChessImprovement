"""
CODE-05 repro: prove the unit-test suite is NOT hermetic — it depends on a copy of the
user's PRODUCTION chess_improvement.db (307 real games / 153 blunders), and several
assertions silently become VACUOUS when that data is absent.

Safe: never opens the production DB for writing. It only re-points
backend.db.DEFAULT_DB_PATH at a nonexistent path BEFORE tests.test_setup imports,
so setup_test_db() skips the `src.backup(dst)` copy.

Run:  /home/robin/miniconda3/bin/python3 audit/scripts/CODE-05_repro.py
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

# --- Sabotage the production-DB copy BEFORE tests.test_setup runs -------------
import backend.db as db_mod
db_mod.DEFAULT_DB_PATH = "/tmp/opencode/definitely_does_not_exist_chess.db"

import tests.test_setup  # noqa: E402  (this is the module under scrutiny)

assert os.environ["CHESS_DB_PATH"].startswith("/tmp/chess_test_"), "isolation failed"
assert not os.path.exists(db_mod.DEFAULT_DB_PATH)
print("OK  tests.test_setup DID isolate onto a temp DB path")
print("    CHESS_DB_PATH =", os.environ["CHESS_DB_PATH"])

# Prove the temp DB is EMPTY of user data now (no backup was taken)
import sqlite3
conn = sqlite3.connect(os.environ["CHESS_DB_PATH"])
games = conn.execute("select count(*) from games").fetchone()[0]
blunders = conn.execute("select count(*) from blunders").fetchone()[0]
conn.close()
print(f"    temp DB games={games} blunders={blunders}  <-- production copy SKIPPED")

# Now run the whole suite against the empty temp DB
loader = unittest.TestLoader()
suite = loader.discover(start_dir=os.path.join(PROJECT_ROOT, "tests"), top_level_dir=PROJECT_ROOT)
runner = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w"))
result = runner.run(suite)
print()
print("=== RESULT (temp DB with NO production data) ===")
print(f"run={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}")
for t, tb in result.failures + result.errors:
    first = tb.strip().splitlines()[-1]
    print(f"  FAIL {t.id()}\n        {first}")
