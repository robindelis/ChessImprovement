"""
FIXER-R2a helper: run the new regression tests against a PRE-fix snapshot of
`backend/repertoire.py`, proving they fail before the patch and pass after it.

The pre-fix snapshot is the file as FIXER-A left it (their `repertoire.py` was the
last edit before this wave), captured at
`/tmp/opencode/fixer_r2a/repertoire_prefix.py` before the first FIXER-R2a edit.

Usage:
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-R2a-01_prefix_runner.py \
        tests.test_repertoire_data_integrity

Never touches chess_improvement.db: `tests.test_setup` points CHESS_DB_PATH at a
throwaway temp database (BRIEF hard rules 1-3).
"""
import importlib.util
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PREFIX_COPY = os.environ.get(
    "FIXER_R2A_PREFIX_COPY", "/tmp/opencode/fixer_r2a/repertoire_prefix.py"
)

# 1. Load the pre-fix module and register it as `backend.repertoire` BEFORE anything
#    else (including tests.test_setup) imports the real one.
import backend  # noqa: E402

_spec = importlib.util.spec_from_file_location("backend.repertoire", PREFIX_COPY)
_prefix = importlib.util.module_from_spec(_spec)
sys.modules["backend.repertoire"] = _prefix
_spec.loader.exec_module(_prefix)
backend.repertoire = _prefix

# 2. Now the isolated temp-DB harness.
import tests.test_setup  # noqa: E402,F401  (FIRST backend-adjacent import by contract)

if __name__ == "__main__":
    targets = sys.argv[1:] or ["tests.test_repertoire_data_integrity"]
    if targets == ["discover"]:
        suite = unittest.TestLoader().discover("tests", top_level_dir=PROJECT_ROOT)
    else:
        suite = unittest.TestLoader().loadTestsFromNames(targets)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\npre-fix module:", PREFIX_COPY)
    print("pre-fix failures:", len(result.failures), "errors:", len(result.errors))
    sys.exit(0 if result.wasSuccessful() else 1)
