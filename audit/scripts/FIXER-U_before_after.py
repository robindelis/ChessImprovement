"""FIXER-U fail-before / pass-after: the same check functions from
`tests/test_api_boundary_hardening.py` run against the PRE-FIX snapshot
(`audit/scripts/api_prefix_snapshot_FIXER-U.py`) and against the patched
`backend.api`, in two SEPARATE processes, because both modules register
exception handlers and middleware on one process-global `FastAPI` instance.

The snapshot is evidence only and is never imported by the app.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # audit/scripts
REPO = os.path.dirname(os.path.dirname(HERE))               # repo root
SNAPSHOT = os.path.join(HERE, "api_prefix_snapshot_FIXER-U.py")

# check name -> True when the check only applies to the patched module
#: Checks that read a symbol the pre-fix module does not have at all, so they
#: are reported as "n/a (pre-fix)" rather than as a failure. Everything else must
#: fail on the snapshot and pass on the patch -- with ONE deliberate exception,
#: `check_local_boundary_still_holds`, which is a CONTROL: FIXER-L already landed
#: that boundary, so it must pass on both sides. It is the evidence that this
#: pass did not regress it.
PATCHED_ONLY = {
    "check_retry_after_agrees_with_the_pool_wait",   # ENGINE_BUSY_RETRY_AFTER is new
}
CONTROL = {
    "check_local_boundary_still_holds",
}


def checks():
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    import tests.test_api_boundary_hardening as mod
    return [c.__name__ for c in mod.CHECKS]


def run_child(module_path):
    """One child process, one module under test, one JSON line per check.

    Separate processes because `tests.test_setup` and both api modules register
    exception handlers, middleware and process-wide singletons on import.
    """
    if module_path.endswith("api_prefix_snapshot_FIXER-U.py"):
        loader = ("import importlib.util as _u; "
                  f"_s = _u.spec_from_file_location('pre_fix_api', {SNAPSHOT!r}); "
                  "mod = _u.module_from_spec(_s); _s.loader.exec_module(mod)")
    else:
        loader = f"import {module_path} as mod"
    code = f'''
import json, sys
sys.path.insert(0, {REPO!r})
import tests.test_setup
{loader}
import tests.test_api_boundary_hardening as suite
out = {{}}
for check in suite.CHECKS:
    try:
        check(mod)
        out[check.__name__] = None
    except Exception as exc:
        out[check.__name__] = f"{{type(exc).__name__}}: {{str(exc)[:400]}}"
    finally:
        suite.purge_markers()
print("@@RESULT@@" + json.dumps(out))
'''
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                          capture_output=True, text=True, timeout=1800)
    for line in proc.stdout.splitlines():
        if line.startswith("@@RESULT@@"):
            return json.loads(line[len("@@RESULT@@"):]), proc.stderr
    return {}, proc.stdout + proc.stderr


def main():
    print(f"pre-fix snapshot: {os.path.relpath(SNAPSHOT, REPO)} "
          f"({os.path.getsize(SNAPSHOT)} bytes)")
    before, before_err = run_child("api_prefix_snapshot_FIXER-U.py")
    after, after_err = run_child("backend.api")
    if not before or not after:
        print("BEFORE child produced no result:\n", before_err[-3000:])
        print("AFTER child produced no result:\n", after_err[-3000:])
        return 2
    names = checks()
    rows, before_pass, after_pass = [], 0, 0
    for name in names:
        b, a = before.get(name, "<not run>"), after.get(name, "<not run>")
        if name in PATCHED_ONLY:
            rows.append((name, "n/a", "PASS" if a is None else "FAIL", a))
            continue
        rows.append((name,
                     "PASS" if b is None else "FAIL",
                     "PASS" if a is None else "FAIL",
                     b if b is not None else a))
        if name not in PATCHED_ONLY:
            before_pass += b is None
            after_pass += a is None
    counted = len(names) - len(PATCHED_ONLY)
    width = max(len(r[0]) for r in rows)
    print()
    print(f"{'check'.ljust(width)}  BEFORE  AFTER   detail (before if it failed, else after)")
    print("-" * (width + 60))
    for name, b, a, detail in rows:
        tag = "  [CONTROL: passes both ways by design]" if name in CONTROL else ""
        print(f"{name.ljust(width)}  {b:6s}  {a:5s}   {str(detail)[:130]}{tag}")
    print("-" * (width + 60))
    print(f"({len(PATCHED_ONLY)} check(s) are n/a on the pre-fix module and are not counted.)")
    print(f"BEFORE: {before_pass} passed / {counted - before_pass} failed")
    print(f"AFTER : {after_pass} passed / {counted - after_pass} failed")
    return 0 if after_pass == counted else 1


if __name__ == "__main__":
    sys.exit(main())
