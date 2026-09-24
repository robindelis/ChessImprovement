"""
FIXER-W helper: run the FIXER-W-01 regression tests against PRE-fix snapshots, proving they
fail before the patch and pass after it (AGENTS.md 6.4).

Pre-fix snapshots (produced once, see `audit/patches/FIXER-W-01.md`):
  * `/tmp/opencode/fixerw/repertoire_prefix.py`            - `backend/repertoire.py` pre-FIXER-W
  * `/tmp/opencode/fixerw/repertoire_builder_prefix.js`    - `frontend/repertoire_builder.js`
  * `/tmp/opencode/fixerw/style_prefix.css`                - `frontend/style.css`
  * `/tmp/opencode/fixerw/sync_robustness_prefix.py`       - `tests/test_repertoire_sync_robustness.py`
  * `/tmp/opencode/fixerw/test_setup_prefix.py`            - `tests/test_setup.py`
  * `/tmp/opencode/fixerw/test_api_prefix.py`              - `tests/test_api.py`

**Backend.** The pre-fix module is registered as `backend.repertoire` BEFORE anything else
(including `tests.test_setup`) imports the real one, exactly like FIXER-V's runner.

**Frontend.** `tests/frontend_runtime.run_scenario` reads `frontend/repertoire_builder.js` and
`frontend/style.css` from disk, so the runner temporarily stages the pre-fix copies and restores
the real files afterwards in a `finally`, so a crash cannot leave the working tree patched out.
The tests' provenance assertions compare the SHA-256 the harness actually executed against the
file on disk, so the swap is *proven*, not assumed.

**tests/**. The pre-fix `tests/test_repertoire_sync_robustness.py`, `tests/test_setup.py` and
`tests/test_api.py` are staged the same way, for the same reason: the point is to run the NEW
regression tests against the OLD infrastructure.

Usage:
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-W-01_prefix_runner.py \
        tests.test_repertoire.TestPreparedMoveLookupIsDeterministic

    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-W-01_prefix_runner.py --order \
        tests.test_repertoire_sync_robustness \
        tests.test_api.TestAPI.test_account_endpoint \
        tests.test_api.TestAPI.test_sync_malformed_games

`--order` runs the named targets as ONE suite in ONE process, which is what an order-dependence
reproduction needs (`unittest` would otherwise order them the same way anyway; the point is a
single shared interpreter, which is how `unittest discover` behaves).

Never touches `chess_improvement.db`: `import tests.test_setup` is the first backend-adjacent
import and points `CHESS_DB_PATH` at a throwaway temp database (BRIEF hard rules 1-3). Port
38281 is never contacted. No Stockfish process is started.
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

SNAP_DIR = os.environ.get("FIXER_W_SNAP_DIR", "/tmp/opencode/fixerw")
PREFIX_BACKEND = os.path.join(SNAP_DIR, "repertoire_prefix.py")

#: `repo-relative real file` -> `pre-fix snapshot`
STAGED_FILES = {
    "frontend/repertoire_builder.js": "repertoire_builder_prefix.js",
    "frontend/style.css": "style_prefix.css",
    "tests/test_repertoire_sync_robustness.py": "sync_robustness_prefix.py",
    "tests/test_setup.py": "test_setup_prefix.py",
    "tests/test_api.py": "test_api_prefix.py",
}

# 1. Load the pre-fix backend module and register it as `backend.repertoire` BEFORE anything
#    else (including tests.test_setup) imports the real one.
import backend  # noqa: E402

_spec = importlib.util.spec_from_file_location("backend.repertoire", PREFIX_BACKEND)
_prefix = importlib.util.module_from_spec(_spec)
sys.modules["backend.repertoire"] = _prefix
_spec.loader.exec_module(_prefix)
backend.repertoire = _prefix

# `tests.test_setup` is imported INSIDE main(), *after* the pre-fix files are staged, so a
# staged `tests/test_setup.py` is really the one the suite runs against. Importing it here
# would put the real (post-fix) module in sys.modules and the swap would be a no-op.


def _stage(only=None):
    """Replace every staged file with its pre-fix copy. Returns {path: (pre_sha, backup)}."""
    recorded = {}
    for rel, snapshot in STAGED_FILES.items():
        if only and snapshot.rsplit("_prefix", 1)[0] not in only:
            continue
        real = os.path.join(PROJECT_ROOT, rel)
        pre = os.path.join(SNAP_DIR, snapshot)
        if not (os.path.isfile(real) and os.path.isfile(pre)):
            continue
        with open(real, "rb") as fh:
            backup = fh.read()
        recorded[rel] = (hashlib.sha256(open(pre, "rb").read()).hexdigest(),
                         hashlib.sha256(backup).hexdigest(), backup)
        shutil.copyfile(pre, real)
    return recorded


def _legacy_stub_ledger():
    """Put `_SyncTestBase.setUp` / `tearDown` back the way they were pre-fix.

    Needed because the ledger bug lives in the *same file* as the new tests that pin it, so
    staging the pre-fix copy of that file would delete the tests instead of exercising them.
    The two pre-fix methods are reproduced verbatim from
    `/tmp/opencode/fixerw/sync_robustness_prefix.py`.
    """
    import tests.test_repertoire_sync_robustness as mod

    def setUp(self):                                       # noqa: N802 - unittest API
        self._patched = []

    def tearDown(self):                                    # noqa: N802 - unittest API
        while self._patched:
            target, name, original = self._patched.pop()
            setattr(target, name, original)

    mod._SyncTestBase.setUp = setUp
    mod._SyncTestBase.tearDown = tearDown
    return mod._SyncTestBase


def _restore(recorded):
    """Put the real files back, and prove it."""
    ok = True
    for rel, (_pre_sha, real_sha, backup) in recorded.items():
        real = os.path.join(PROJECT_ROOT, rel)
        with open(real, "wb") as fh:
            fh.write(backup)
        with open(real, "rb") as fh:
            ok = ok and hashlib.sha256(fh.read()).hexdigest() == real_sha
    return ok


def main():
    argv = sys.argv[1:]
    one_process = False
    legacy_ledger = False
    only = None
    while argv and argv[0].startswith("--"):
        flag = argv.pop(0)
        if flag == "--order":
            one_process = True
        elif flag == "--legacy-ledger":
            legacy_ledger = True
        elif flag == "--only":
            only = set(argv.pop(0).split(","))
        else:
            raise SystemExit("unknown flag %r" % flag)
    targets = argv or ["tests.test_repertoire.TestPreparedMoveLookupIsDeterministic"]

    recorded = _stage(only=only)
    import tests.test_setup  # noqa: E402,F401  (FIRST backend-adjacent import by contract)
    if legacy_ledger:
        _legacy_stub_ledger()
    try:
        loader = unittest.TestLoader()
        if one_process or targets == ["discover"]:
            suite = unittest.TestSuite()
            for name in targets:
                if name == "discover":
                    suite = loader.discover("tests", top_level_dir=PROJECT_ROOT)
                else:
                    suite.addTests(loader.loadTestsFromName(name))
        else:
            suite = loader.loadTestsFromNames(targets)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        restored = _restore(recorded)

    print("\npre-fix backend : %s" % PREFIX_BACKEND)
    print("legacy stub ledger reverted: %s" % legacy_ledger)
    for rel, (pre_sha, _real_sha, _backup) in recorded.items():
        print("pre-fix staged  : %-42s sha256=%s" % (rel, pre_sha))
    print("real files restored and verified: %s" % restored)
    print("pre-fix failures: %d errors: %d" % (len(result.failures), len(result.errors)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
