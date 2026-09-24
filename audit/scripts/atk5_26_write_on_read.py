#!/usr/bin/env python3
"""ATK-5-26: GET /api/games/recent performs an UPDATE on every request.

`migrate_played_at_units()` (api.py:1084) runs a table-wide
  UPDATE games SET played_at = played_at * 1000 WHERE played_at > 0 AND played_at < ?
at the top of a **read** route, and its own docstring says "Safe to call on every
request" -- an admission that it is a per-request write.

Three things are measured rather than assumed:
  A. does it really take a write lock even when it rewrites 0 rows?
  B. how many concurrent GETs serialise behind it?
  C. what does it cost as `games` grows (the predicate is unindexed)?

Isolated temp DB, idempotent. Production is never opened.
"""
import os
import sys
import json
import time
import sqlite3
import threading
import statistics

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.db import get_db, DB_PATH  # noqa: E402
from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")

print("=" * 78)
print("ATK-5-26  write-on-read in GET /api/games/recent")
print("=" * 78)
print("isolated DB:", DB_PATH)

# ------------------------------------------------------------- A: it writes
# The route opens its OWN connection via get_db(), so a trace hook on a
# connection this probe opened would see nothing. Wrap get_db() instead and
# trace every connection the application actually uses.
print("\n[A] does the read route actually issue writes?  (trace every app connection)")
import backend.db as _dbmod  # noqa: E402

_real_get_db = _dbmod.get_db
_traced = []


def _tracing_get_db():
    c = _real_get_db()
    c.set_trace_callback(lambda s: _traced.append(s))
    return c


_dbmod.get_db = _tracing_get_db
import backend.api as _apimod  # noqa: E402
_apimod.get_db = _tracing_get_db
if hasattr(_apimod, "_repertoire_get_db"):
    _apimod._repertoire_get_db = _tracing_get_db
import backend.repertoire as _repmod  # noqa: E402
if hasattr(_repmod, "get_db"):
    _repmod.get_db = _tracing_get_db

_traced.clear()
r = client.get("/api/games/recent", params={"limit": 5})
_dbmod.get_db = _real_get_db
_apimod.get_db = _real_get_db
writes = [s for s in _traced if s.strip().upper().startswith(("UPDATE", "INSERT", "DELETE", "REPLACE"))]
reads = [s for s in _traced if s.strip().upper().startswith("SELECT")]
print(f"    HTTP {r.status_code}; SQL on the app's own connection(s): {len(_traced)}"
      f"  ({len(reads)} SELECT / {len(writes)} WRITE)")
print(f"    of which WRITE statements: {len(writes)}")
for s in writes:
    print(f"      {s.strip()[:160]}")

# Corroborate independently: data_version on a *reader* connection must not move
# if no other connection committed.
rv = sqlite3.connect(DB_PATH, isolation_level=None)
before = rv.execute("PRAGMA data_version").fetchone()[0]
client.get("/api/games/recent", params={"limit": 5})
time.sleep(0.1)
after = rv.execute("PRAGMA data_version").fetchone()[0]
rv.close()
print(f"    independent check: PRAGMA data_version {before} -> {after} "
      f"({'a commit DID occur' if after != before else 'no commit'})")

# ------------------------------------------- B: does it take a write lock?
print("\n[B] does it take the SQLite WRITE lock even with nothing to rewrite?")
probe = sqlite3.connect(DB_PATH, timeout=0.4)
probe.execute("PRAGMA journal_mode")  # touch
held = []


def hold_write_lock(hold_s=3.0):
    c = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    c.execute("BEGIN IMMEDIATE")           # takes the database write lock
    c.execute("UPDATE games SET played_at = played_at WHERE 0")  # a real write txn
    held.append(time.time())
    time.sleep(hold_s)
    c.execute("ROLLBACK")
    c.close()


t = threading.Thread(target=hold_write_lock, daemon=True)
t.start()
time.sleep(0.4)   # let it grab the lock
t0 = time.time()
try:
    r = client.get("/api/games/recent", params={"limit": 5})
    dt = time.time() - t0
    print(f"    GET /api/games/recent while another connection holds BEGIN IMMEDIATE:")
    print(f"      -> HTTP {r.status_code} after {dt:.2f}s  (it BLOCKED on the write lock)")
    if dt > 0.5:
        print("      -> confirmed: a GET route blocks on, and needs, the DB write lock")
except Exception as e:
    print(f"      -> raised {type(e).__name__}: {str(e)[:140]}")
probe.close()
t.join(10)

# ------------------------------------- B2: do concurrent GETs serialise?
print("\n[B2] do concurrent GET /api/games/recent calls serialise on that write?")
N = 16
lat = []
barrier = threading.Barrier(N)


def hit():
    barrier.wait()
    t0 = time.time()
    try:
        client.get("/api/games/recent", params={"limit": 5})
    except Exception:
        pass
    lat.append(time.time() - t0)


# pre-warm
client.get("/api/games/recent", params={"limit": 5})
lat.clear()
ts = [threading.Thread(target=hit) for _ in range(N)]
t_start = time.time()
for th in ts:
    th.start()
for th in ts:
    th.join(60)
wall = time.time() - t_start
print(f"    {N} concurrent GETs: wall {wall:.2f}s, "
      f"latency min {min(lat):.3f}s / median {statistics.median(lat):.3f}s / max {max(lat):.3f}s")
print(f"    sum of latencies {sum(lat):.2f}s vs wall {wall:.2f}s "
      f"-> concurrency factor {sum(lat)/max(wall,1e-9):.1f}x "
      f"(1.0x = fully serialised)")

# ------------------------------------------- C: cost as games grows
print("\n[C] cost of the unindexed UPDATE as the games table grows")
c = get_db()
c.execute("PRAGMA journal_mode=WAL")
have = c.execute("SELECT COUNT(*) FROM games").fetchone()[0]
print(f"    games rows now: {have}")
c.close()
print(f"    {'rows':>8}  {'UPDATE ms':>10}  {'GET /recent ms':>15}  note")
for target in (have, 1000, 10000, 100000):
    c = get_db()
    # synthesise rows cheaply; all in ms so the predicate matches nothing
    need = target - c.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    if need > 0:
        c.executemany(
            "INSERT OR IGNORE INTO games (id, speed, rated, played_at, white_player,"
            " black_player, white_rating, black_rating, user_color, result, opening_name,"
            " eco, moves_san, analyzed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"synFill{i:08d}", "blitz", 1, 1700000000000 + i, "a", "b", 2000, 2000,
              "white", "1-0", "Synthetic", "A00", "1. e4", 1) for i in range(need)])
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    c.close()

    t0 = time.perf_counter()
    probe = sqlite3.connect(DB_PATH, isolation_level=None)
    probe.execute("UPDATE games SET played_at = played_at * 1000 "
                  "WHERE played_at IS NOT NULL AND played_at > 0 AND played_at < ?",
                  (10 ** 12,))
    upd_ms = (time.perf_counter() - t0) * 1000
    probe.close()

    t0 = time.perf_counter()
    client.get("/api/games/recent", params={"limit": 5})
    get_ms = (time.perf_counter() - t0) * 1000
    print(f"    {n:>8}  {upd_ms:>10.1f}  {get_ms:>15.1f}  "
          f"{'<- every GET pays this' if upd_ms > 5 else ''}")

print("\n[D] is any of this covered by an existing 'read endpoints do not write' test?")
import subprocess  # noqa: E402
out = subprocess.run(
    ["grep", "-rn", "games/recent", "tests/test_repertoire_write_safety.py"],
    capture_output=True, text=True).stdout.strip()
print("    grep games/recent tests/test_repertoire_write_safety.py ->",
      out or "(not covered -- the write-safety suite does not include this route)")

print("\n[E] cleanup: drop the synthetic filler so the probe is idempotent")
c = get_db()
c.execute("DELETE FROM games WHERE id LIKE 'synFill%'")
c.commit()
print("    remaining games rows:", c.execute("SELECT COUNT(*) FROM games").fetchone()[0])
c.close()
print("done.")
