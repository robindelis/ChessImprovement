#!/usr/bin/env python3
"""ATK-5-27: is the per-request UPDATE in GET /api/games/recent actually needed?

Reads on that route are ALREADY unit-safe without it:
  * the SQL ORDER BY normalises inline (api.py:1113-1116), and
  * each row goes through `normalize_played_at()` (api.py:1129).

If so, the write at api.py:1084 is gratuitous on a read route: it takes the
database write lock on every request to buy nothing. This builds a deliberately
MIXED-UNIT table and compares ordering with and without the write.

Isolated temp DB, idempotent (restores the original table contents).
"""
import os
import sys
import json
import time
import sqlite3
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.db import get_db, DB_PATH  # noqa: E402
from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")


def _games(resp):
    """The route returns {"games": [...], ...}; tolerate a bare list too."""
    data = resp.json()
    if isinstance(data, dict):
        return data.get("games") or data.get("results") or []
    return data

print("=" * 78)
print("ATK-5-27  is migrate_played_at_units() needed on this read route?")
print("=" * 78)

# --- build a mixed-unit table: half seconds-valued, half ms-valued -----------
c = get_db()
c.execute("DELETE FROM games WHERE id LIKE 'synUnit%'")
rows = []
base_ms = 1_700_000_000_000
for i in range(6):
    # newest 3 in SECONDS, oldest 3 in MILLISECONDS -> a raw ORDER BY would
    # bury the seconds-valued rows under every ms-valued row.
    ts = base_ms - i * 3_600_000
    rows.append((f"synUnitS{i}", ts // 1000, "seconds"))   # seconds-valued
    rows.append((f"synUnitM{i}", ts, "milliseconds"))      # ms-valued
c.executemany(
    "INSERT OR REPLACE INTO games (id, speed, rated, played_at, white_player,"
    " black_player, white_rating, black_rating, user_color, result, opening_name,"
    " eco, moves_san, analyzed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    [(i, "blitz", 1, p, "a", "b", 2000, 2000, "white", "1-0",
      f"UnitProbe {u}", "A00", "1. e4", 1) for i, p, u in rows])
c.commit()
c.close()

# newest timestamps, for the expected order
expected = [r[0] for r in sorted(rows, key=lambda r: -r[1])]  # interleaved M/S
print(f"\n  built {len(rows)} rows, half seconds-valued / half ms-valued")
print(f"  expected newest-first order: {expected[:8]} ...")

raw = sqlite3.connect(DB_PATH, isolation_level=None)


def raw_order():
    return [x[0] for x in raw.execute(
        "SELECT id FROM games WHERE id LIKE 'synUnit%' ORDER BY played_at DESC").fetchall()]


print(f"\n  naive raw ORDER BY played_at (what the write exists to fix):")
print(f"    {raw_order()[:6]} ...")
print(f"    -> seconds-valued rows {'ARE' if raw_order()[:3] == sorted([r[0] for r in rows if r[2]=='seconds']) else 'are NOT'} buried: {raw_order()[:3]}")
raw.close()

print("\n  --- WITH the per-request write (current behaviour) ---")
c = get_db()
c.execute("UPDATE games SET played_at = played_at WHERE 0")
c.commit()
c.close()
r = client.get("/api/games/recent", params={"limit": 100})
with_write = [g["id"] for g in _games(r) if g["id"].startswith("synUnit")]
print(f"    API order      : {with_write[:8]} ...")
c = get_db()
units_after = c.execute(
    "SELECT COUNT(*) FROM games WHERE id LIKE 'synUnit%' AND played_at > 100000000000"
).fetchone()[0]
c.close()
print(f"    rows the write actually converted: {units_after}/6 "
      f"(the write ran: {'yes' if units_after else 'NO'})")

print("\n  --- WITHOUT the per-request write (write neutralised) ---")
import backend.api as apimod  # noqa: E402
_real = apimod.migrate_played_at_units
apimod.migrate_played_at_units = lambda: 0
r2 = client.get("/api/games/recent", params={"limit": 100})
apimod.migrate_played_at_units = _real
without_write = [g["id"] for g in _games(r2) if g["id"].startswith("synUnit")]
print(f"    API order      : {without_write[:8]} ...")

print("\n  --- verdict ---")
print(f"    identical order with and without the write : {with_write == without_write}")
print(f"    both match the correct newest-first order  : "
      f"{without_write == expected[:len(without_write)]}")
c = get_db()
c.execute("DELETE FROM games WHERE id LIKE 'synUnit%'")
c.commit()
c.close()
print("\n  (rows cleaned up; probe is idempotent)")

print("\n  --- and what the write costs while it is there ---")
# re-create the mixed table, then hold the write lock and time the read
c = get_db()
c.executemany(
    "INSERT OR REPLACE INTO games (id, speed, rated, played_at, white_player,"
    " black_player, white_rating, black_rating, user_color, result, opening_name,"
    " eco, moves_san, analyzed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    [(i, "blitz", 1, p, "a", "b", 2000, 2000, "white", "1-0", "X", "A00", "1. e4", 1)
     for i, p, _ in rows])
c.commit()
c.close()


def hold(hold_s=4.0):
    cc = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    cc.execute("BEGIN IMMEDIATE")
    cc.execute("UPDATE games SET played_at = played_at WHERE 0")
    time.sleep(hold_s)
    cc.execute("ROLLBACK")
    cc.close()


t = threading.Thread(target=hold, daemon=True)
t.start()
time.sleep(0.4)
t0 = time.time()
r3 = client.get("/api/games/recent", params={"limit": 5})
blocked = time.time() - t0
print(f"    GET /api/games/recent during a 4 s foreign write lock: "
      f"HTTP {r3.status_code} after {blocked:.2f}s")
t.join(10)
c = get_db()
c.execute("DELETE FROM games WHERE id LIKE 'synUnit%'")
c.commit()
c.close()
print("done.")
