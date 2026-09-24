#!/usr/bin/env python3
"""ATK-5 re2-6: the isolation invariant (AGENTS.md 9) against the NEWEST code.

Many modules and tests have been added since the invariant was established.  This
re-verifies it three ways:

  A. STATIC: does any module outside tests/ open the production DB, or derive a
     DB path from an env var that can be unset (the CODE-05-10 pattern that
     `probe_guard.require_isolated_db()` exists to prevent)?
  B. RUNTIME: after importing every backend module, is the app still pointed at a
     temp DB, and is production still byte-identical?
  C. Does any raw `sqlite3.connect` in tests/ bypass `probe_guard`?

Production is only ever read with a plain byte read (fingerprint), never opened
as a SQLite connection.

Read-only; idempotent.
"""
import os
import sys
import ast
import glob
import json
import hashlib
import sqlite3
import tempfile

REPO = "/home/robin/Projects/ChessImprovement"
os.chdir(REPO)
sys.path.insert(0, REPO)

print("=" * 78)
print("ATK-5 re2-6  isolation invariant re-verification")
print("=" * 78)
PROD = os.path.join(REPO, "chess_improvement.db")


def fingerprint(path):
    """Plain byte read. Opens NO SQLite connection (AGENTS.md 9.1)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"size": st.st_size, "mtime": st.st_mtime, "sha256": h.hexdigest()}


before = fingerprint(PROD)
print(f"\nproduction BEFORE: {json.dumps(before, indent=None)[:200]}")

# ---------------------------------------------------------------- A: static
print("\n" + "=" * 78)
print("[A] STATIC analysis")
print("=" * 78)
print("\n[A1] modules outside tests/ that mention the production filename:")
hits = []
for path in sorted(glob.glob("backend/*.py") + glob.glob("*.py")
                   + glob.glob("electron/*.js") + glob.glob("frontend/*.js")):
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    if "chess_improvement.db" in src:
        for i, line in enumerate(src.splitlines(), 1):
            if "chess_improvement.db" in line:
                hits.append((path, i, line.strip()[:110]))
if hits:
    for p, i, l in hits:
        print(f"    {p}:{i}  {l}")
else:
    print("    (none)")

print("\n[A2] the CODE-05-10 pattern -- a DB path defaulted to production:")
pat = []
for path in sorted(glob.glob("backend/*.py") + glob.glob("*.py")
                   + glob.glob("tests/*.py") + glob.glob("audit/scripts/*.py")):
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if "chess_improvement.db" in s and "environ" in s and "get(" in s:
            pat.append((path, i, s[:120]))
        if re.match(r'.*DB_PATH\s*=\s*os\.environ\.get\(\s*["\']CHESS_DB_PATH["\']\s*\)\s*$', s) if (re := __import__("re")) else False:
            pat.append((path, i, s[:120]))
if pat:
    for p, i, l in pat:
        print(f"    <<<< {p}:{i}  {l}")
else:
    print("    (none) -- no env-defaulted-to-production DB path")

print("\n[A3] every raw sqlite3.connect / open(db) in tests/ and audit/scripts/:")
rows = []
for path in sorted(glob.glob("tests/*.py") + glob.glob("audit/scripts/*.py")):
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except (OSError, SyntaxError):
        continue
    src = open(path, encoding="utf-8", errors="replace").read()
    uses_guard = "require_isolated_db" in src or "probe_guard" in src
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name == "connect" and "sqlite3" in ast.dump(fn):
                rows.append((path, node.lineno, "sqlite3.connect", uses_guard))
            elif name in ("open_production_readonly", "fingerprint", "require_isolated_db",
                          "open_isolated_db", "require_no_live_http"):
                rows.append((path, node.lineno, f"probe_guard.{name}", uses_guard))
rawc = [r for r in rows if r[2] == "sqlite3.connect"]
guardless = [r for r in rawc if not r[3]]
print(f"    sqlite3.connect call sites : {len(rawc)}")
print(f"    ...in files WITHOUT probe_guard: {len(guardless)}")
for p, l, k, g in guardless[:25]:
    print(f"      <<<< {p}:{l}")
for p, l, k, g in rawc[:12]:
    if g:
        print(f"      ok   {p}:{l}  (file imports probe_guard)")

print("\n[A4] live-HTTP calls aimed at the production port, in tests/:")
bad = []
for path in sorted(glob.glob("tests/*.py") + glob.glob("audit/scripts/*.py")):
    src = open(path, encoding="utf-8", errors="replace").read()
    for i, line in enumerate(src.splitlines(), 1):
        if "38281" in line and ("requests." in line or "urlopen" in line or "http://" in line):
            if "127.0.0.1:38281" in line or "localhost:38281" in line:
                bad.append((path, i, line.strip()[:110]))
if bad:
    for p, i, l in bad:
        print(f"    <<<< {p}:{i}  {l}")
else:
    print("    (none)")

# --------------------------------------------------------------- B: runtime
print("\n" + "=" * 78)
print("[B] RUNTIME: import every backend module, then check the DB path")
print("=" * 78)
import tests.test_setup  # noqa: E402  (FIRST, redirects to a temp DB)
import backend.db  # noqa: E402

print(f"    CHESS_DB_PATH = {os.environ.get('CHESS_DB_PATH')}")
assert os.path.basename(os.environ.get("CHESS_DB_PATH", "")).startswith("chess_test_")
assert os.path.abspath(os.environ["CHESS_DB_PATH"]) != os.path.abspath(PROD)
import backend.api  # noqa: E402
import backend.repertoire  # noqa: E402
import backend.evaluator  # noqa: E402
import backend.analyzer  # noqa: E402
import backend.scout  # noqa: E402
import backend.openings  # noqa: E402
import backend.lichess_client  # noqa: E402

print(f"    backend.db.get_db_path() = {backend.db.get_db_path()}")
print(f"    -> still a temp file: "
      f"{os.path.abspath(backend.db.get_db_path()) != os.path.abspath(PROD)}")
# and the connection it hands out really is the temp one
_c = backend.db.get_db()
try:
    rows = _c.execute("PRAGMA database_list").fetchall()
    print(f"    PRAGMA database_list -> {[dict(r) for r in rows]}")
finally:
    _c.close()

print("\n" + "=" * 78)
print("[C] production fingerprint AFTER (must be identical)")
print("=" * 78)
after = fingerprint(PROD)
print(f"    production AFTER : {json.dumps(after)[:200]}")
print(f"    IDENTICAL        : {before == after}")
for side in ("-wal", "-shm"):
    p = PROD + side
    print(f"    {os.path.basename(p):24} exists: {os.path.exists(p)}")
print(f"\n    temp dir now holds: "
      f"{len([f for f in os.listdir(tempfile.gettempdir()) if f.startswith('chess_test_')])} "
      f"chess_test_* file(s)")
print("done.")
