#!/usr/bin/env python3
"""ATK-5 re2-1: is GET /api/blunders/{id} an unbounded threadpool saturator?

FIXER-B/L's own comment (api.py:1391-1396) names the hazard it was fixing:
  "an un-cached, un-rate-limited, fully blocking requests.get(timeout=8) (twice
   on a 401, so up to 16 s) issued from a `def` route, i.e. from a worker thread
   in the shared anyio pool that also serves the SSE endpoints."

They added memo + single-flight, which fixes *re-opening the same blunder*.
This probe asks what they did NOT fix:
  A. is there any cap on CONCURRENT outbound explorer calls?
  B. does the single-flight engage for the same POSITION across blunders?
     (memo+lock are keyed by the RAW fen_before, not the 4-token norm)
  C. what does a LEGITIMATE user experience while the pool is saturated?

Everything runs against an isolated temp DB on a real uvicorn socket. The
network is replaced by a counting, latency-simulating stub, so no real external
traffic and no real Stockfish.

Idempotent: synthetic rows are prefixed synRe2 and removed at the end.
"""
import os
import sys
import json
import time
import socket
import threading
import logging
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.db import get_db  # noqa: E402

# ---------------------------------------------------------------- seed data
SEED_N = 60
conn = get_db()
conn.execute("DELETE FROM blunders WHERE game_id LIKE 'synRe2%'")
conn.execute("DELETE FROM blunders WHERE id >= 90000")
conn.execute("DELETE FROM games WHERE id LIKE 'synRe2%'")
grows, brows = [], []
for i in range(SEED_N):
    gid = f"synRe2G{i:03d}"
    # SAME 4-token position for every row, but a different clock -> a different
    # RAW fen string. This is the normal real-world shape: two games reaching the
    # same position have different move counters.
    fen = f"rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 {i + 1}"
    grows.append((gid, "blitz", 1, 1700000000000 + i, "a", "b", 2000, 2000,
                  "white", "1-0", "Probe", "C00", "1. e4", 1))
    brows.append((90000 + i, gid, 5, 5, "white", fen, fen, "e7e5", "e5", "Nf3", "Nf3",
                  -30, 200, 230, "Probe", "C00", 0, "[]", "x", "y", "development", 900, 1700000000))
conn.executemany(
    "INSERT OR REPLACE INTO games (id,speed,rated,played_at,white_player,black_player,"
    "white_rating,black_rating,user_color,result,opening_name,eco,moves_san,analyzed_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", grows)
conn.executemany(
    "INSERT INTO blunders (id,game_id,move_number,ply,color,fen_before,fen_after,"
    "played_move,played_san,best_move,best_san,eval_before,eval_after,cpl_loss,"
    "opening_name,eco,is_book_move,refutation_pv,explanation,strategic_idea,category,"
    "opening_games,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", brows)
conn.commit()
BASE_ID = conn.execute("SELECT MIN(id) FROM blunders WHERE game_id LIKE 'synRe2%'").fetchone()[0]
conn.close()
print(f"seeded {SEED_N} synthetic blunders, ids {BASE_ID}..{BASE_ID + SEED_N - 1}")
print(f"all share ONE 4-token position; only the fullmove number differs\n")

# ---------------------------------------------- latency-simulating explorer
import backend.lichess_client as lc  # noqa: E402

LAT = float(os.environ.get("ATK5_LAT", "2.0"))
CALLS = {"n": 0}
_c = threading.Lock()


def slow_stats(self, fen, play=""):
    with _c:
        CALLS["n"] += 1
    time.sleep(LAT)              # a representative explorer round trip
    return None                  # explorer unreachable -> the failure path


lc.LichessClient.get_opening_explorer_stats = slow_stats

# ------------------------------------------------------------- real server
import backend.api as apimod  # noqa: E402
from backend.api import app  # noqa: E402
import uvicorn  # noqa: E402

PORT = 39441
logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                    log_level="critical", access_log=False))
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(600):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        break
    except OSError:
        time.sleep(0.05)
time.sleep(0.4)
print(f"real uvicorn on {PORT}; simulated explorer latency {LAT}s\n")

BASE = f"http://127.0.0.1:{PORT}"


def timed_get(path, timeout=90):
    t0 = time.time()
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            r.read()
            return time.time() - t0, r.status
    except urllib.error.HTTPError as e:
        e.read()
        return time.time() - t0, e.code
    except Exception as e:
        return time.time() - t0, f"EXC {type(e).__name__}"


print("=" * 78)
print("[A/B] 48 concurrent blunder-detail views, all at the SAME position")
print("=" * 78)
ids = [BASE_ID + i for i in range(48)]
with _c:
    CALLS["n"] = 0
lat, st = [], []
barrier = threading.Barrier(len(ids))


def hit(bid):
    barrier.wait()
    d, s = timed_get(f"/api/blunders/{bid}")
    lat.append(d)
    st.append(s)


ts = [threading.Thread(target=hit, args=(b,)) for b in ids]
t_start = time.time()
for t in ts:
    t.start()

# --- C: probe a cheap sync route WHILE the pool is saturated ---------------
time.sleep(0.8)
print("  (now probing cheap sync routes while the burst is in flight)")
cheap = {}
for path in ("/api/dashboard", "/api/repertoire/studies", "/api/games/recent?limit=1",
             "/api/settings", "/api/repertoire/train/progress"):
    d, s = timed_get(path, timeout=90)
    cheap[path] = (d, s)
    print(f"    {d:7.2f}s  HTTP {s}  GET {path}")

for t in ts:
    t.join(120)
wall = time.time() - t_start

print(f"\n  burst wall clock          : {wall:.2f}s")
print(f"  outbound explorer calls   : {CALLS['n']} for 48 views of ONE position")
print(f"  single-flight engaged?    : "
      f"{'NO -- ' + str(CALLS['n']) + ' calls' if CALLS['n'] > 1 else 'yes (' + str(CALLS['n']) + ' call)'}")
print(f"  status codes              : {sorted(set(st))}")
print(f"  latency min/med/max       : {min(lat):.2f} / {sorted(lat)[len(lat)//2]:.2f} / {max(lat):.2f}s")
print(f"\n  cheap-route latency during the burst (baseline first):")
for path, (d, s) in cheap.items():
    print(f"    {d:7.2f}s  {path}")

print("\n  -> the burst is over; re-measure the same cheap routes for comparison")
for path in cheap:
    d, s = timed_get(path)
    print(f"    {d:7.2f}s  {path}")

print("\n" + "=" * 78)
print("[C2] the single-flight DOES work for the SAME blunder id re-opened")
print("=" * 78)
with _c:
    CALLS["n"] = 0
ids2 = [BASE_ID] * 16
lat2 = []
barrier2 = threading.Barrier(len(ids2))


def hit2(bid):
    barrier2.wait()
    d, s = timed_get(f"/api/blunders/{bid}")
    lat2.append(d)


ts2 = [threading.Thread(target=hit2, args=(b,)) for b in ids2]
t0 = time.time()
for t in ts2:
    t.start()
for t in ts2:
    t.join(120)
print(f"  16 concurrent views of ONE blunder id -> {CALLS['n']} outbound call(s) "
      f"in {time.time()-t0:.2f}s")
print("  -> confirms the memo/lock works per-raw-FEN, which is exactly the")
print("     narrow case the fixer targeted. Distinct blunders at the same")
print("     POSITION are not deduplicated because the key is the raw 6-token FEN.")

print("\n" + "=" * 78)
print("[D] include_master_stats=false -- the only escape, unused by the frontend")
print("=" * 78)
with _c:
    CALLS["n"] = 0
d, s = timed_get(f"/api/blunders/{BASE_ID}?include_master_stats=false")
print(f"  {d:.3f}s HTTP {s}  outbound calls: {CALLS['n']}")
d, s = timed_get(f"/api/blunders/{BASE_ID}")
print(f"  {d:.3f}s HTTP {s}  outbound calls: {CALLS['n']}  (default path)")

print("\n" + "=" * 78)
print("[E] does the threadpool bound the damage? (worst case, no explorer at all)")
print("=" * 78)
apimod.get_master_stats = lambda fen: None      # network fully removed
with _c:
    CALLS["n"] = 0
N = 200
barrier3 = threading.Barrier(N)
st3 = []


def hit3(bid):
    barrier3.wait()
    d, s = timed_get(f"/api/blunders/{bid}", timeout=120)
    st3.append(s)


ts3 = [threading.Thread(target=hit3, args=(BASE_ID + (i % SEED_N),)) for i in range(N)]
t0 = time.time()
for t in ts3:
    t.start()
d_cheap, s_cheap = None, None
time.sleep(0.5)
d_cheap, s_cheap = timed_get("/api/dashboard", timeout=120)
for t in ts3:
    t.join(180)
print(f"  {N} concurrent requests, network removed entirely")
print(f"  burst wall clock        : {time.time()-t0:.2f}s")
print(f"  statuses                : {sorted(set(st3))}")
print(f"  /api/dashboard during it: {d_cheap:.2f}s HTTP {s_cheap}")

srv.should_exit = True
time.sleep(0.5)

# ---------------------------------------------------------------- cleanup
c = get_db()
c.execute("DELETE FROM blunders WHERE game_id LIKE 'synRe2%'")
c.execute("DELETE FROM blunders WHERE id >= 90000")
c.execute("DELETE FROM games WHERE id LIKE 'synRe2%'")
c.commit()
c.close()
print("\nsynthetic rows removed; probe is idempotent. done.")
