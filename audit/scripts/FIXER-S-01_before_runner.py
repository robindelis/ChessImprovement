#!/usr/bin/env python3
"""
FIXER-S-01 before/after harness -- `tests/test_blunder_identity.py` against the
PRE-FIX `backend/analyzer.py` and `backend/db.py`.

Why a scratch tree instead of swapping files in place: two earlier fixers in
this audit (FIXER-P0, FIXER-N) clobbered a concurrent agent's landed work by
`cp`-ing over a shared repo path, and several other agents are editing
`backend/repertoire.py` / `backend/api.py` right now.  This script never writes
to the repository.  It copies the tree to a temp directory, symlinks the
heavyweight assets (`bin/`, `openings_data/`), substitutes the pre-fix sources
and runs the module there.

Usage
-----
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-S-01_before_runner.py before
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-S-01_before_runner.py after
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-S-01_before_runner.py before all

`before` installs `audit/scripts/FIXER-S-01_prefix_analyzer.py` and
`audit/scripts/FIXER-S-01_prefix_db.py` verbatim, plus a **shim** that supplies
only the NEW public names with exactly the pre-fix behaviour, so the failures
reported are genuine behavioural regressions and not `ImportError`s:

* `UnidentifiableBlunderError`  -- pre-fix nothing raised it.
* `natural_key_of(row)`          -- pre-fix `_upsert_blunder` used the raw
  `(row["game_id"], row["ply"], row["played_move"])` triple with no validation
  whatsoever, so the shim returns it unvalidated and never raises.
* `NEVER_REVIEW_DUE` / `RETIRED_DRILL_STATUS` / `PRACTISED_DRILL_SQL` -- policy
  constants with no pre-fix counterpart; the tests that use them fail because
  nothing retires a card.
* `GameAnalyzer.save_blunders_to_db(blunders, game_ids=None)` -- the shim
  accepts and **ignores** `game_ids`, which is precisely the pre-fix semantics:
  the batch named the only scope, so a surplus of stored rows survived.
* `GameAnalyzer.analyze_and_store(game)` -- pre-fix, no scope was passed.

The shim touches nothing else: `normalize_score`, `analyze_game_openings` and
`_upsert_blunder` keep their pre-fix bodies byte-for-byte.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = "/home/robin/miniconda3/bin/python3"

PREFIX_ANALYZER = os.path.join(REPO, "audit", "scripts", "FIXER-S-01_prefix_analyzer.py")
PREFIX_DB = os.path.join(REPO, "audit", "scripts", "FIXER-S-01_prefix_db.py")

#: Appended to the pre-fix `backend/analyzer.py`.  Every name here is one the
#: new test module imports; each is given the pre-fix behaviour verbatim.
SHIM = '''

# --------------------------------------------------------------------------- #
# FIXER-S-01 before-shim: the NEW public names, with the PRE-FIX behaviour.
# --------------------------------------------------------------------------- #
NEVER_REVIEW_DUE = 9223372036854775807
RETIRED_DRILL_STATUS = "retired"
PRACTISED_DRILL_SQL = (
    "COALESCE(d.attempts, 0) > 0 OR COALESCE(d.successes, 0) > 0 "
    "OR COALESCE(d.last_attempt_date, 0) > 0 "
    "OR COALESCE(d.status, 'new') NOT IN ('new', '')"
)


class UnidentifiableBlunderError(ValueError):
    """Pre-fix: nothing ever raised this, so nothing raises it now."""


def natural_key_of(row):
    """Pre-fix: `_upsert_blunder` used the raw triple, unvalidated."""
    return (row.get("game_id"), row.get("ply"), row.get("played_move"))


_save_blunders_to_db_prefix = GameAnalyzer.save_blunders_to_db


def _save_blunders_to_db(self, blunders, game_ids=None):
    # Pre-fix there was no scope argument; the batch was the only scope.
    return _save_blunders_to_db_prefix(self, blunders)


GameAnalyzer.save_blunders_to_db = _save_blunders_to_db
'''

SKIP = {".git", "node_modules", "__pycache__", "bin", "openings_data",
        "audit", ".env"}


def _copy_tree(destination):
    os.makedirs(destination, exist_ok=True)
    for entry in os.listdir(REPO):
        if entry in SKIP or entry.startswith("."):
            continue
        source = os.path.join(REPO, entry)
        if os.path.isdir(source):
            shutil.copytree(source, os.path.join(destination, entry),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, os.path.join(destination, entry))
    for heavy in ("bin", "openings_data"):
        origin = os.path.join(REPO, heavy)
        if os.path.isdir(origin):
            os.symlink(origin, os.path.join(destination, heavy))


def _install_prefix(scratch):
    shutil.copyfile(PREFIX_ANALYZER, os.path.join(scratch, "backend", "analyzer.py"))
    shutil.copyfile(PREFIX_DB, os.path.join(scratch, "backend", "db.py"))
    with open(os.path.join(scratch, "backend", "analyzer.py"), "a", encoding="utf-8") as fh:
        fh.write(SHIM)
    installed = open(os.path.join(scratch, "backend", "analyzer.py"),
                     encoding="utf-8").read()
    assert "def normalize_score" in installed
    assert "def _reconcile_game_blunders" not in installed, "prefix file is not pre-fix"
    assert "UnidentifiableBlunderError" in installed, "shim not appended"


def _run(scratch, targets):
    command = [PYTHON, "-m", "unittest"] + targets
    print("$ " + " ".join(command[1:]))
    completed = subprocess.run(command, cwd=scratch, env=dict(os.environ))
    return completed.returncode


def main(argv):
    mode = (argv[1] if len(argv) > 1 else "before").lower()
    if mode not in ("before", "after"):
        print(__doc__)
        return 2
    targets = argv[2:] or ["tests.test_blunder_identity"]
    if targets == ["all"]:
        targets = ["discover", "tests"]
    scratch = tempfile.mkdtemp(prefix="fixer_s_before_")
    try:
        _copy_tree(scratch)
        if mode == "before":
            _install_prefix(scratch)
            print("installed PRE-FIX backend/analyzer.py + backend/db.py (+ behaviour shim)")
        else:
            print("running the tree as-is (this patch)")
        code = _run(scratch, targets)
        print("%s RESULT: exit=%d" % (mode.upper(), code))
        return code
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
