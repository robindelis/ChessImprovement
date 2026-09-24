#!/usr/bin/env python3
"""ATK-5 re2-1c: threadpool starvation by GET /api/blunders/{id} -- ISOLATED SERVER.

Two earlier attempts and what they taught me (recorded so neither is repeated):
  * re2_1  ran the server in the SAME process as the client threads and measured
    an 87 s burst / 85 s /api/dashboard.  That was client/server GIL contention.
  * re2_1b ran the server in a subprocess, but the child re-ran
    `tests.test_setup`, which made it CREATE A NEW EMPTY DATABASE.  Every
    request correctly 404'd and nothing was measured.  Also a 404 is not proof
    of isolation -- it is proof the row was missing.

This version: the child inherits the parent's already-redirected CHESS_DB_PATH
(the documented "helper subprocess" case), asserts the isolation invariants
before importing any backend module, and the parent VERIFIES from its own
connection that the child can see the seeded row before drawing any conclusion.

Isolated temp DB, no real network, no real Stockfish, idempotent.
"""
import os
import sys
import time
import socket
import subprocess
import threading
import sqlite3

import socket as _sock_forcheck
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.db import get_db  # noqa: E402
from tests.probe_guard import require_isolated_db  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("ATK5_PORT", "39445"))
# Fail loudly if something is already listening: reusing a stale server would
# silently measure the WRONG process.
try:
    socket.create_connection(("127.0.0.1", PORT), 0.3).close()
    raise SystemExit(f"port {PORT} is already in use -- refusing to measure a stale server")
except (ConnectionRefusedError, socket.timeout, OSError):
    pass
LAT = float(os.environ.get("ATK5_LAT", "2.0"))
CALLS_FILE = "/tmp/opencode/atk5_re2_calls.txt"
SEED_N = 60
DB = require_isolated_db()
print(f"isolated DB (fail-closed): {DB}")

# ---------------------------------------------------------------- seed data
conn = get_db()
conn.execute("DELETE FROM blunders WHERE game_id LIKE 'synRe2%'")
conn.execute("DELETE FROM blunders WHERE id >= 90000")
conn.execute("DELETE FROM games WHERE id LIKE 'synRe2%'")
grows, brows = [], []
for i in range(SEED_N):
    gid = f"synRe2G{i:03d}"
    # ONE 4-token position; only the fullmove counter differs. This is the real
    # shape: two games reaching the same position have different move counters,
    # so the raw FEN (the memo/lock key) differs while the position does not.
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
VISIBLE = conn.execute("SELECT COUNT(*) FROM blunders WHERE id >= 90000").fetchone()[0]
conn.close()
print(f"seeded {VISIBLE} blunders (ids 90000..{90000 + SEED_N - 1}), all ONE position")

# ---------------------------------------------------------- server subprocess
env = dict(os.environ)
env["CHESS_DB_PATH"] = DB            # inherit, do NOT re-create
env["ATK5_LAT"] = str(LAT)
env["ATK5_CALLS_FILE"] = CALLS_FILE
env["PYTHONPATH"] = HERE
env["PYTHONWARNINGS"] = "ignore"
if os.path.exists(CALLS_FILE):
    os.remove(CALLS_FILE)
proc = subprocess.Popen(
    ["/home/robin/miniconda3/bin/python3", "-m", "atk5_re2_server", str(PORT)],
    env=env, stdout=open("/tmp/opencode/atk5_re2_srv.log","w"),
    stderr=subprocess.STDOUT, cwd=HERE)
for _ in range(800):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        break
    except OSError:
        time.sleep(0.05)
time.sleep(0.6)
if proc.poll() is not None:
    print(f"SERVER DIED rc={proc.returncode}")
    print(open("/tmp/opencode/atk5_re2_srv.log").read()[-3000:])
    # re-run in the foreground to see the traceback
    import subprocess as _sp
    r = _sp.run(["/home/robin/miniconda3/bin/python3","-m","atk5_re2_server",str(PORT)],
                env=env, cwd=HERE, capture_output=True, timeout=25)
    print("--- foreground rc", r.returncode, "---")
    print(r.stdout.decode()[-2000:]); print(r.stderr.decode()[-3000:])
    sys.exit(1)
print(f"server in its OWN process (pid {proc.pid}) on {PORT}; explorer latency {LAT}s\n")


def calls_made():
    try:
        with open(CALLS_FILE) as fh:
            return int(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def raw_get(path, read_timeout=180.0):
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


# --- PROVE the child sees the seeded data before trusting any measurement ----
print("=" * 78)
print("PRE-FLIGHT: does the child see the seeded rows?")
print("=" * 78)
d, h, b = raw_get("/api/blunders/90000")
ok = "200 OK" in h
print(f"  GET /api/blunders/90000 -> {h}  in {d:.2f}s   "
      f"{'<-- data visible, measurement is valid' if ok else '<-- INVALID MEASUREMENT'}")
if not ok:
    print("  FULL BODY:", b.decode("latin-1")[:1500])
    proc.terminate()
    sys.exit(1)
# A row id that does not exist must 404 -> proves 200 above was a real hit.
d2, h2, _ = raw_get("/api/blunders/999999")
print(f"  GET /api/blunders/999999 -> {h2}  (control: must be 404)")
print(f"  outbound explorer calls so far: {calls_made()}")


def burst(ids, label, probe_paths=(), probe_at=1.0):
    before = calls_made()
    n = len(ids)
    res = [None] * n
    bar = threading.Barrier(n)

    def work(i, bid, q=""):
        bar.wait()
        res[i] = raw_get(f"/api/blunders/{bid}{q}")

    ts = [threading.Thread(target=work, args=(i, b, "")) for i, b in enumerate(ids)]
    t0 = time.time()
    for t in ts:
        t.start()
    probes = []
    if probe_paths:
        time.sleep(probe_at)
        for p in probe_paths:
            probes.append((p, raw_get(p)))
    for t in ts:
        t.join(300)
    wall = time.time() - t0
    lat = sorted(r[0] for r in res if r)
    sts = sorted({r[1].replace("HTTP/1.1 ", "") for r in res if r})
    after = calls_made()
    print(f"\n{label}")
    print(f"  requests {n} | wall {wall:6.2f}s (one call = {LAT}s, fully parallel "
          f"~{LAT:.2f}s)")
    print(f"  latency min/med/max : {lat[0]:.2f} / {lat[len(lat)//2]:.2f} / {lat[-1]:.2f}s")
    print(f"  statuses            : {sts}")
    print(f"  outbound calls      : {after - before} for {n} requests "
          f"({(after - before) / max(n, 1):.2f} per request)")
    for p, (dd, hh, _bb) in probes:
        print(f"  [concurrent] {dd:8.2f}s {hh:24} GET {p}")
    return wall, lat, after - before, probes


CHEAP = ("/api/dashboard", "/api/repertoire/studies", "/api/games/recent?limit=1",
         "/api/settings", "/api/repertoire/tree?repertoire=white")

print("\n" + "=" * 78)
print("BASELINE (idle)")
print("=" * 78)
base = {}
for p in CHEAP:
    d, h, _ = raw_get(p)
    base[p] = d
    print(f"  {d:7.3f}s  {h:24} GET {p}")

print("\n" + "=" * 78)
print("[A/C] 48 concurrent blunder-detail views, all at ONE position")
print("=" * 78)
wall, lat, ncalls, probes = burst([90000 + i for i in range(48)], "", CHEAP)
print(f"\n  single-flight: {'NOT engaged -- %d calls' % ncalls if ncalls > 1 else 'engaged (%d)' % ncalls}")
worst = max(((d, p) for p, (d, h, _b) in probes), default=(0, ""))
print(f"  worst concurrent cheap-route latency: {worst[0]:.2f}s on {worst[1]} "
      f"(baseline {base.get(worst[1], 0):.3f}s) "
      f"-> {worst[0] / max(base.get(worst[1], 1e-9), 1e-9):.0f}x degradation")

print("\n" + "=" * 78)
print("[B] control: 16 concurrent views of the SAME blunder id (the fixed case)")
print("=" * 78)
burst([90000] * 16, "")

print("\n" + "=" * 78)
print("[C2] 48 concurrent with ?include_master_stats=false (escape unused by the app)")
print("=" * 78)
before = calls_made()
n = 48
res = [None] * n
bar = threading.Barrier(n)


def w2(i):
    bar.wait()
    res[i] = raw_get(f"/api/blunders/{90000 + i}?include_master_stats=false")


ts = [threading.Thread(target=w2, args=(i,)) for i in range(n)]
t0 = time.time()
for t in ts:
    t.start()
for t in ts:
    t.join(120)
lat = sorted(r[0] for r in res)
print(f"  wall {time.time() - t0:.2f}s  latency max {lat[-1]:.2f}s  "
      f"outbound calls {calls_made() - before}")

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
