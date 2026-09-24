#!/usr/bin/env python3
"""ATK-5 re2-3: LocalBoundaryMiddleware edge cases (the ABUSE-03 fix).

Everything runs over a REAL uvicorn socket in this process's own event loop, so
the middleware sees genuine ASGI headers.  No real network, no Stockfish.

Attacked:
  1. Origin parsing: case, trailing dot, userinfo, IPv6 literal, non-numeric and
     absurd ports, multiple Origin headers, `Origin: null` vs the literal string.
  2. Host parsing: IPv6 literal, trailing dot, absent, userinfo.
  3. Sec-Fetch-Site present-but-lying, and the `browser_driven` scoping.
  4. Verb/method mismatches: which ROUTES mutate, and on what HTTP verb.
  5. Whether any mutating route escapes the boundary.

Isolated temp DB, idempotent.
"""
import os
import sys
import json
import time
import socket
import threading
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.api import app  # noqa: E402
from backend.db import get_db  # noqa: E402
import uvicorn  # noqa: E402

PORT = 39447
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
HOST = f"127.0.0.1:{PORT}"
LOCAL = HOST


def raw(req: bytes, label="", read_s=6.0):
    s = socket.create_connection(("127.0.0.1", PORT), 5)
    s.settimeout(read_s)
    buf = b""
    try:
        # sendall can raise once the server aborts mid-body (that IS the 413
        # behaviour under test), so keep whatever we managed to read.
        try:
            s.sendall(req)
        except (BrokenPipeError, ConnectionResetError):
            pass
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
    if not buf:
        return "<NO RESPONSE / connection closed>", b""
    head = buf.split(b"\r\n", 1)[0].decode("latin-1")
    return head, buf


def get(path="/api/settings", extra=""):
    return raw(f"GET {path} HTTP/1.1\r\nHost: {LOCAL}\r\n{extra}Connection: close\r\n\r\n"
               .encode())


def post(path="/api/repertoire/clear", body=b"{}", extra=""):
    return raw((f"POST {path} HTTP/1.1\r\nHost: {LOCAL}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                f"{extra}Connection: close\r\n\r\n").encode() + body)


print("=" * 78)
print("[1] Origin parsing -- is anything that is NOT loopback let through?")
print("=" * 78)
CLEAN = get("/api/settings")[0]
print(f"    baseline (no Origin)                : {CLEAN}")
ORIGINS = [
    ("http://127.0.0.1:%d" % PORT, "self, exact"),
    ("HTTP://127.0.0.1:%d" % PORT, "uppercase scheme+host"),
    ("http://127.0.0.1:%d/" % PORT, "trailing slash"),
    ("http://127.0.0.1.:%d" % PORT, "trailing dot in host"),
    ("http://user@127.0.0.1:%d" % PORT, "userinfo prefix"),
    ("http://user:pass@127.0.0.1:%d" % PORT, "user:pass@ prefix"),
    ("http://[::1]:%d" % PORT, "IPv6 loopback literal"),
    ("http://[::1]" , "IPv6 loopback, no port"),
    ("http://localhost:%d" % PORT, "localhost"),
    ("http://127.0.0.1:0" , "port 0"),
    ("http://127.0.0.1:99999999" , "absurd port"),
    ("http://127.0.0.1:38281x" , "non-numeric port"),
    ("http://127.0.0.1.evil.com:%d" % PORT, "suffix confusion"),
    ("http://evil.com@127.0.0.1:%d" % PORT, "evil.com@ then loopback"),
    ("http://127.0.0.1@evil.com:%d" % PORT, "loopback@ then evil.com"),
    ("https://127.0.0.1:%d" % PORT, "https loopback"),
    ("null", "literal null"),
    ("NULL", "literal NULL"),
    ("file://", "file scheme"),
    ("", "empty Origin"),
    ("http://127.0.0.2:%d" % PORT, "other loopback IP"),
    ("http://[::ffff:127.0.0.1]:%d" % PORT, "IPv4-mapped IPv6"),
]
for origin, note in ORIGINS:
    st, _ = get("/api/settings", extra=f"Origin: {origin}\r\n")
    verdict = "ALLOWED" if "200" in st else ("blocked" if "403" in st else st)
    flag = ""
    # a non-loopback origin that is allowed is the interesting case
    hostish = origin.replace("http://", "").split("@")[-1].split("/")[0]
    looks_local = ("127.0.0.1" in hostish or "localhost" in hostish or "[::1]" in hostish
                   or hostish == "")
    if "ALLOWED" == verdict and not looks_local and origin not in ("", "null", "NULL", "file://"):
        flag = "   <<<< NON-LOOPBACK ALLOWED"
    print(f"    {str(origin)[:34]:36} {verdict:8}  {note}{flag}")

print("\n    multiple Origin headers (smuggling attempt):")
for combo in (f"Origin: http://127.0.0.1:{PORT}\r\nOrigin: http://evil.com",
              f"Origin: http://evil.com\r\nOrigin: http://127.0.0.1:{PORT}"):
    st, _ = get("/api/settings", extra=combo + "\r\n")
    print(f"      {combo.replace(chr(13)+chr(10),' | '):58} -> "
          f"{'ALLOWED' if '200' in st else 'blocked' if '403' in st else st}")

print("\n" + "=" * 78)
print("[2] Host header parsing (DNS-rebinding defence)")
print("=" * 78)
HOSTS = [
    (f"127.0.0.1:{PORT}", "loopback ip+port"),
    ("127.0.0.1", "loopback ip, no port"),
    ("localhost", "localhost"),
    ("[::1]:%d" % PORT, "IPv6 literal"),
    ("[::1]", "IPv6 literal, no port"),
    ("127.0.0.1.:%d" % PORT, "trailing dot"),
    ("127.0.0.1.evil.com", "suffix confusion"),
    ("evil.com", "public host"),
    ("", "empty Host"),
    ("evil.com@127.0.0.1", "userinfo then loopback"),
]
for h, note in HOSTS:
    # a browser-driven request (has Sec-Fetch-Mode) so the Host check is scoped in
    req = (f"GET /api/settings HTTP/1.1\r\nHost: {h}\r\nSec-Fetch-Mode: cors\r\n"
           f"Connection: close\r\n\r\n").encode()
    st, _ = raw(req)
    verdict = "ALLOWED" if "200" in st else ("blocked" if "403" in st else st)
    print(f"    {str(h)[:28]:30} {verdict:8}  {note}")

print("\n" + "=" * 78)
print("[3] Sec-Fetch-Site lying, and the browser_driven scoping")
print("=" * 78)
for sfs in ("same-origin", "none", "same-site", "cross-site", "cross-site-abc", "SAME-SITE"):
    st, _ = get("/api/settings", extra=f"Sec-Fetch-Site: {sfs}\r\n")
    print(f"    Sec-Fetch-Site: {sfs:16} -> {'ALLOWED' if '200' in st else 'blocked' if '403' in st else st}")
print("\n    cross-origin POST with NO fetch metadata at all (a non-browser client):")
st, _ = post("/api/repertoire/clear", extra="")
print(f"      {st}  <- no Origin means no Origin check; that is the documented design")
print("    cross-origin POST with only Sec-Fetch-Site: same-site (browser-shaped lie):")
st, _ = post("/api/repertoire/clear", extra="Sec-Fetch-Site: same-site\r\n")
print(f"      {st}")
print("    cross-origin POST with Origin: null (sandboxed iframe / file://):")
st, _ = post("/api/repertoire/clear", extra="Origin: null\r\n")
print(f"      {st}")

print("\n" + "=" * 78)
print("[4] VERB vs BEHAVIOUR: which routes MUTATE, and on which verb?")
print("=" * 78)
# Seed something observable, then see which verbs can change it.
c = get_db()
c.execute("DELETE FROM settings WHERE key='atk5_verb_probe'")
c.commit()
c.close()


def settings_value():
    cc = get_db()
    try:
        r = cc.execute("SELECT value FROM settings WHERE key='atk5_verb_probe'").fetchone()
        return r[0] if r else None
    finally:
        cc.close()


MUTATORS = [
    ("POST", "/api/settings", b'{"lichess_username":"atk5_verb_probe"}'),
    ("PUT", "/api/settings", b'{"lichess_username":"atk5_verb_probe"}'),
    ("PATCH", "/api/settings", b'{"lichess_username":"atk5_verb_probe"}'),
    ("GET", "/api/settings", None),
    ("DELETE", "/api/settings", None),
]
print(f"    {'verb':7} {'route':26} {'status':10} stored value afterwards")
for verb, path, body in MUTATORS:
    b = body or b""
    req = (f"{verb} {path} HTTP/1.1\r\nHost: {LOCAL}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(b)}\r\n"
           f"Connection: close\r\n\r\n").encode() + b
    st, _ = raw(req)
    print(f"    {verb:7} {path:26} {st.replace('HTTP/1.1 ',''):10} "
          f"{settings_value()!r}")
    cc = get_db()
    cc.execute("DELETE FROM settings WHERE key='atk5_verb_probe'")
    cc.commit()
    cc.close()

print("\n    the SSE GET routes -- do they WRITE? (trace the app's own connection)")
import backend.db as _dbm  # noqa: E402
_traced = []
_real = _dbm.get_db


def _t():
    cc = _real()
    cc.set_trace_callback(lambda s: _traced.append(s))
    return cc


_dbm.get_db = _t
import backend.api as _apimod  # noqa: E402
_apimod.get_db = _t
for path in ("/api/repertoire/eval_stream?fen=rnbqkbnr%2Fpppppppp%2F8%2F8%2F8%2F8%2FPPPPPPPP%2FRNBQKBNR%20w%20KQkq%20-%200%201",
             "/api/games/sync_recent_stream?max_games=1"):
    _traced.clear()
    raw(f"GET {path} HTTP/1.1\r\nHost: {LOCAL}\r\nConnection: close\r\n\r\n".encode(),
        read_s=4.0)
    w = [s for s in _traced if s.strip().upper().startswith(("UPDATE", "INSERT", "DELETE", "REPLACE"))]
    print(f"      {path[:62]:64} writes={len(w)}")
    for s in w[:3]:
        print(f"          {s.strip()[:110]}")
_dbm.get_db = _real
_apimod.get_db = _real

print("\n" + "=" * 78)
print("[5] is any MUTATING route outside the boundary's method set?")
print("=" * 78)
import re  # noqa: E402
src = open("/home/robin/Projects/ChessImprovement/backend/api.py", encoding="utf-8").read()
routes = re.findall(r'@app\.(get|post|put|patch|delete)\("([^"]+)"', src)
print(f"    BODY_METHODS (the set the body-capper uses): {sorted(__import__('backend.api', fromlist=['x']).BODY_METHODS)}")
print("    routes declared with a verb outside BODY_METHODS that still take a body:")
seen = set()
for verb, path in routes:
    if verb not in ("post", "put", "patch", "delete") and path not in seen:
        seen.add(path)
print(f"      (GET routes cannot carry a body through the cap; a GET with a body is")
print(f"       not capped unless Transfer-Encoding is present -- see [6])")

print("\n" + "=" * 78)
print("[6] GET with a body: is the 2 MiB cap applied?")
print("=" * 78)
big = b"x" * (3 * 1024 * 1024)
req = (f"GET /api/settings HTTP/1.1\r\nHost: {LOCAL}\r\nContent-Type: application/json\r\n"
       f"Content-Length: {len(big)}\r\nConnection: close\r\n\r\n").encode() + big
st, _ = raw(req, read_s=15)
print(f"    GET with a 3 MiB declared Content-Length -> {st}")
req = (f"GET /api/settings HTTP/1.1\r\nHost: {LOCAL}\r\nContent-Type: application/json\r\n"
       f"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n").encode()
chunk = b"a" * 65536
for _ in range(48):
    req += f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n"
req += b"0\r\n\r\n"
st, _ = raw(req, read_s=15)
print(f"    GET with 3 MiB chunked (no Content-Length)  -> {st}")

print("\n" + "=" * 78)
print("[7] header size / slow-loris")
print("=" * 78)
huge = "X-Pad: " + ("A" * 60000) + "\r\n"
st, _ = get("/api/settings", extra=huge)
print(f"    60 KB single header -> {st}")
many = "".join(f"X-P{i}: {'B' * 900}\r\n" for i in range(200))
st, _ = get("/api/settings", extra=many)
print(f"    200 x 900 B headers (~190 KB) -> {st}")

srv.should_exit = True
time.sleep(0.5)
print("\ndone.")
