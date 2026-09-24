"""FIXER-L before/after runner.

Imports the pre-fix `backend/api.py` snapshot
(`audit/scripts/api_prefix_snapshot_FIXER-L.py`) and the patched `backend.api`
into ONE process and runs the SAME check functions from
`tests/test_api_security.py` against both, printing a fail-before / pass-after
table.

Usage:
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-L_before_after.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # <repo root>
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tests.test_setup  # noqa: E402  (must follow the sys.path insert; isolates CHESS_DB_PATH)

import tests.test_api_security as checks  # noqa: E402


def _load_prefix():
    path = os.path.join(HERE, "api_prefix_snapshot_FIXER-L.py")
    spec = importlib.util.spec_from_file_location("api_prefix_snapshot_FIXER_L", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(module):
    results = []
    for check in checks.CHECKS:
        try:
            check(module)
            results.append((check.__name__, None))
        except Exception as exc:
            message = str(exc).strip().splitlines()
            head = message[0] if message else type(exc).__name__
            results.append((check.__name__, f"{type(exc).__name__}: {head}"[:220]))
    return results


def main():
    prefix = _load_prefix()
    import backend.api as patched

    print(f"pre-fix snapshot : {prefix.__file__}")
    print(f"patched module  : {patched.__file__}")
    print()
    before = _run(prefix)
    after = _run(patched)

    name_width = max(len(n) for n, _ in before) + 2
    print(f"{'CHECK'.ljust(name_width)} {'BEFORE (pre-fix api.py)'.ljust(48)} AFTER")
    print("-" * (name_width + 78))
    for (name, before_error), (_, after_error) in zip(before, after):
        verdict = "PASS" if after_error is None else "FAIL"
        cell = ("ok" if before_error is None else before_error)
        print(f"{name.ljust(name_width)} {cell[:46].ljust(48)} {verdict}"
              + ("" if after_error is None else f"  <- {after_error}"))

    before_pass = sum(1 for _, err in before if err is None)
    after_pass = sum(1 for _, err in after if err is None)
    print()
    print(f"BEFORE: {before_pass} passed / {len(before) - before_pass} failed")
    print(f"AFTER : {after_pass} passed / {len(after) - after_pass} failed")
    return 0 if after_pass == len(after) else 1


if __name__ == "__main__":
    raise SystemExit(main())
