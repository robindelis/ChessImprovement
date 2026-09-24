#!/usr/bin/env python3
"""ATK-5-23: ground truth for the lone-surrogate crash over a REAL uvicorn socket.

TestClient re-raises what the ASGI app raises, which hides what a real client
sees. This drives a live server on an isolated DB and reports, per payload:
  * the exact HTTP status line (or "connection closed with no response"),
  * whether the response is a well-formed body,
  * the uvicorn-side exception class,
  * whether the server is still alive afterwards (i.e. is this fatal or per-request).

Isolated temp DB, idempotent.
"""
import os
import sys
import json
import socket
import threading
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.api import app  # noqa: E402
import uvicorn  # noqa: E402

PORT = 39429
_exc_lines = []


class _Capture(logging.Handler):
    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return
        if record.exc_info:
            msg += " || " + record.exc_info[0].__name__ + ": " + str(record.exc_info[1])[:200]
        _exc_lines.append(f"{record.levelname}: {msg[:300]}")


uvicorn_logger = logging.getLogger("uvicorn.error")
uvicorn_logger.addHandler(_Capture())
uvicorn_logger.setLevel(logging.INFO)

cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="info", access_log=False)
srv = uvicorn.Server(cfg)
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(600):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        break
    except OSError:
        time.sleep(0.05)
time.sleep(0.4)
print("server up on", PORT)

HOST = f"127.0.0.1:{PORT}"


def send(raw: bytes, label):
    _exc_lines.clear()
    s = socket.create_connection(("127.0.0.1", PORT), 5)
    s.settimeout(10)
    try:
        s.sendall(raw)
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
    except Exception as e:
        buf = b""
        print(f"  {label}\n     CLIENT ERROR {type(e).__name__}: {e}")
    finally:
        s.close()
    head = buf.split(b"\r\n\r\n", 1)
    status = head[0].split(b"\r\n")[0].decode("latin-1") if head and head[0] else "<NO RESPONSE>"
    body = head[1][:120].decode("utf-8", "replace") if len(head) > 1 else ""
    print(f"  {label}")
    print(f"     status : {status}")
    print(f"     body   : {body!r}")
    interesting = [l for l in _exc_lines
                   if "Traceback" in l or "Error" in l or "Exception" in l
                   or "surrogate" in l or "UnicodeEncode" in l]
    for l in interesting[:4]:
        print(f"     server : {l}")
    if not interesting:
        print("     server : (no error logged)")
    return status


def get(path_qs):
    return (f"GET {path_qs} HTTP/1.1\r\nHost: {HOST}\r\n"
            f"Connection: close\r\n\r\n").encode()


def post(body, ct="application/json"):
    h = (f"POST /api/scout/check_tree HTTP/1.1\r\nHost: {HOST}\r\n"
         f"Content-Type: {ct}\r\nContent-Length: {len(body)}\r\n"
         f"Connection: close\r\n\r\n").encode()
    return h + body


S = b'"\xed\xa0\x80"'
SUR_Q = "%ED%A0%80"     # percent-encoded lone high surrogate

print("=" * 78)
print("ATK-5-23  real-socket ground truth")
print("=" * 78)

print("\n[1] GET with a percent-encoded lone surrogate in the query string")
for path in ("/api/scout/search?q=" + SUR_Q,
             "/api/repertoire/explore?fen=" + SUR_Q,
             "/api/blunders?category=" + SUR_Q,
             "/api/repertoire/train/next?color=" + SUR_Q,
             "/api/dashboard?color=" + SUR_Q,
             "/api/repertoire/tree?repertoire=" + SUR_Q,
             "/api/scout/recent?source_type=" + SUR_Q,
             "/api/train/paths?color=" + SUR_Q,
             "/api/repertoire/game_gaps?color=" + SUR_Q,
             "/api/repertoire/train/progress?color=" + SUR_Q,
             "/api/games/recent?limit=" + SUR_Q,
             "/api/repertoire/explore?ratings=" + SUR_Q):
    send(get(path), path)

print("\n[2] POST variants")
send(post(S), "POST check_tree  body=\"\xed\xa0\x80\"  (5 bytes, ct=application/json)")
send(post(b"[" + S + b"]"), "POST check_tree  body=[\"\xed\xa0\x80\"]  (7 bytes)")
send(post(b'{"source":"\xed\xa0\x80","repertoire":"white"}'),
     "POST check_tree  body={\"source\":\"\\ud800\",...}  (ct=application/json)")
send(post(b"\xff", ct="text/plain"), "POST check_tree  body=\\xff  (ct=text/plain)")
send(post(b"[" * 4000 + b"]" * 4000, ct="text/plain"),
     "POST check_tree  body=4000-deep  (ct=text/plain)  [ABUSE-08 re-open]")
send(post(b"[" * 4000 + b"]" * 4000),
     "POST check_tree  body=4000-deep  (ct=application/json)  [guard works]")

print("\n[3] control: a valid astral-plane character must be 200/4xx, never 500")
send(get("/api/scout/search?q=%F0%9F%8E%B3"), "GET /api/scout/search?q=<U+1F600 emoji>")
send(get("/api/scout/search?q=%C3%A9"), "GET /api/scout/search?q=<U+00E9 e-acute>")

print("\n[4] is the server still healthy after all of that?")
send(get("/api/settings"), "GET /api/settings (health check)")

srv.should_exit = True
time.sleep(0.6)
print("\ndone.")
