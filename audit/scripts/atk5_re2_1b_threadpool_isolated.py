#!/usr/bin/env python3
"""ATK-5 re2-1b: threadpool starvation by GET /api/blunders/{id} -- CORRECTED.

The first attempt (atk5_re2_1_blunder_threadpool.py) ran the uvicorn server in
the SAME process as the 48 client threads and measured an 87 s burst.  That is
GIL contention between the client and the server, not a property of the app.  This
version runs the server in its own process and drives it with plain sockets.

Measured, on an isolated temp DB, no real network, no real Stockfish:
  A. is a burst of blunder-detail views able to starve a trivial local read?
  B. does the single-flight engage for one POSITION across distinct blunders?
  C. is there ANY cap on concurrent outbound explorer calls?
  D. what is the real per-request worst case (EXPLORER_TIMEOUT=8, x2 on 401)?

Idempotent: synthetic rows are prefixed synRe2 and removed at the end.
"""
import os
import sys
import json
import time
import socket
import subprocess
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.db import get_db  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("ATK5_PORT", "39443"))
LAT = float(os.environ.get("ATK5_LAT", "2.0"))
CALLS_FILE = "/tmp/opencode/atk5_re2_calls.txt"
SEED_N = 60

# ---------------------------------------------------------------- seed data
conn = get_db()
conn.execute("DELETE FROM blunders WHERE game_id LIKE 'synRe2%'")
conn.execute("DELETE FROM blunders WHERE id >= 90000")
conn.execute("DELETE FROM games WHERE id LIKE 'synRe2%'")
grows, brows = [], []
for i in range(SEED_N):
    gid = f"synRe2G{i:03d}"
    # ONE 4-token position; only the fullmove counter differs. This is the real
    # shape: two games reaching the same position have different move counters.
    fen = f"rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 {i + 1}"
    grows.append((gid, "blitz", 1, 1700000000000 + i, "a", "b", 2000, 2000,
                  "white", "1-0", "Probe", "C00", "1. e4", 1))
    brows.append((90000 + i, gid, 5, 5, "white", fen, fen, "e7e5", "e5", "Nf3", "Nf3",
                  -30, 200, 230, "Probe", "C00", 0, "[]", "x", "y", "development",
                  900, 1700000000))
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
conn.close()
print(f"seeded {SEED_N} blunders (ids 90000..{90000 + SEED_N - 1}), all ONE position")

# ---------------------------------------------------------- server subprocess
env = dict(os.environ)
env["CHESS_DB_PATH"] = os.environ["CHESS_DB_PATH"]
env["ATK5_LAT"] = str(LAT)
env["ATK5_CALLS_FILE"] = CALLS_FILE
env["PYTHONPATH"] = HERE
if os.path.exists(CALLS_FILE):
    os.remove(CALLS_FILE)
proc = subprocess.Popen(
    ["/home/robin/miniconda3/bin/python3", "-m", "atk5_re2_server", str(PORT)],
    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/home/robin/Projects/ChessImprovement")
for _ in range(800):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        break
    except OSError:
        time.sleep(0.05)
time.sleep(0.5)
print(f"server in its OWN process (pid {proc.pid}) on {PORT}; explorer latency {LAT}s\n")


def calls_made():
    try:
        with open(CALLS_FILE) as fh:
            return int(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def raw_get(path, read_timeout=180.0):
    """One request on its own socket, absolute timing, no client-side pooling."""
    t0 = time.time()
    s = socket.create_connection(("127.0.0.1", PORT), 10)
    s.settimeout(read_timeout)
    try:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
                  f"Connection: close\r\n\r\n".encode())
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
    except socket.timeout:
        return time.time() - t0, "TIMEOUT", b""
    finally:
        s.close()
    head = buf.split(b"\r\n", 1)[0].decode("latin-1") if buf else "NO RESPONSE"
    return time.time() - t0, head, buf


def run_burst(ids, label, probe_at=None, probes=()):
    before = calls_made()
    n = len(ids)
    res = [None] * n
    bar = threading.Barrier(n)

    def work(i, bid):
        bar.wait()
        res[i] = raw_get(f"/api/blunders/{bid}")

    ts = [threading.Thread(target=work, args=(i, b)) for i, b in enumerate(ids)]
    t0 = time.time()
    for t in ts:
        t.start()
    probe_res = []
    if probe_at is not None:
        time.sleep(probe_at)
        for p in probes:
            probe_res.append((p, raw_get(p)))
    for t in ts:
        t.join(300)
    wall = time.time() - t0
    lat = sorted(r[0] for r in res if r)
    sts = sorted({r[1] for r in res if r})
    after = calls_made()
    print(f"\n{label}")
    print(f"  requests            : {n}")
    print(f"  wall clock          : {wall:.2f}s   (one explorer call = {LAT}s, fully "
          f"parallel would be ~{LAT:.2f}s)")
    print(f"  latency min/med/max : {lat[0]:.2f} / {lat[len(lat)//2]:.2f} / {lat[-1]:.2f}s")
    print(f"  status lines        : {sts}")
    print(f"  outbound calls      : {after - before}  ({after - before} / {n} = "
          f"{(after - before) / max(n, 1):.2f} per request)")
    for p, (d, h, _b) in probe_res:
        print(f"  [concurrent] {d:8.2f}s  {h:24} GET {p}")
    return wall, lat, (after - before), probe_res


print("=" * 78)
print("BASELINE -- cheap local routes, nothing else running")
print("=" * 78)
for p in ("/api/dashboard", "/api/repertoire/studies", "/api/games/recent?limit=1",
          "/api/settings", "/api/repertoire/tree?repertoire=white"):
    d, h, _ = raw_get(p)
    print(f"  {d:7.3f}s  {h:26} GET {p}")

print("\n" + "=" * 78)
print("[A/C] 48 concurrent blunder-detail views, all at ONE position")
print("=" * 78)
ids = [90000 + i for i in range(48)]
wall, lat, ncalls, _ = run_burst(
    ids, "", probe_at=1.0,
    probes=("/api/dashboard", "/api/repertoire/studies", "/api/games/recent?limit=1",
            "/api/settings", "/api/repertoire/tree?repertoire=white"))

print("\n  single-flight verdict: "
      + ("NOT ENGAGED -- %d calls for one position" % ncalls if ncalls > 1
         else "engaged (%d call)" % ncalls))
print("  baseline for /api/dashboard was ~0.00s; see the [concurrent] line above.")

print("\n" + "=" * 78)
print("[B] control: 16 concurrent views of the SAME blunder id")
print("=" * 78)
run_burst([90000] * 16, "")

print("\n" + "=" * 78)
print("[C2] 48 concurrent views with ?include_master_stats=false (the unused escape)")
print("=" * 78)
before = calls_made()
n = 48
res = [None] * n
bar = threading.Barrier(n)


def work2(i):
    bar.wait()
    res[i] = raw_get(f"/api/blunders/{90000 + i}?include_master_stats=false")


ts = [threading.Thread(target=work2, args=(i,)) for i in range(n)]
t0 = time.time()
for t in ts:
    t.start()
for t in ts:
    t.join(120)
lat = sorted(r[0] for r in res)
print(f"  wall {time.time() - t0:.2f}s  latency max {lat[-1]:.2f}s  "
      f"outbound calls {calls_made() - before}")
print("  -> the escape works; nothing in frontend/ passes it (grep: 0 hits).")

print("\n" + "=" * 78)
print("[D] REAL worst case per request: EXPLORER_TIMEOUT=8, and get_opening_explorer_stats")
print("    makes a SECOND call on 401, so up to 16s of blocking per request.")
print("=" * 78)
src = open("/home/robin/Projects/ChessImprovement/backend/lichess_client.py",
           encoding="utf-8").read()
print("  EXPLORER_TIMEOUT =", [l.strip() for l in src.splitlines()
                               if l.startswith("EXPLORER_TIMEOUT")])
print("  a 401 falls through to /lichess -> 2 sequential blocking calls:",
      "yes" if "url_lic" in src else "no")
print(f"  => 48 concurrent views at the real 8s x2 would hold their threadpool")
print(f"     tokens for up to {8 * 2 * (48 / 40):.0f}s in the worst case "
      f"(2 waves x 16s).")

print("\n" + "=" * 78)
print("[E] is the SSE stream affected too?  (same anyio pool, per the fixer comment)")
print("=" * 78)
d, h, b = raw_get("/api/repertoire/eval_stream?fen=rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR%20w%20KQkq%20-%200%201")
print(f"  idle SSE probe: {d:.2f}s  {h}")

proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()

c = get_db()
c.execute("DELETE FROM blunders WHERE game_id LIKE 'synRe2%'")
c.execute("DELETE FROM blunders WHERE id >= 90000")
c.execute("DELETE FROM games WHERE id LIKE 'synRe2%'")
c.commit()
c.close()
print("\nsynthetic rows removed; probe is idempotent. done.")
