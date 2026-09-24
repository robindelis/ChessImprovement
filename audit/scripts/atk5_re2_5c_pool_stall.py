#!/usr/bin/env python3
"""ATK-5 re2-5c: a 300 s engine wait in a `def` route exhausts the anyio pool.

`SYNC_POOL_WAIT_TIMEOUT = 300.0` (evaluator.py:126).  `SyncEnginePool.acquire()`
blocks a *worker thread* for up to that long, and every `def` route in this app
runs in the anyio threadpool, whose limiter is 40 tokens.  So 40 concurrent
requests that each need an engine can hold every token for 300 s -- after which
even `GET /api/dashboard` (a 2 ms local SQLite read) cannot be served.

This measures exactly that, over a REAL uvicorn socket, with the engine replaced
by a fake checkout so no real Stockfish is ever spawned.

Design notes (so the numbers are not self-deceiving):
  * the server runs in THIS process only for the socket layer; the measurement
    is wall-clock on real sockets, and the fake engine `time.sleep()`s, so the
    GIL is not the bottleneck;
  * a control pass repeats the flood with the pool's wait_timeout set to 0.05 s
    to show the difference is the wait, not the request count;
  * `include`d health probes use SEPARATE connections so they queue the same way
    a real second browser tab would.

Isolated temp DB, idempotent.
"""
import os
import sys
import time
import socket
import threading
import logging
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from tests.probe_guard import require_isolated_db  # noqa: E402
from backend.db import get_db  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("ATK5_PORT", "39451"))
WORK = float(os.environ.get("ATK5_WORK", "3.0"))     # how long one "analysis" takes
N = int(os.environ.get("ATK5_N", "44"))             # >= the 40-token limiter
DB = require_isolated_db()
print(f"isolated DB: {DB}")
print(f"flood N={N} concurrent /api/repertoire/explore, fake analysis {WORK}s each\n")

env = dict(os.environ)
env["CHESS_DB_PATH"] = DB
env["ATK5_WORK"] = str(WORK)
env["ATK5_WAIT"] = os.environ.get("ATK5_WAIT", "300")
env["PYTHONPATH"] = HERE
env["PYTHONWARNINGS"] = "ignore"
log = open("/tmp/opencode/atk5_re2_5c.log", "w")
proc = subprocess.Popen(
    ["/home/robin/miniconda3/bin/python3", "-m", "atk5_re2_5c_server", str(PORT)],
    env=env, stdout=log, stderr=subprocess.STDOUT, cwd=HERE)
for _ in range(800):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        break
    except OSError:
        time.sleep(0.05)
time.sleep(0.6)
if proc.poll() is not None:
    print(f"SERVER DIED rc={proc.returncode}")
    print(open("/tmp/opencode/atk5_re2_5c.log").read()[-3000:])
    import subprocess as _sp
    r = _sp.run(["/home/robin/miniconda3/bin/python3","-m","atk5_re2_5c_server",str(PORT)],
                env=env, cwd=HERE, capture_output=True, timeout=30)
    print("--- foreground rc", r.returncode, "---")
    print(r.stdout.decode()[-2000:]); print(r.stderr.decode()[-3000:])
    sys.exit(1)
print(f"server pid {proc.pid} on {PORT}\n")


def get(path, read_s=200.0, connect_s=5.0):
    t0 = time.time()
    try:
        s = socket.create_connection(("127.0.0.1", PORT), connect_s)
    except Exception as e:
        return time.time() - t0, f"CONNECT-FAIL {type(e).__name__}", b""
    s.settimeout(read_s)
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
    except (ConnectionResetError, OSError):
        pass
    finally:
        s.close()
    if not buf:
        return time.time() - t0, "<no response>", b""
    return time.time() - t0, buf.split(b"\r\n", 1)[0].decode("latin-1"), buf


EXPLORE = ("/api/repertoire/explore?fen=rnbqkbnr%2Fpppppppp%2F8%2F8%2F4P3%2F8%2FPPPP1PPP%2FRNBQKBNR"
           "%20b%20KQkq%20e3%200%201")

print("=" * 78)
print("BASELINE (idle)")
print("=" * 78)
for p in ("/api/dashboard", "/api/settings", "/api/repertoire/studies"):
    d, h, _ = get(p, read_s=30)
    print(f"    {d:7.3f}s  {h:26} GET {p}")

print("\n" + "=" * 78)
print(f"[A] flood {N} concurrent engine-using requests, then probe a cheap route")
print("=" * 78)
res = {}
bar = threading.Barrier(N)


def flood(i):
    bar.wait()
    res[i] = get(EXPLORE, read_s=400)


ts = [threading.Thread(target=flood, args=(i,)) for i in range(N)]
for t in ts:
    t.start()
time.sleep(0.5)
print("    flooding; probing cheap routes REPEATEDLY on separate connections")
print("    (a real second browser tab, polling while the first is busy)")
probes = []
for i in range(6):
    d, h, _ = get("/api/dashboard", read_s=400)
    probes.append(("/api/dashboard", d, h))
    print(f"      t+{0.5 + i * 3.0:5.1f}s  {d:8.2f}s  {h:28} GET /api/dashboard")
    time.sleep(3.0)
d, h, _ = get(EXPLORE, read_s=400)
print(f"      {d:8.2f}s  {h:28} GET <one more explore>")
for t in ts:
    t.join(600)

st = sorted({v[1].replace("HTTP/1.1 ", "") for v in res.values()})
lats = sorted(v[0] for v in res.values())
print(f"\n    flood statuses           : {st}")
print(f"    flood latency min/med/max: {lats[0]:.2f} / {lats[len(lats)//2]:.2f} / {lats[-1]:.2f}s")
worst = max(probes, key=lambda x: x[1])
print(f"    worst cheap-route probe  : {worst[1]:.2f}s on {worst[0]}  (baseline 0.002s)")

print("\n" + "=" * 78)
print("[B] CONTROL: same flood, but with CHESS_ENGINE_WAIT_TIMEOUT=0.05")
print("=" * 78)
proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
time.sleep(0.4)
env["ATK5_WAIT"] = "0.05"
log2 = open("/tmp/opencode/atk5_re2_5c.log", "w")
proc2 = subprocess.Popen(
    ["/home/robin/miniconda3/bin/python3", "-m", "atk5_re2_5c_server", str(PORT + 1)],
    env=env, stdout=log2, stderr=subprocess.STDOUT, cwd=HERE)
P2 = PORT + 1
for _ in range(800):
    try:
        socket.create_connection(("127.0.0.1", P2), 0.2).close()
        break
    except OSError:
        time.sleep(0.05)
time.sleep(0.6)
if proc2.poll() is not None:
    print("SERVER DIED:\n", open("/tmp/opencode/atk5_re2_5c.log").read()[-2000:])
    proc2.terminate()
    sys.exit(0)
print("    server up with a 0.05s wait timeout; repeating the flood")


def get2(path, read_s=200.0):
    t0 = time.time()
    try:
        s = socket.create_connection(("127.0.0.1", P2), 5)
    except Exception as e:
        return time.time() - t0, f"CONNECT-FAIL {type(e).__name__}", b""
    s.settimeout(read_s)
    try:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{P2}\r\n"
                  f"Connection: close\r\n\r\n".encode())
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
    except socket.timeout:
        return time.time() - t0, "TIMEOUT", b""
    except (ConnectionResetError, OSError):
        pass
    finally:
        s.close()
    if not buf:
        return time.time() - t0, "<no response>", b""
    return time.time() - t0, buf.split(b"\r\n", 1)[0].decode("latin-1"), buf


res2 = {}
bar2 = threading.Barrier(N)


def flood2(i):
    bar2.wait()
    res2[i] = get2(EXPLORE)


ts2 = [threading.Thread(target=flood2, args=(i,)) for i in range(N)]
for t in ts2:
    t.start()
time.sleep(0.5)
p2s = []
for i in range(6):
    d, h, _ = get2("/api/dashboard", read_s=400)
    p2s.append(("/api/dashboard", d, h))
    print(f"      t+{0.5 + i * 1.0:5.1f}s  {d:8.2f}s  {h:28} GET /api/dashboard")
    time.sleep(1.0)
for t in ts2:
    t.join(600)
st2 = sorted({v[1].replace("HTTP/1.1 ", "") for v in res2.values()})
print(f"\n    flood statuses           : {st2}")
worst2 = max(p2s, key=lambda x: x[1])
print(f"    worst cheap-route probe  : {worst2[1]:.2f}s on {worst2[0]}")
print("\n    -> the difference between [A] and [B] is the 300 s wait, not the load.")

proc2.terminate()
try:
    proc2.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc2.kill()
print("\ndone.")
