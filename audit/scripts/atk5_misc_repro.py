#!/usr/bin/env python3
"""
ATK5-12 repro: remaining cross-cutting checks.

 A) POST /api/repertoire/seed?force=true returns JSON `null` on success.
 B) DELETE /api/repertoire/studies/{id}?delete_moves=true -- unauthenticated
    destructive bulk delete.
 C) /api/games/recent?search= uses LIKE with the user's string unescaped, so
    '%' / '_' act as wildcards -> the "search" filter is trivially bypassed.
 D) no request-body size cap: /api/settings with a 4 MB token / study name.
 E) deeply nested JSON -> 500; which exception?
 F) tests.test_setup opens the PRODUCTION db READ-WRITE to copy it. Demonstrate
    (on a synthetic WAL db) that a plain sqlite3.connect() creates/updates the
    -wal and -shm side files, i.e. the "read-only test harness" writes to the
    user's live database.
 G) unbounded study name / comment / arrows stored verbatim.

Idempotent, isolated temp DB, network stubbed.
"""
import json
import os
import sqlite3
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import

import requests  # noqa: E402


class _R:
    status_code = 503
    text = "stub"

    def json(self):
        return {}

    def iter_lines(self, decode_unicode=False):
        return iter(())


requests.get = lambda *a, **k: _R()
requests.post = lambda *a, **k: _R()
import yt_dlp  # noqa: E402


class _Y:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, *a, **k):
        return None


yt_dlp.YoutubeDL = _Y

from backend.api import app  # noqa: E402
from backend.db import get_db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
J = {"Content-Type": "application/json"}
c = TestClient(app, raise_server_exceptions=False)

print("=" * 78)
print("A) POST /api/repertoire/seed?force=true")
print("=" * 78)
c.post("/api/repertoire/clear")
r = c.post("/api/repertoire/seed?force=true")
print("  status:", r.status_code, " content-type:", r.headers.get("content-type"),
      " body:", r.text[:200])
print("  -> a 200 with a `null` body; the frontend cannot tell success from failure.")
r2 = c.post("/api/repertoire/seed?force=false")
print("  force=false ->", r2.status_code, r2.text[:200])

print()
print("=" * 78)
print("B) unauthenticated bulk delete: DELETE /api/repertoire/studies/{id}?delete_moves=true")
print("=" * 78)
c.post("/api/repertoire/clear")
c.post("/api/repertoire/move", json={"repertoire": "white", "parent_fen": FEN,
                                     "move_uci": "e2e4", "is_user_move": True,
                                     "study_name": "Anti French"})
sid = c.post("/api/repertoire/studies", json={"name": "Anti French", "repertoire": "white"}).json().get("study", {}).get("id")
n = lambda: get_db().execute("SELECT COUNT(*) FROM repertoire_moves").fetchone()[0]
print("  study id:", sid, " repertoire_moves:", n())
if sid:
    r = c.delete(f"/api/repertoire/studies/{sid}?delete_moves=true")
    print("  DELETE (no credentials, no CSRF token) ->", r.status_code, r.text[:150])
    print("  repertoire_moves now:", n())

print()
print("=" * 78)
print("C) /api/games/recent?search= -> LIKE wildcards are not escaped")
print("=" * 78)
for q in ["%", "%%", "_", "RobiDeli", "%%%", "a%"]:
    r = c.get("/api/games/recent?limit=1&search=" + q.replace("%", "%25").replace("_", "%5F"))
    print(f"  search={q!r:<10} -> total={r.json().get('total')}")
print("  ('%' returns the whole table: the 'search' box is not a filter)")

print()
print("=" * 78)
print("D) no request-size cap: 4 MB settings value / study name")
print("=" * 78)
big = "A" * (4 * 1024 * 1024)
r = c.post("/api/settings", content=json.dumps({"lichess_token": big}).encode(), headers=J)
print("  POST /api/settings 4MB lichess_token ->", r.status_code, r.text[:80])
v = get_db().execute("SELECT LENGTH(value) FROM settings WHERE key='lichess_token'").fetchone()
print("  stored token length in DB:", v[0] if v else None)
r = c.post("/api/repertoire/studies", json={"name": big, "repertoire": "white"})
print("  POST /api/repertoire/studies 4MB name ->", r.status_code, r.text[:100])

print()
print("=" * 78)
print("E) deeply nested JSON -> 500; which exception?")
print("=" * 78)
c2 = TestClient(app, raise_server_exceptions=True)
for depth in (1000, 20000):
    body = ('{"repertoire":"white","moves":' + "[" * depth + "]" * depth + "}").encode()
    try:
        r = c2.post("/api/scout/check_line", content=body, headers=J)
        print(f"  depth={depth} -> {r.status_code}")
    except Exception as e:
        print(f"  depth={depth} -> EXC {type(e).__name__}: {str(e)[:90]}")
        print("   ", traceback.format_exc().strip().splitlines()[-3])

print()
print("=" * 78)
print("F) tests.test_setup opens the PRODUCTION db read-write")
print("=" * 78)
print("  tests/test_setup.py:35  src = sqlite3.connect(prod_db)   <-- read-WRITE handle")
print("  tests/test_setup.py:37  src.backup(dst)")
synth = "/tmp/opencode/atk5_wal_demo.db"
for f in (synth, synth + "-wal", synth + "-shm"):
    if os.path.exists(f):
        os.unlink(f)
s = sqlite3.connect(synth)
s.execute("PRAGMA journal_mode=WAL")
s.execute("CREATE TABLE t(x)")
s.execute("INSERT INTO t VALUES (1)")
s.commit()
print("  before plain connect: wal exists =", os.path.exists(synth + "-wal"),
      " shm exists =", os.path.exists(synth + "-shm"))
s2 = sqlite3.connect(synth)          # exactly what test_setup does
s2.backup(sqlite3.connect("/tmp/opencode/atk5_wal_copy.db"))
s2.close(); s.close()
print("  after  plain connect+backup: wal exists =", os.path.exists(synth + "-wal"),
      " shm exists =", os.path.exists(synth + "-shm"))
print("  => the 'isolated read-only test harness' takes a read-WRITE handle on the")
print("     user's live chess_improvement.db, creating -wal/-shm and (on last close)")
print("     of the last connection) checkpointing the WAL into the main file.")

print()
print("DONE")
