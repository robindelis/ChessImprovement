"""
FIXER-V helper: run the new line-end-semantics tests against a PRE-fix snapshot, proving they
fail before the patch and pass after it (AGENTS.md §6.4).

The pre-fix snapshots are produced by `audit/scripts/FIXER-V-01_make_prefix.py`:
  * `/tmp/opencode/fixerv/repertoire_prefix.py`          - `backend/repertoire.py` pre-FIXER-V
  * `/tmp/opencode/fixerv/repertoire_trainer_prefix.js`  - `frontend/repertoire_trainer.js` pre-FIXER-V

**Shims.** The pre-fix module has no line-end vocabulary, because that vocabulary IS the patch. The
runner injects the missing *names only* so the test module can import; every assertion still
exercises pre-fix logic. This is the same pattern FIXER-T used:

| shim | pre-fix behaviour it stands in for |
| --- | --- |
| `LINE_POSITION_*` | the strings the pre-fix code never produced |
| `classify_line_position` | the pre-fix predicate `is_line_end = (opponent_reply is None or next_expected_uci is None)` |
| `LINE_END_*` | reason strings the pre-fix code never produced |

**Frontend.** `tests/frontend_runtime.run_scenario` reads `frontend/repertoire_trainer.js` from
disk, so the runner temporarily stages the pre-fix trainer into `frontend/` and restores the real
file afterwards (in a `finally`, so a crash cannot leave the working tree patched out). The
provenance assertion inside the test compares the SHA-256 the harness executed against the file on
disk, so the swap is *proven*, not assumed.

Usage:
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-V-01_prefix_runner.py \
        tests.test_line_end_semantics

Never touches chess_improvement.db: `tests.test_setup` points CHESS_DB_PATH at a throwaway temp
database (BRIEF hard rules 1-3). Port 38281 is never contacted. No Stockfish process is started.
"""
import hashlib
import importlib.util
import os
import shutil
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PREFIX_COPY = os.environ.get(
    "FIXER_V_PREFIX_COPY", "/tmp/opencode/fixerv/repertoire_prefix.py"
)
PREFIX_JS = os.environ.get(
    "FIXER_V_PREFIX_JS", "/tmp/opencode/fixerv/repertoire_trainer_prefix.js"
)
REAL_JS = os.path.join(PROJECT_ROOT, "frontend", "repertoire_trainer.js")
JS_BACKUP = os.path.join("/tmp/opencode/fixerv", "repertoire_trainer_REAL.js")


# 1. Load the pre-fix module and register it as `backend.repertoire` BEFORE anything else
#    (including tests.test_setup) imports the real one.
import backend  # noqa: E402

_spec = importlib.util.spec_from_file_location("backend.repertoire", PREFIX_COPY)
_prefix = importlib.util.module_from_spec(_spec)
sys.modules["backend.repertoire"] = _prefix
_spec.loader.exec_module(_prefix)
backend.repertoire = _prefix


# 2. Inject ONLY the names the pre-fix file cannot have.
if not hasattr(_prefix, "LINE_POSITION_CONTINUES"):
    _prefix.LINE_POSITION_CONTINUES = "continues"
    _prefix.LINE_POSITION_COMPLETED = "completed_line"
    _prefix.LINE_POSITION_AWAITING_REPLY = "awaiting_user_reply"
    _prefix.LINE_END_REPERTOIRE_EXHAUSTED = "repertoire_exhausted"
    _prefix.LINE_END_UNANSWERED_BRANCHES = "unanswered_opponent_branches"

if not hasattr(_prefix, "classify_line_position"):
    def _classify(reached_by_user_move, has_prepared_reply):
        """The pre-fix predicate, verbatim.

        `is_line_ended = (opponent_reply is None or next_expected_uci is None)` cannot express
        "the opponent moved and nothing is prepared" separately from "nothing is prepared at
        all", so the overruled case is folded into the completion.
        """
        if has_prepared_reply:
            return _prefix.LINE_POSITION_CONTINUES
        return _prefix.LINE_POSITION_COMPLETED
    _prefix.classify_line_position = _classify


# 3. The isolated temp-DB harness.
import tests.test_setup  # noqa: E402,F401  (FIRST backend-adjacent import by contract)


def _swap_in_pre_fix_frontend():
    """Stage the pre-fix trainer, returning the sha256 of the file now on disk."""
    if not os.path.isfile(PREFIX_JS):
        return None
    os.makedirs(os.path.dirname(JS_BACKUP), exist_ok=True)
    if not os.path.isfile(JS_BACKUP):
        shutil.copyfile(REAL_JS, JS_BACKUP)
    shutil.copyfile(PREFIX_JS, REAL_JS)
    with open(REAL_JS, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _restore_frontend():
    if os.path.isfile(JS_BACKUP):
        shutil.copyfile(JS_BACKUP, REAL_JS)


def main():
    swapped = _swap_in_pre_fix_frontend()
    try:
        targets = sys.argv[1:] or ["tests.test_line_end_semantics"]
        if targets == ["discover"]:
            suite = unittest.TestLoader().discover("tests", top_level_dir=PROJECT_ROOT)
        else:
            suite = unittest.TestLoader().loadTestsFromNames(targets)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        _restore_frontend()

    print("\npre-fix backend : %s" % PREFIX_COPY)
    print("pre-fix frontend: %s (sha256 on disk: %s)" % (PREFIX_JS, swapped))
    print("real frontend restored: %s" % os.path.isfile(REAL_JS))
    print("pre-fix failures: %d errors: %d" % (len(result.failures), len(result.errors)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
