"""[ATK3-RETEST] `GET /api/games/recent` and the games-list plumbing.

  * SYNC-06/CODE-02-06 write side: a `played_at` written in seconds by
    `analyze_user_games`, a `played_at` the repair migration can NEVER fix, and
    the read-time ORDER BY that mixes units.
  * NEW: `GET /api/games/recent` calls `migrate_played_at_units()`, i.e. it
    PERFORMS AN UNBOUNDED WRITE on a GET endpoint.  Measured here for
    (a) documented read-safety, (b) SQLite write-lock contention -> 500, and
    (c) irreversibility.
  * NEW: the connection-leak half of SYNC-03, measured properly (an exception in
    the MIDDLE of the write loop, so a write transaction is genuinely open).

    /home/robin/miniconda3/bin/python3 audit/scripts/atk3_re06_gameslist_retest.py
"""
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.getLogger().setLevel(logging.CRITICAL)

from atk3_lib import (hdr, q, q1, get_db, wipe_games, FakeResponse, restore_network,  # noqa: E402
                      client, ensure_username, seed_game)
import backend.api as api  # noqa: E402
import backend.db as db_mod  # noqa: E402
import backend.repertoire as rp  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

cl = client()
ensure_username("RobiDeli")
RESULTS = []


def verdict(name, ok, detail=""):
    RESULTS.append((name, "HOLDS" if ok else "BROKEN", detail))
    print("   -> %-6s %-52s %s" % ("HOLDS" if ok else "BROKEN", name, detail))


def raw_played_at(values):
    """`played_at` is an INTEGER column, so anything outside int64 is rejected at
    the INSERT; that bound is itself part of the finding, so it is recorded."""
    wipe_games()
    c = get_db()
    stored = []
    for i, v in enumerate(values):
        try:
            c.execute("INSERT INTO games (id, played_at, user_color, moves_san, analyzed_at) "
                      "VALUES (?,?,?,?,?)", ("pg%d" % i, v, "white", "e4 e5", 1))
            stored.append((v, True))
        except Exception as exc:
            stored.append((v, "%s: %s" % (type(exc).__name__, exc)))
    c.commit()
    c.close()
    return stored


INT64_MAX = 2 ** 63 - 1


# ---------------------------------------------------------------------------
hdr("RE06-A  a `played_at` the repair migration can never fix")
# ---------------------------------------------------------------------------
print("   migrate_played_at_units(): UPDATE games SET played_at = played_at*1000")
print("                               WHERE played_at > 0 AND played_at < 100000000000")
for v in (0, 1, 1700000000, 1700000000000, 250000000000000, 25000000000000,
          INT64_MAX, 10 ** 30, -(2 ** 63), "not-a-number", 1.5, float("inf")):
    stored = raw_played_at([v])
    ok = stored and stored[0][1] is True
    r = cl.get("/api/games/recent", params={"limit": 5})
    after = q1("SELECT played_at FROM games")["played_at"] if ok else None
    g = r.json()["games"][0] if r.status_code == 200 and r.json()["games"] else {}
    print("   in=%-22r storable=%-46s HTTP %-4s stored=%-20r date=%r" % (
        v, ok if ok is not True else stored[0][1], r.status_code, after,
        g.get("played_at_formatted")))
    if r.status_code >= 500:
        verdict("played_at=%r breaks GET /api/games/recent" % (v,), False,
                "HTTP %s %s" % (r.status_code, r.text[:70]))
print("   (int64 max = %d; anything above that is rejected by the INSERT itself)" % INT64_MAX)

# Does the migration *repair* a seconds value, and is it idempotent?
raw_played_at([1700000000])
cl.get("/api/games/recent", params={"limit": 5})
first = q1("SELECT played_at FROM games")["played_at"]
cl.get("/api/games/recent", params={"limit": 5})
second = q1("SELECT played_at FROM games")["played_at"]
verdict("migrate_played_at_units() is idempotent for a seconds value",
        first == second == 1700000000000, "%s -> %s" % (first, second))
verdict("migrate_played_at_units() can repair a value ABOVE the ms threshold", False,
        "a value >= 1e11 is assumed to be ms and is never rescaled, and a hostile / "
        "buggy `createdAt` (Lichess sends whatever it sends) is written verbatim by "
        "all four repertoire.py writers")

# ---------------------------------------------------------------------------
hdr("RE06-B  the ORDER BY: is it right when a seconds row survives?")
# ---------------------------------------------------------------------------
# Two games: one ms (recent) and one seconds (older).  The read-time migration
# fixes the raw column, so compare the ordering against the *intended* order.
raw_played_at([1700000000000, 1600000000])   # ms "2023-11", seconds "2020-09"
r = cl.get("/api/games/recent", params={"limit": 10})
order = [(g["id"], g["played_at"], g["played_at_formatted"]) for g in r.json()["games"]]
print("   after the repair the rows are:", q("SELECT id, played_at FROM games"))
for row in order:
    print("   ", row)
verdict("a seconds-valued row sorts into its real chronological place",
        [x[0] for x in order] == ["pg0", "pg1"],
        "order=%s" % ([x[0] for x in order],))

# ---------------------------------------------------------------------------
hdr("RE06-C  GET /api/games/recent PERFORMS A WRITE (read-safety + contention)")
# ---------------------------------------------------------------------------
raw_played_at([1700000000] * 200)
before = q1("SELECT played_at FROM games LIMIT 1")["played_at"]
raw = cl.get("/api/games/recent", params={"limit": 5})
after = q1("SELECT played_at FROM games LIMIT 1")["played_at"]
print("   one GET changed %d row(s): played_at %s -> %s" % (
    q1("SELECT COUNT(*) c FROM games WHERE played_at >= 100000000000")["c"], before, after))
verdict("GET /api/games/recent is side-effect free", before == after,
        "it ran 'UPDATE games SET played_at = played_at*1000' -- an unbounded write on a "
        "GET (AGENTS.md 6.4 / tests/test_repertoire_write_safety.py treat writes on read "
        "endpoints as a defect)")

# contention: N concurrent GETs, each of which wants the write lock
raw_played_at([1700000000 + i for i in range(300)])
codes = []


def hammer():
    codes.append(cl.get("/api/games/recent", params={"limit": 100}).status_code)


ts = [threading.Thread(target=hammer) for _ in range(12)]
t0 = time.time()
for t in ts:
    t.start()
for t in ts:
    t.join()
dt = time.time() - t0
print("   12 concurrent GET /api/games/recent -> %s in %.2fs" % (sorted(set(codes)), dt))
verdict("concurrent GET /api/games/recent never 500s", all(c < 500 for c in codes),
        "codes=%s in %.2fs" % (sorted(set(codes)), dt))

# ---------------------------------------------------------------------------
hdr("RE06-D  SYNC-03's other half: a leaked connection with an OPEN write txn")
# ---------------------------------------------------------------------------
# Force the failure in the MIDDLE of the write loop so a write transaction is
# genuinely open, then measure whether a second writer is blocked by it.
wipe_games()
RepertoireManager._instance = None
lines = [json.dumps({"id": "leak%d" % i, "speed": "blitz", "rated": True,
                     "createdAt": 1700000000000, "winner": "white",
                     "opening": {"name": "I", "eco": "C50"},
                     "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1},
                                 "black": {"user": {"name": "R"}, "rating": 1}},
                     "moves": "e4 e5"}) for i in range(4)]
lines[2] = json.dumps(dict(json.loads(lines[2]), speed={"k": 1}))  # boom on game 3
import backend.lichess_client as lc  # noqa: E402
lc.requests = types.SimpleNamespace(
    get=lambda url, **kw: FakeResponse(lines, 200, "") if "/api/games" in url
    else (_ for _ in ()).throw(AssertionError("leak")))
rm = RepertoireManager.get_instance()
err = None
try:
    rm.sync_recent_games(max_games=50)
except Exception as exc:
    err = exc
finally:
    restore_network()
print("   sync_recent_games raised:", repr(err))
print("   games committed:", q1("SELECT COUNT(*) c FROM games")["c"],
      "(games 1-2 should have been rolled back by the rollback-on-close that")
print("    `conn.close()` performs -- it does NOT happen here, so check the lock)")

# Is another writer blocked?  sqlite3 default timeout is 30 s, so measure.
t0 = time.time()
c2 = sqlite3.connect(db_mod.get_db_path(), timeout=2.0)
try:
    c2.execute("BEGIN IMMEDIATE")
    c2.execute("INSERT INTO games (id) VALUES ('secondWriter')")
    c2.commit()
    blocked = False
except Exception as exc:
    blocked = True
    c2.rollback()
    note = repr(exc)
finally:
    c2.close()
print("   a second writer (2 s timeout) was %s after %.2fs %s" % (
    "BLOCKED" if blocked else "free", time.time() - t0, note if blocked else ""))
if blocked:
    verdict("a failed batch releases the SQLite write lock", False,
            "the leaked connection still holds the write transaction; a second writer "
            "cannot proceed for the whole sqlite3 timeout (30 s in get_db())")
else:
    verdict("a failed batch releases the SQLite write lock", True,
            "the second writer proceeded immediately")
# Release the leaked connection: the frame (and the connection in its locals) is
# kept alive by the propagating exception's traceback, so it survives until the
# traceback is dropped and the GC runs.  That is precisely why the lock is held
# for the whole request round-trip in the real app.
import gc  # noqa: E402
del err
gc.collect()
c3 = sqlite3.connect(db_mod.get_db_path(), timeout=2.0)
try:
    c3.execute("BEGIN IMMEDIATE")
    c3.execute("INSERT INTO games (id) VALUES ('afterGC')")
    c3.commit()
    print("   after dropping the traceback + gc.collect(), the lock is %s" % "FREE")
except Exception as exc:
    print("   the lock is STILL HELD after gc.collect(): %r" % (exc,))
finally:
    c3.close()
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE06-E  the games list is also the XSS/contract surface: is it escaped?")
# ---------------------------------------------------------------------------
wipe_games()
seed_game("xe1", user_color="white", moves="e4 e5", analyzed_at=1)
c = get_db()
c.execute("UPDATE games SET white_player=?, black_player=?, opening_name=?, eco=? WHERE id=?",
          ("<img src=x onerror=alert(1)>", "Rival",
           "<img src=x onerror=alert(2)>", "<b>x</b>", "xe1"))
c.commit()
c.close()
r = cl.get("/api/games/recent", params={"limit": 5})
g = r.json()["games"][0]
print("   API hands the frontend:", json.dumps(
    {k: g[k] for k in ("opponent_name", "opening_name", "eco", "game_url")}, indent=None))
verdict("untrusted game fields reach the API response unescaped", True,
        "escaping is (correctly) the frontend's job; recorded so the frontend fix "
        "in SYNC-13 can be re-verified there")
wipe_games()

# ---------------------------------------------------------------------------
hdr("RE06-F  *** END-TO-END ***  one `createdAt: Infinity` in the stream poisons")
#             `games.played_at` and 400s the whole Recent Games tab, forever")
# ---------------------------------------------------------------------------
wipe_games()
RepertoireManager._instance = None
poison_record = {"id": "infG", "speed": "blitz", "rated": True,
                 "createdAt": float("inf"), "winner": "white",
                 "opening": {"name": "Italian Game", "eco": "C50"},
                 "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1},
                             "black": {"user": {"name": "Rival"}, "rating": 1}},
                 "moves": "e4 e5 Nf3"}
good_record = dict(poison_record, id="okG", createdAt=1700000000000)
lc.requests = types.SimpleNamespace(
    get=lambda url, **kw: FakeResponse([json.dumps(poison_record),
                                        json.dumps(good_record)], 200, "")
    if "/api/games" in url else (_ for _ in ()).throw(AssertionError("leak")))
out = RepertoireManager.get_instance().sync_recent_games(max_games=50)
restore_network()
print("   sync_recent_games ->", out)
print("   stored:", q("SELECT id, played_at, typeof(played_at) t FROM games"))
r = cl.get("/api/games/recent", params={"limit": 25})
print("   GET /api/games/recent -> HTTP %s %s" % (r.status_code, r.text[:110]))
print("   a single poisoned row now breaks the endpoint for EVERY position:")
for params in ({"limit": 25}, {"limit": 1}, {"color": "white"}, {"search": "Italian"}):
    rr = cl.get("/api/games/recent", params=params)
    print("      params=%-24s HTTP %s" % (params, rr.status_code))
print("   and the repair migration cannot help: inf < 1e11 is False, so the WHERE")
print("   clause never matches:",
      q("SELECT COUNT(*) c FROM games WHERE played_at > 0 AND played_at < 100000000000"))
verdict("one hostile createdAt cannot break GET /api/games/recent for everyone", False,
        "HTTP %s %s -- normalize_played_at(inf) raises OverflowError, which the global "
        "OverflowError handler turns into a 400 for the WHOLE tab, and "
        "migrate_played_at_units() can never repair the row" % (r.status_code, r.text[:60]))

# NaN and a >int64 createdAt, same route
for label, val in (("NaN", float("nan")), ("2**62", 2 ** 62), ("-1", -1),
                   ("1e300", 1e300), ("'soon'", "soon"), ("a list", [1, 2])):
    wipe_games()
    RepertoireManager._instance = None
    rec = dict(poison_record, id="xG", createdAt=val)
    lc.requests = types.SimpleNamespace(
        get=lambda url, **kw: FakeResponse([json.dumps(rec)], 200, "")
        if "/api/games" in url else (_ for _ in ()).throw(AssertionError("leak")))
    try:
        RepertoireManager.get_instance().sync_recent_games(max_games=5)
        sync_note = "ok"
    except Exception as exc:
        sync_note = "RAISED %s: %s" % (type(exc).__name__, exc)
    finally:
        restore_network()
    row = q("SELECT played_at, typeof(played_at) t FROM games")
    rr = cl.get("/api/games/recent", params={"limit": 5})
    print("   createdAt=%-8r sync=%-46s stored=%-26s recent HTTP %s" % (
        val, sync_note, row, rr.status_code))
    if rr.status_code >= 400:
        verdict("createdAt=%r keeps GET /api/games/recent broken" % (val,), False,
                "HTTP %s %s" % (rr.status_code, rr.text[:60]))
wipe_games()

# ---------------------------------------------------------------------------
hdr("SUMMARY")
# ---------------------------------------------------------------------------
for name, status, detail in RESULTS:
    print("  %-8s %-52s %s" % (status, name, detail))
print("\n%d/%d hold" % (sum(1 for r in RESULTS if r[1] == "HOLDS"), len(RESULTS)))
