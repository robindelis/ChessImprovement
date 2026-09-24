#!/usr/bin/env python3
"""ATK-5 re2-8: the decisive measurement of the 300 s engine wait.

Earlier passes were confounded: once `explorer_cache` was warm, the request never
needed an engine, so the "wait" was 0.00 s.  This version:

  * clears `explorer_cache` before every measured request, so the engine is
    ALWAYS genuinely required;
  * uses a DISTINCT FEN per request, so no memo or DB row can be reused;
  * holds the 2-slot pool with genuinely-blocking analyses;
  * measures the pure pool mechanism AND the end-to-end route.

The claim under test: `SYNC_POOL_WAIT_TIMEOUT = 300.0` (evaluator.py:126) means a
caller blocks a worker thread for up to 5 MINUTES before it can be told the
engine is busy -- which is exactly what FIXER-B's comment at api.py:1036 says
must not happen ("must not be waited out for 300 s").

Isolated temp DB, own-process server, no real Stockfish, idempotent.
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
PORT = int(os.environ.get("ATK5_PORT", "39455"))
WORK = float(os.environ.get("ATK5_WORK", "3.0"))
WAIT = os.environ.get("ATK5_WAIT", "300")
DB = require_isolated_db()
print(f"isolated DB: {DB}   pool wait_timeout={WAIT}s   fake analysis={WORK}s\n")

env = dict(os.environ)
env.update({"CHESS_DB_PATH": DB, "ATK5_WORK": str(WORK), "ATK5_WAIT": WAIT,
            "PYTHONPATH": HERE, "PYTHONWARNINGS": "ignore"})
log = open("/tmp/opencode/atk5_re2_8.log", "w")
proc = subprocess.Popen(
    ["/home/robin/miniconda3/bin/python3", "-m", "atk5_re2_8_server", str(PORT)],
    env=env, stdout=log, stderr=subprocess.STDOUT, cwd=HERE)
for _ in range(800):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        break
    except OSError:
        time.sleep(0.05)
time.sleep(0.6)
if proc.poll() is not None:
    print("SERVER DIED:\n", open("/tmp/opencode/atk5_re2_8.log").read()[-2500:])
    sys.exit(1)
print(f"server pid {proc.pid} on {PORT}\n")


def timed(path, read_s=430.0):
    t0 = time.time()
    s = socket.create_connection(("127.0.0.1", PORT), 5)
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
        return time.time() - t0, "TIMEOUT(never answered)", b""
    except (ConnectionResetError, OSError):
        pass
    finally:
        s.close()
    if not buf:
        return time.time() - t0, "<no response>", b""
    return time.time() - t0, buf.split(b"\r\n", 1)[0].decode("latin-1"), buf


def clear_cache():
    c = get_db()
    c.execute("DELETE FROM explorer_cache")
    c.commit()
    c.close()


# 12 structurally distinct positions (different piece placement) so nothing is shared
import chess  # noqa: E402

# Enumerate every position reachable in the first 3 plies -- all distinct, all legal.
FENS = []
def walk(b, depth):
    if depth == 0:
        FENS.append(" ".join(b.fen().split()[:4]))
        return
    for m in b.legal_moves:
        b.push(m)
        walk(b, depth - 1)
        b.pop()
walk(chess.Board(), 3)
FENS = list(dict.fromkeys(FENS))
print(f"prepared {len(FENS)} distinct positions (all 3-ply openings)")


def explore(fen4):
    from urllib.parse import quote
    return "/api/repertoire/explore?fen=" + quote(fen4, safe="") + "&color=black"


print("=" * 78)
print("[A] COLD CONTROL: one request, empty cache, idle pool  (the work itself)")
print("=" * 78)
clear_cache()
d, h, _ = timed(explore(FENS[0]))
print(f"    {d:7.2f}s  {h}   <- one analysis, no contention. This is the floor.")

print("\n" + "=" * 78)
print("[B] SATURATED: 6 requests in flight, then a 7th on a COLD position")
print("=" * 78)
for f in FENS[:6]:
    clear_cache()
res = {}


def go(i, f):
    res[i] = timed(explore(f))


HOLD = min(6, len(FENS) - 2)
print(f'    using {HOLD} holder positions')
ts = [threading.Thread(target=go, args=(i, FENS[i])) for i in range(HOLD)]
for t in ts:
    t.start()
time.sleep(0.8)
clear_cache()
print(f"    pool is now saturated (2 slots busy, {HOLD - 2} queued). Firing one more\n"
      "    on a fresh cold position -- this is the one that must WAIT:\n")
t0 = time.time()
d, h, b = timed(explore(FENS[HOLD + 1]), read_s=430)
print(f"    extra request: {d:7.2f}s  {h}")
for t in ts:
    t.join(600)
print(f"\n    the 6 holders: {sorted(round(v[0], 1) for v in res.values())}")
print(f"    -> the 7th waited {d:.1f}s for an engine, with pool wait_timeout={WAIT}s.")
print(f"       At the shipped default that ceiling is {WAIT}s = 5 minutes.")

print("\n" + "=" * 78)
print("[C] the same with a SHORT wait -- proves it is the wait, not the work")
print("=" * 78)
proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
time.sleep(0.4)
env["ATK5_WAIT"] = "2"
P2 = PORT + 1
log2 = open("/tmp/opencode/atk5_re2_8b.log", "w")
proc2 = subprocess.Popen(
    ["/home/robin/miniconda3/bin/python3", "-m", "atk5_re2_8_server", str(P2)],
    env=env, stdout=log2, stderr=subprocess.STDOUT, cwd=HERE)
for _ in range(800):
    try:
        socket.create_connection(("127.0.0.1", P2), 0.2).close()
        break
    except OSError:
        time.sleep(0.05)
time.sleep(0.6)
if proc2.poll() is not None:
    print("    (control server failed)")
else:
    def timed2(path, read_s=200.0):
        t0 = time.time()
        s = socket.create_connection(("127.0.0.1", P2), 5)
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

    r2 = {}

    def go2(i, f):
        r2[i] = timed2(explore(f))

    ts2 = [threading.Thread(target=go2, args=(i, FENS[i])) for i in range(HOLD)]
    for t in ts2:
        t.start()
    time.sleep(0.8)
    clear_cache()
    d, h, b = timed2(explore(FENS[HOLD + 1]), read_s=200)
    print(f"    extra request: {d:7.2f}s  {h}")
    for t in ts2:
        t.join(300)
    proc2.terminate()
    try:
        proc2.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc2.kill()

print("\n" + "=" * 78)
print("[D] SOURCE FACTS (no measurement needed)")
print("=" * 78)
src = open("/home/robin/Projects/ChessImprovement/backend/evaluator.py",
           encoding="utf-8").read().splitlines()
for i, l in enumerate(src, 1):
    if "SYNC_POOL_WAIT_TIMEOUT" in l or "ASYNC_POOL_WAIT_TIMEOUT" in l:
        print(f"    evaluator.py:{i}  {l.strip()}")
print()
asrc = open("/home/robin/Projects/ChessImprovement/backend/api.py",
            encoding="utf-8").read().splitlines()
for i in range(1029, 1042):
    print(f"    api.py:{i+1}  {asrc[i][:104]}")
print("\ndone.")
