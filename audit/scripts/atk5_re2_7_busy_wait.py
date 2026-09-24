#!/usr/bin/env python3
"""ATK-5 re2-7: the 300 s "bounded" wait -- what a caller actually experiences.

`SYNC_POOL_WAIT_TIMEOUT = 300.0` (evaluator.py:126).  FIXER-B's own comment at
api.py:1036-1037 says the EngineBusy 503 exists because the wait "must not be
waited out for 300 s".  The default *is* 300 s, so the 503 (where it is mapped)
arrives only after 300 s of blocking.

This measures the two routes that DO map EngineBusy -> 503, plus the two that
swallow it, over a real uvicorn socket, with the engine replaced by a slow fake.

Also records a NEGATIVE result honestly: I hypothesised that a 300 s block in a
`def` route would exhaust the 40-token anyio threadpool and starve unrelated
routes.  Under 60 concurrent engine requests, `/api/dashboard` answered in
0.00-0.01 s throughout, so that starvation does NOT occur.  Reported as
disproven, not as a bug.

Isolated temp DB, own-process server, idempotent, no real Stockfish.
"""
import os
import sys
import time
import socket
import threading
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from tests.probe_guard import require_isolated_db  # noqa: E402
from backend.db import get_db  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("ATK5_PORT", "39453"))
WORK = float(os.environ.get("ATK5_WORK", "4.0"))
DB = require_isolated_db()
print(f"isolated DB: {DB}\n")

env = dict(os.environ)
env["CHESS_DB_PATH"] = DB
env["ATK5_WORK"] = str(WORK)
env["ATK5_WAIT"] = os.environ.get("ATK5_WAIT", "300")
env["PYTHONPATH"] = HERE
env["PYTHONWARNINGS"] = "ignore"
log = open("/tmp/opencode/atk5_re2_7.log", "w")
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
    print("SERVER DIED:\n", open("/tmp/opencode/atk5_re2_7.log").read()[-2500:])
    sys.exit(1)
print(f"server pid {proc.pid} on {PORT}; fake analysis {WORK}s; "
      f"pool wait_timeout={env['ATK5_WAIT']}s\n")


def timed(path, method="GET", body=b"", read_s=420.0):
    t0 = time.time()
    try:
        s = socket.create_connection(("127.0.0.1", PORT), 5)
    except Exception as e:
        return time.time() - t0, f"CONNECT-FAIL {type(e).__name__}", b""
    s.settimeout(read_s)
    try:
        h = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
             f"Connection: close\r\n")
        if body:
            h += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        s.sendall((h + "\r\n").encode() + body)
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
    except socket.timeout:
        return time.time() - t0, "TIMEOUT(never answered)", b""
    except (ConnectionResetError, OSError):
        pass
    finally:
        s.close()
    if not buf:
        return time.time() - t0, "<no response>", b""
    return time.time() - t0, buf.split(b"\r\n", 1)[0].decode("latin-1"), buf


EXPLORE = ("/api/repertoire/explore?fen=rnbqkbnr%2Fpppppppp%2F8%2F8%2F4P3%2F8%2FPPPP1PPP%2FRNBQKBNR"
           "%20b%20KQkq%20e3%200%201")
EVAL = ("/api/train/evaluate_move", b'{"fen":"rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",'
                                 b'"move_uci":"g8f6"}')

print("=" * 78)
print("[A] saturate the 2-slot engine pool, then hit each engine-using route")
print("=" * 78)
# Warm the pool and hold both slots with long-running explore calls.
HOLDERS = 6
hold_res = {}


def hold(i):
    hold_res[i] = timed(EXPLORE)


hs = [threading.Thread(target=hold, args=(i,)) for i in range(HOLDERS)]
for t in hs:
    t.start()
time.sleep(1.0)
print(f"    {HOLDERS} holders in flight; pool cap is 2, each analysis {WORK}s\n")

print("    route                              waited        status")
d, h, b = timed(EXPLORE)
print(f"    GET  /api/repertoire/explore       {d:7.2f}s   {h}")
d2, h2, b2 = timed(EVAL[0], method="POST", body=EVAL[1])
print(f"    POST /api/train/evaluate_move      {d2:7.2f}s   {h2}")
if b2:
    print(f"         body: {b2.split(chr(13).encode()+chr(10).encode())[-1][:160]!r}")

for t in hs:
    t.join(600)
print(f"\n    holder latencies: "
      f"{sorted(round(v[0], 1) for v in hold_res.values())}")

print("\n" + "=" * 78)
print("[B] the same, with a SHORT pool wait (what a 503 should feel like)")
print("=" * 78)
proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
time.sleep(0.4)
env["ATK5_WAIT"] = os.environ.get("ATK5_SHORT", "2")
P2 = PORT + 1
log2 = open("/tmp/opencode/atk5_re2_7b.log", "w")
proc2 = subprocess.Popen(
    ["/home/robin/miniconda3/bin/python3", "-m", "atk5_re2_5c_server", str(P2)],
    env=env, stdout=log2, stderr=subprocess.STDOUT, cwd=HERE)
for _ in range(800):
    try:
        socket.create_connection(("127.0.0.1", P2), 0.2).close()
        break
    except OSError:
        time.sleep(0.05)
time.sleep(0.6)
if proc2.poll() is not None:
    print("    (control server failed; skipping)")
else:
    def timed2(path, method="GET", body=b"", read_s=120.0):
        t0 = time.time()
        s = socket.create_connection(("127.0.0.1", P2), 5)
        s.settimeout(read_s)
        try:
            h = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{P2}\r\n"
                 f"Connection: close\r\n")
            if body:
                h += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            s.sendall((h + "\r\n").encode() + body)
            buf = b""
            while True:
                c = s.recv(65536)
                if not c:
                    break
                buf += c
        except socket.timeout:
            return time.time() - t0, "TIMEOUT(never answered)", b""
        except (ConnectionResetError, OSError):
            pass
        finally:
            s.close()
        if not buf:
            return time.time() - t0, "<no response>", b""
        return time.time() - t0, buf.split(b"\r\n", 1)[0].decode("latin-1"), buf

    hr = {}
    hs2 = [threading.Thread(target=lambda i=i: hr.__setitem__(i, timed2(EXPLORE)))
           for i in range(HOLDERS)]
    for t in hs2:
        t.start()
    time.sleep(1.0)
    d, h, _ = timed2(EXPLORE)
    print(f"    GET  /api/repertoire/explore       {d:7.2f}s   {h}")
    d2, h2, b2 = timed2(EVAL[0], method="POST", body=EVAL[1])
    print(f"    POST /api/train/evaluate_move      {d2:7.2f}s   {h2}")
    if b2:
        print(f"         body: {b2.split(chr(13).encode()+chr(10).encode())[-1][:160]!r}")
    for t in hs2:
        t.join(300)
    proc2.terminate()
    try:
        proc2.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc2.kill()

print("\n" + "=" * 78)
print("[C] NEGATIVE RESULT (recorded so it is not re-attempted)")
print("=" * 78)
print("    Hypothesis: a long block in a `def` route exhausts the 40-token anyio")
print("    threadpool and starves unrelated sync routes.")
print("    Measured (atk5_re2_5c_pool_stall.py, 60 concurrent explore requests,")
print("    fake analysis 2 s, pool wait 300 s):")
print("      GET /api/dashboard during the flood : 0.00-0.01 s, HTTP 200, six")
print("                                            consecutive samples over 15 s")
print("      flood latency min/med/max           : 0.16 / 0.31 / 176.12 s")
print("    -> the REQUESTS queue (176 s), but the threadpool is NOT exhausted and")
print("       unrelated routes are NOT starved.  Starvation is disproven; the")
print("       176 s queueing delay is the real defect and is reported as such.")
print("done.")
