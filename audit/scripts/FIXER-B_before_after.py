"""
FIXER-B fail-before / pass-after evidence.

Runs the exact same contract checks used by `tests/test_api_contract.py` against
TWO modules in one process:

  BEFORE : audit/scripts/api_prefix_snapshot.py  -- `backend/api.py` exactly as it
           was immediately before FIXER-B's patch (rebuilt by
           audit/scripts/FIXER-B_make_prefix_snapshot.py)
  AFTER  : backend/api.py                        -- the patched module

Isolated temp DB: this script imports `tests.test_setup` first, which points
`CHESS_DB_PATH` at a throwaway copy. It NEVER touches chess_improvement.db and
NEVER talks to http://127.0.0.1:38281.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-B_before_after.py
"""
import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
# `audit/scripts` is the script dir, so make the project root importable as `tests.*`
if "" not in sys.path:
    sys.path.insert(0, "")

import tests.test_setup  # MUST be first -- isolated temp SQLite DB

import backend.api as after_module
from tests.test_api_contract import CHECKS

SNAPSHOT = os.path.join(HERE, "api_prefix_snapshot.py")


def load_snapshot():
    spec = importlib.util.spec_from_file_location("api_prefix_snapshot", SNAPSHOT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_prefix_snapshot"] = mod
    spec.loader.exec_module(mod)
    return mod


def run_suite(module, label):
    passed, failed = [], []
    for name, fn in CHECKS:
        try:
            fn(module)
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"  [FAIL] {label}  {name}")
            print(f"         -> {type(exc).__name__}: {exc}")
        else:
            passed.append(name)
            print(f"  [ ok ] {label}  {name}")
    return passed, failed


def main():
    before_module = load_snapshot()
    print("=" * 100)
    print("BEFORE  (pre-fix snapshot: audit/scripts/api_prefix_snapshot.py)")
    print("=" * 100)
    before_ok, before_bad = run_suite(before_module, "BEFORE")

    print()
    print("=" * 100)
    print("AFTER   (patched: backend/api.py)")
    print("=" * 100)
    after_ok, after_bad = run_suite(after_module, "AFTER ")

    print()
    print("=" * 100)
    print(f"BEFORE: {len(before_ok)} passed / {len(before_bad)} failed")
    print(f"AFTER : {len(after_ok)} passed / {len(after_bad)} failed")
    print("=" * 100)
    newly_fixed = [n for n, _ in before_bad if n in set(after_ok)]
    regressed = [n for n, _ in after_bad]
    print(f"NEWLY FIXED by the patch ({len(newly_fixed)}):")
    for n in newly_fixed:
        print(f"  + {n}")
    print(f"STILL FAILING after the patch ({len(regressed)}):")
    for n in regressed:
        print(f"  ! {n}")
    return 0 if not regressed else 1


if __name__ == "__main__":
    sys.exit(main())
