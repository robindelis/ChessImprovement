"""
CODE-05 repro #2: prove tests/test_timeline_spam.py's 4 "spam resistance" tests are
FALSE POSITIVES — they execute against a hand-written JavaScript mock embedded in the
Python file (tests/test_timeline_spam.py:58-170), NOT against frontend/board.js or
frontend/repertoire_builder.js.

Proof strategy: re-point the test module's FRONTEND_DIR at a scratch copy of frontend/
in which board.js and repertoire_builder.js have been replaced with EMPTY files and the
vendor dir is symlinked. If the tests still pass, the production frontend is irrelevant.

Safe: never writes into the repo; works entirely under /tmp/opencode.

Run:  /home/robin/miniconda3/bin/python3 audit/scripts/CODE-05_repro_timeline_false_positive.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRATCH = os.path.join("/tmp/opencode", "code05_frontend_sabotage")

# 1. Build a sabotaged copy of frontend/
shutil.rmtree(SCRATCH, ignore_errors=True)
os.makedirs(SCRATCH)
real_frontend = os.path.join(PROJECT_ROOT, "frontend")
for name in os.listdir(real_frontend):
    src = os.path.join(real_frontend, name)
    dst = os.path.join(SCRATCH, name)
    if os.path.isdir(src):
        os.symlink(src, dst)          # vendor/, assets/ stay real
    else:
        shutil.copy2(src, dst)

# 2. Delete the two production modules under test
for target in ("board.js", "repertoire_builder.js"):
    with open(os.path.join(SCRATCH, target), "w", encoding="utf-8") as fh:
        fh.write("// CODE-05: production module DELETED for this experiment\n")
print(f"sabotaged frontend copy: {SCRATCH}")
print("  board.js               ->", os.path.getsize(os.path.join(SCRATCH, 'board.js')), "bytes")
print("  repertoire_builder.js  ->", os.path.getsize(os.path.join(SCRATCH, 'repertoire_builder.js')), "bytes")
print(f"  real board.js is still {os.path.getsize(os.path.join(real_frontend,'board.js'))} bytes on disk (untouched)\n")

# 3. Run the real tests against the sabotaged copy
probe = f'''
import sys, os, unittest
sys.path.insert(0, {PROJECT_ROOT!r})
import tests.test_setup
import tests.test_timeline_spam as m
m.FRONTEND_DIR = {SCRATCH!r}          # <-- the only change
suite = unittest.TestLoader().loadTestsFromModule(m)
r = unittest.TextTestRunner(verbosity=2, stream=open(os.devnull,"w")).run(suite)
print("tests_run:", r.testsRun)
print("failures :", len(r.failures))
print("errors   :", len(r.errors))
for t, tb in r.failures + r.errors:
    print("  FAIL", t.id(), "->", tb.strip().splitlines()[-1])
'''
res = subprocess.run(["/home/robin/miniconda3/bin/python3", "-c", probe],
                     capture_output=True, text=True, cwd=PROJECT_ROOT)
print(res.stdout)
if res.returncode != 0:
    print("STDERR:", res.stderr[-3000:])
