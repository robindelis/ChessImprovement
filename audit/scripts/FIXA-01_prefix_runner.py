"""
FIXER-A helper: run the regression tests (or any unittest target) against a PRE-FIX snapshot of
`backend/repertoire.py`, proving the new tests actually fail before the patch.

The pre-fix snapshot is the file this agent edited, copied to
/tmp/opencode/fixera/repertoire_prefix.py before the first edit (see audit/patches/FIXER-A-01.md).

Usage:
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXA-01_prefix_runner.py \
        tests.test_repertoire_write_safety

Never touches chess_improvement.db: tests.test_setup points CHESS_DB_PATH at a temp copy.
"""
import importlib.util
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PREFIX_COPY = os.environ.get("FIXA_PREFIX_COPY", "/tmp/opencode/fixera/repertoire_prefix.py")

# 1. Import the pre-fix module *and* register it as `backend.repertoire` before anything else
#    (including tests.test_setup) imports the real one.
import backend  # noqa: E402

_spec = importlib.util.spec_from_file_location("backend.repertoire", PREFIX_COPY)
_prefix = importlib.util.module_from_spec(_spec)
sys.modules["backend.repertoire"] = _prefix
_spec.loader.exec_module(_prefix)
backend.repertoire = _prefix

# 2. Now the isolated temp-DB harness.
import tests.test_setup  # noqa: E402,F401  (FIRST backend-adjacent import by contract)

if __name__ == "__main__":
    targets = sys.argv[1:] or ["tests.test_repertoire_write_safety"]
    if targets == ["discover"]:
        suite = unittest.TestLoader().discover("tests", top_level_dir=PROJECT_ROOT)
    else:
        suite = unittest.TestLoader().loadTestsFromNames(targets)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\npre-fix module:", PREFIX_COPY)
    print("pre-fix failures:", len(result.failures), "errors:", len(result.errors))
    sys.exit(0 if result.wasSuccessful() else 1)
