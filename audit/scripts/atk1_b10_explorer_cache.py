"""BUILDER-10 probe: `GET /api/repertoire/explore?ratings=<anything>` is an unbounded,
never-evicted write into `explorer_cache` (append-only table, no TTL cleanup)."""
import os
import sys
import json
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

import backend.repertoire as R
import backend.db as dbm

FENS = [
    chess.STARTING_FEN,
    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1",
]


def fake_get(url, params=None, headers=None, timeout=None, **kw):
    import chess as _c
    b = _c.Board(params.get("fen", ""))
    sans = [b.san(m) for m in list(b.legal_moves)[:3]]

    class Resp:
        status_code = 200

        def json(self):
            return {"white": 100, "draws": 50, "black": 100,
                    "moves": [{"uci": b.parse_san(s).uci(), "san": s, "white": 30,
                               "draws": 10, "black": 20, "averageRating": 2300} for s in sans]}
    return Resp()


R.requests = type("m", (), {"get": staticmethod(fake_get)})
wipe()

print("=" * 78)
print("A. `ratings` is a free-text query parameter and is part of the cache PRIMARY KEY")
print("=" * 78)
for i, ratings in enumerate([
    "2000,2200,2500",
    "2000",
    "1500,1600,1700,1800,1900",
    "not-a-rating-list",
    "'; DROP TABLE repertoire_moves; --",
    "x" * 4000,
]):
    r = CLIENT.get("/api/repertoire/explore", params={"fen": FENS[i % len(FENS)], "ratings": ratings})
    print(f"   ratings={ratings[:40]!r:<45} -> HTTP {r.status_code}, "
          f"{len(r.json().get('moves', []))} moves")

rows = raw("SELECT fen, ratings, length(ratings) lr, updated_at FROM explorer_cache ORDER BY fen, ratings")
print(f"\n   explorer_cache now holds {len(rows)} rows:")
for r_ in rows:
    print(f"     fen={r_['fen'][:28]!r:<32} ratings={r_['ratings'][:30]!r:<34} len={r_['lr']}")

print("\n" + "=" * 78)
print("B. nothing ever deletes from explorer_cache")
print("=" * 78)
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "backend", "repertoire.py"), encoding="utf-8").read()
db = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "backend", "db.py"), encoding="utf-8").read()
print("   DELETE/VACUUM against explorer_cache in repertoire.py:",
      src.count("DELETE FROM explorer_cache"), "occurrence(s)")
print("   DELETE/VACUUM against explorer_cache in db.py         :",
      db.count("DELETE FROM explorer_cache"), "occurrence(s)")
print("   INSERT OR REPLACE INTO explorer_cache in repertoire.py:",
      src.count("INSERT OR REPLACE INTO explorer_cache"))

print("\n   the read path treats an entry as valid for 7 days and then simply ignores it:")
import re
m = re.search(r"# Cache valid for (\d+) days\s*\n\s*if time\.time\(\) - cached\[\"updated_at\"\] < (\S+)",
              src)
print("   ", m.group(0).replace("\n", " ") if m else "(pattern not found)")

print("\n   ageing every row past the 7-day window and re-reading:")
conn = __import__("sqlite3").connect(dbm.get_db_path())
conn.execute("UPDATE explorer_cache SET updated_at = updated_at - 30*86400")
conn.commit()
conn.close()
CLIENT.get("/api/repertoire/explore", params={"fen": FENS[0], "ratings": "2000,2200,2500"})
print("   explorer_cache rows after a full expiry + a fresh read:",
      one("SELECT COUNT(*) c FROM explorer_cache")["c"], "(nothing reaped)")
size = os.path.getsize(dbm.get_db_path())
print("   temp DB size now: %.1f kB" % (size / 1024.0))
