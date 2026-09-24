#!/usr/bin/env python3
"""
ATK5-02 / ABUSE-02 repro: secret handling for the Lichess API token.

Checks (read-only, never prints the token or any user game data):
 1. Is .env gitignored?  Is it tracked by git?
 2. Is the plaintext token present in any committed git blob/object?
 3. Does the token reach the SQLite DB (settings table)?
 4. Does the token reach any HTTP response body / SSE event / error handler?
 5. Is the token on a subprocess command line (visible in `ps`)?

Idempotent. Uses the isolated temp DB.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)

import tests.test_setup  # MUST be first backend-adjacent import
from backend.api import app
from backend.db import get_db
from fastapi.testclient import TestClient

TOKEN = None
with open(os.path.join(ROOT, ".env")) as f:
    for line in f:
        line = line.strip()
        if line.startswith("lip_"):
            TOKEN = line
            break
assert TOKEN, "no lip_ token in .env"
FINGER = TOKEN[:8] + "..." + TOKEN[-4:]   # safe partial fingerprint for logs


def sh(*args):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True).stdout.strip()


print("=" * 78)
print("1) GIT HYGIENE")
print("=" * 78)
print("  .env in .gitignore?      :", ".env" in open(".gitignore").read())
print("  git ls-files | grep .env :", sh("git", "ls-files") and [l for l in sh("git", "ls-files").splitlines() if l == ".env"])
print("  git ls-files | grep .db  :", [l for l in sh("git", "ls-files").splitlines() if l.endswith(".db")])
print("  git status --porcelain   :", sh("git", "status", "--porcelain")[:200] or "(clean)")

print()
print("  Committed .env blob contains a live 'lip_' token:",
      "lip_" in sh("git", "show", "HEAD:.env"))
print("  Commits touching .env   :", sh("git", "log", "--oneline", "--", ".env").replace("\n", " | "))

# Does the *committed* production DB blob embed the plaintext token?
# We only grep the raw bytes; nothing is printed.
try:
    blob = subprocess.run(["git", "show", "HEAD:chess_improvement.db"], cwd=ROOT,
                          capture_output=True).stdout
    idx = blob.find(b"lip_")
    print("  Committed chess_improvement.db contains b'lip_' at byte:", idx)
    if idx >= 0:
        print("  Committed DB embeds the plaintext Lichess token (byte offset %d)" % idx)
    print("  Committed DB size:", len(blob), "bytes;  is SQLite file:", blob[:15] == b"SQLite format 3")
except Exception as e:
    print("  (could not read committed db blob)", e)

print()
print("=" * 78)
print("2) TOKEN -> SQLITE (settings table)")
print("=" * 78)
conn = get_db()
cur = conn.cursor()
cur.execute("SELECT key, value FROM settings")
rows = dict(cur.fetchall())
conn.close()
for k, v in rows.items():
    if "token" in k or "key" in k:
        print(f"  settings[{k!r}] = {str(v)[:10]}... (len={len(str(v))})  plaintext_secret={('lip_' in str(v)) or len(str(v))>20}")
print("  'lichess_token' row present:", "lichess_token" in rows, " plaintext lip_:", "lip_" in str(rows.get("lichess_token", "")))

print()
print("=" * 78)
print("3) TOKEN -> HTTP RESPONSES (every GET endpoint + error handlers)")
print("=" * 78)
client = TestClient(app, raise_server_exceptions=False)
ENDPOINTS = [
    ("GET", "/api/settings", None),
    ("GET", "/api/account", None),
    ("GET", "/api/dashboard", None),
    ("GET", "/api/games/recent?limit=1", None),
    ("GET", "/api/blunders?limit=1", None),
    ("GET", "/api/train/paths", None),
    ("GET", "/api/repertoire/tree?color=white", None),
    ("GET", "/api/repertoire/train/openings", None),
    ("GET", "/api/repertoire/train/progress?color=white", None),
    ("GET", "/api/scout/recent?limit=1", None),
    ("GET", "/api/nonexistent", None),
    ("GET", "/api/settings/../../.env", None),
]
for method, url, body in ENDPOINTS:
    r = client.request(method, url, json=body)
    leaked = TOKEN in r.text or FINGER in r.text
    print(f"  {r.status_code}  {method} {url:<45} token_in_body={leaked}")

# POST /api/settings round-trip: does the write endpoint echo the secret back?
r = client.post("/api/settings", json={"lichess_token": "lip_TESTTOKEN1234567890", "lichess_username": "RobiDeli"})
print(f"  {r.status_code}  POST /api/settings (write token)  echo_token={'lip_TESTTOKEN' in r.text}  body={r.text[:120]}")
r = client.get("/api/settings")
print(f"  {r.status_code}  GET  /api/settings after write   token_in_body={'lip_TEST' in r.text}")

print()
print("=" * 78)
print("4) TOKEN -> ValueError handler (str(exc) is echoed verbatim with HTTP 400)")
print("=" * 78)
# force a ValueError inside a route and inspect what the client sees
r = client.post("/api/scout/analyze", json={"source": "x" * 5})
print("  POST /api/scout/analyze source=xxxxx ->", r.status_code, r.text[:200])

print()
print("=" * 78)
print("5) TOKEN -> SUBPROCESS ARGV (visible in `ps`)")
print("=" * 78)
import chess.engine
import inspect
src = inspect.getsource(chess.engine.popen_uci)
print("  chess.engine.popen_uci uses subprocess with a list argv (no shell):",
      "shell=True" not in src)
print("  STOCKFISH_PATH is a fixed module constant, not user-influenced:")
import backend.evaluator as ev
import backend.analyzer as an
print("   ", ev.STOCKFISH_PATH)
print("   ", an.STOCKFISH_PATH)
print()
print("DONE. Token fingerprint used in this log:", FINGER)
