"""ATK3-06 : hostile / malformed NDJSON from the Lichess stream.
Does one bad line abort the whole sync, or silently corrupt the DB?"""
import os
import sys
import json
import gc
import sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import hdr, wipe_games, q, q1, fake_ndjson, restore_network, client  # noqa: E402


def wipe():
    """wipe_games(), tolerating the write-lock left behind by the un-rolled-back sync."""
    for _ in range(3):
        try:
            wipe_games()
            return
        except sqlite3.OperationalError as e:
            print("   [db still locked by the leaked connection: %s] gc.collect()" % e)
            gc.collect()
    wipe_games()

cl = client()
import backend.lichess_client as lc  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402
rm = RepertoireManager.get_instance()


def g(gid, **kw):
    d = {"id": gid, "speed": "blitz", "rated": True, "createdAt": 1700000000000,
         "winner": "white", "opening": {"name": "Italian Game", "eco": "C50"},
         "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1800},
                     "black": {"user": {"name": "Rival"}, "rating": 1800}},
         "moves": "e4 e5 Nf3 Nc6"}
    d.update(kw)
    return d


hdr("STEP A — the parser itself (LichessClient.fetch_user_games) on hostile lines")
lines = [
    json.dumps(g("ok1")),
    "",                                   # blank
    "   ",                                # whitespace
    "{not json",                          # broken json
    "[1,2,3]",                            # valid JSON, WRONG TYPE
    json.dumps(g("ok2")),
    '{"id":"partial","moves":"e4 e5"',    # truncated final line, no newline
]
fake_ndjson(lines)
parsed = lc.LichessClient().fetch_user_games("RobiDeli", max_games=10)
restore_network()
print("  raw lines in :", len(lines))
print("  games out    :", len(parsed), "types:", [type(x).__name__ for x in parsed])
print("  ids          :", [x.get('id') if isinstance(x, dict) else x for x in parsed])
print("  -> parser is tolerant of blank/broken lines, but happily returns NON-DICT objects")

hdr("STEP B — feed that same stream to POST /api/games/sync_recent (sync_recent_games)")
wipe()
import time as _t
_t0 = _t.time()
fake_ndjson(lines)
try:
    r = cl.post("/api/games/sync_recent?max_games=20")
    print("  HTTP", r.status_code, r.text[:200])
except Exception as e:
    print("  EXCEPTION", type(e).__name__, e)
restore_network()
print("  elapsed: %.1fs" % (_t.time() - _t0))
print("  games rows:", q("SELECT id, result, moves_san FROM games"))
print("  -> 2 of 3 valid games were lost: no commit happened, the connection was never")
print("     rolled back/closed, and the open write txn blocked the next writer:")
t1 = _t.time()
try:
    wipe()
    print("     next writer had to wait %.1fs for the busy timeout" % (_t.time() - t1))
except Exception as e:
    print("     next writer FAILED:", e)

hdr("STEP C — same stream into /api/sync (which iterates g.get(...) unguarded)")
wipe()
fake_ndjson(lines)
r = cl.post("/api/sync?max_games=20")
restore_network()
print("  HTTP", r.status_code)
print("  body:", r.text[:200])
print("  games rows persisted:", q("SELECT id FROM games"))
print("  -> a single `[1,2,3]` line kills the whole loop; games AFTER the bad line are never stored")

hdr("STEP D — games with NO id at all")
wipe()
fake_ndjson([json.dumps(g("good")), json.dumps(g(None, id=None)), json.dumps(g("tail"))])
r = cl.post("/api/sync?max_games=20")
restore_network()
print("  HTTP", r.status_code, r.text[:160])
rows = q("SELECT id, typeof(id) t FROM games")
print("  rows:", rows)
print("  -> games.id is TEXT PRIMARY KEY, which in SQLite does NOT imply NOT NULL")

hdr("STEP E — 2 games with id=NULL (does the PK even hold?)")
wipe()
fake_ndjson([json.dumps(g(None, id=None)), json.dumps(g(None, id=None)),
             json.dumps(g("tail"))])
r = cl.post("/api/sync?max_games=20")
restore_network()
print("  HTTP", r.status_code, r.text[:120])
print("  rows:", q("SELECT id, typeof(id) t, moves_san FROM games"))
print("  count with NULL id:", q1("SELECT COUNT(*) c FROM games WHERE id IS NULL")["c"])

hdr("STEP F — unicode / control-char / HTML player names + hostile 'id' (frontend renders these)")
wipe()
fake_ndjson([json.dumps(g('<img src=x onerror=alert(1)>',
                          players={"white": {"user": {"name": "<img src=x onerror=alert(1)>"},
                                             "rating": 1800},
                                   "black": {"user": {"name": "Rival\x00\x07"}, "rating": 1}}))])
r = cl.post("/api/sync?max_games=20")
restore_network()
print("  HTTP", r.status_code, r.text[:120])
rr = cl.get("/api/games/recent")
print("  /api/games/recent ->", json.dumps(rr.json()["games"], indent=1)[:700])
print("  !! frontend renders opponent_name/opening_name/eco/game_url via innerHTML (repertoire_builder.js:992-1015)")

hdr("STEP G — 10 000-move 'game'")
wipe()
huge = " ".join(["e4", "e5"] * 5000)
fake_ndjson([json.dumps(g("huge", moves=huge))])
r = cl.post("/api/games/sync_recent?max_games=5")
restore_network()
print("  HTTP", r.status_code, r.text[:120])
row = q1("SELECT LENGTH(moves_san) L FROM games WHERE id='huge'")
print("  stored moves_san length:", row)
t0 = __import__("time").time()
rm._user_stats_cache = {}
rm._user_stats_time = 0
st = rm.get_user_position_stats("white")
print("  get_user_position_stats() over it: %.2fs, %d indexed positions" % (__import__("time").time() - t0, len(st)))

hdr("STEP H — moves in UCI instead of SAN (Lichess always sends SAN)")
wipe()
fake_ndjson([json.dumps(g("uci", moves="e2e4 e7e5 g1f3 b8c6"))])
r = cl.post("/api/games/sync_recent?max_games=5")
restore_network()
print("  HTTP", r.status_code, r.text[:120])
print("  stored:", q("SELECT id, moves_san, opening_name, eco FROM games"))
g2 = cl.get("/api/games/recent").json()["games"][0]
print("  /api/games/recent:", {k: g2[k] for k in ("opening_name", "eco", "total_plies", "opening_ply")})

hdr("STEP I — garbage tokens in `moves`")
wipe()
fake_ndjson([json.dumps(g("junk", moves="e4 e5 ♚♛ ZZZz e2e4 XX 1-0 O-O 1/2-1/2 e4"))])
r = cl.post("/api/games/sync_recent?max_games=5")
restore_network()
print("  HTTP", r.status_code, r.text[:120])
print("  stored:", q("SELECT id, opening_name, eco, moves_san FROM games"))
print("  /api/recent:", [{k: v for k, v in x.items() if k in ('id','opening_name','eco','total_plies')}
                         for x in cl.get("/api/games/recent").json()["games"]])
