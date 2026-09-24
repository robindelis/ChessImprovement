#!/usr/bin/env python3
"""ATK-5-17: attack the *new* LocalBoundaryMiddleware (FIXER-L's ABUSE-03/07/08/09 fix).

Three questions:
  A. Is `json_max_nesting_depth` itself a CPU amplifier? (2 MB cap x pure-Python
     byte loop, executed BEFORE pydantic, on every body-bearing request.)
  B. Is the depth check bypassable by omitting / lying about `Content-Type`,
     leaving the original ABUSE-08 RecursionError -> 500 reachable?
  C. Is the body cap applied to a chunked / no-Content-Length body, and can the
     buffer be grown past the cap before the abort?

Isolated temp DB, idempotent.
"""
import os
import sys
import time
import json as _json
import socket
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                      # noqa: E402
from backend.api import (  # noqa: E402
    app, json_max_nesting_depth, MAX_BODY_BYTES, MAX_JSON_DEPTH,
    is_allowed_local_origin, is_allowed_request_host,
)
from fastapi.testclient import TestClient  # noqa: E402
import uvicorn  # noqa: E402

print("=" * 78)
print("ATK-5-17  LocalBoundaryMiddleware attack")
print("=" * 78)
print("MAX_BODY_BYTES =", MAX_BODY_BYTES, " MAX_JSON_DEPTH =", MAX_JSON_DEPTH)

# ---------------------------------------------------------------- A: CPU cost
print("\n--- A. json_max_nesting_depth() CPU cost, per body size ---")
for n in (64 * 1024, 256 * 1024, 1024 * 1024, MAX_BODY_BYTES):
    payload = b'{"x":"' + b"a" * (n - 10) + b'"}'          # a *valid*, shallow doc
    t0 = time.perf_counter()
    d = json_max_nesting_depth(payload)
    dt = time.perf_counter() - t0
    print(f"  {n:>9} bytes -> depth {d:>3}  {dt*1000:8.2f} ms   "
          f"({n/max(dt,1e-9)/1e6:.1f} MB/s)")

print("\n  full ASGI request (2 MB valid JSON body, endpoint that just 422s):")
client = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")
big = _json.dumps({"comment": "a" * (MAX_BODY_BYTES - 64)}).encode()
for label, body, ct in (
    ("valid shallow, 2 MB", big, "application/json"),
    ("flat 2 MB of spaces", b" " * (MAX_BODY_BYTES - 1), "application/json"),
    ("2 MB no content-type", big, None),
):
    kw = {"content": body}
    if ct:
        kw["headers"] = {"Content-Type": ct}
    t0 = time.perf_counter()
    try:
        r = client.post("/api/repertoire/move_meta", **kw)
        st = r.status_code
    except Exception as e:
        st = f"EXC {type(e).__name__}"
    print(f"    {label:22} -> {st!s:>8}  {(time.perf_counter()-t0)*1000:8.1f} ms")

# ------------------------------------------------- B: depth-check CT bypass
print("\n--- B. Can the depth guard be bypassed with a lying Content-Type? ---")
deep = "[" * 4000 + "]" * 4000
cases = [
    ("application/json", "application/json"),
    ("text/plain", "text/plain"),
    ("application/x-www-form-urlencoded", "application/x-www-form-urlencoded"),
    ("application/octet-stream", "application/octet-stream"),
    ("json+ld suffix", "application/vnd.api+json"),
    ("charset padded", "application/json; charset=utf-8"),
    ("mixed case", "APPLICATION/JSON"),
    ("leading space", " application/json"),
    ("none", None),
    ("form-data", "multipart/form-data; boundary=xx"),
]
for label, ct in cases:
    kw = {"content": deep}
    if ct is not None:
        kw["headers"] = {"Content-Type": ct}
    t0 = time.perf_counter()
    try:
        r = client.post("/api/scout/check_tree", **kw)
        st, txt = r.status_code, r.text[:110].replace("\n", " ")
    except Exception as e:
        st, txt = f"EXC {type(e).__name__}", str(e)[:110]
    dt = time.perf_counter() - t0
    bad = "  <<<< 500 / RecursionError" if (st == 500 or "EXC" in str(st)
                                           or "recursi" in txt.lower()) else ""
    print(f"  {label:38} -> {st!s:>8} {dt*1000:7.1f}ms  {txt}{bad}")

# -------------------------------------------- C: chunked body / cap bypass
print("\n--- C. raw-socket: chunked body, no Content-Length ---")
PORT = 39417
_stop = threading.Event()


def _serve():
    cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    while not getattr(srv, "started", False) and not _stop.is_set():
        time.sleep(0.02)


t = threading.Thread(target=_serve, daemon=True)
t.start()
for _ in range(400):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        break
    except OSError:
        time.sleep(0.05)
time.sleep(0.3)

HOSTHDR = f"127.0.0.1:{PORT}"


def raw(req: bytes, read_s=3.0, expect_close=True):
    s = socket.create_connection(("127.0.0.1", PORT), 3)
    s.settimeout(read_s)
    s.sendall(req)
    buf = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    finally:
        s.close()
    return buf


def chunked_body(chunks, path="/api/repertoire/move_meta"):
    head = (f"POST {path} HTTP/1.1\r\nHost: {HOSTHDR}\r\n"
            "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
            "Connection: close\r\n\r\n")
    out = head.encode()
    for c in chunks:
        out += f"{len(c):x}\r\n".encode() + c + b"\r\n"
    out += b"0\r\n\r\n"
    return out


print("  [1] chunked 4 MB in 64 KB chunks, no Content-Length:")
r = raw(chunked_body([b'{"comment":"' + b"a" * 65528 + b'"}'] * 64))
print("      ", r.split(b"\r\n")[0] if r else b"<no response / closed>")
print("       -> refused early?" , b"413" in r[:200])

print("  [2] chunked 40 MB (far over the 2 MB cap):")
big_chunks = [b'{"comment":"' + b"a" * 65528 + b'"}'] * 640
t0 = time.perf_counter()
r = raw(chunked_body(big_chunks))
print("      ", r.split(b"\r\n")[0] if r else b"<no response>", f"{(time.perf_counter()-t0)*1000:.0f} ms")
print("       -> 413?" , b"413" in r[:200], " body bytes accepted before abort:",
      "unknown (server aborted mid-stream)" if b"413" in r[:200] else "ALL 40 MB")

print("  [3] duplicate Host headers (last-wins?):")
r = raw(f"GET /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
        f"Host: evil.example.com\r\nConnection: close\r\n\r\n".encode())
print("      ", r.split(b"\r\n")[0] if r else b"<none>")
print("  [4] reverse order (Host: 127.0.0.1 last):")
r = raw(f"GET /api/settings HTTP/1.1\r\nHost: evil.example.com\r\n"
        f"Host: 127.0.0.1:{PORT}\r\nConnection: close\r\n\r\n".encode())
print("      ", r.split(b"\r\n")[0] if r else b"<none>")

print("  [5] Origin: null (sandboxed iframe / file://):")
r = raw(f"POST /api/repertoire/clear HTTP/1.1\r\nHost: {HOSTHDR}\r\n"
        f"Origin: null\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
        f"Connection: close\r\n\r\n{{}}".encode())
print("      ", r.split(b"\r\n")[0] if r else b"<none>")
print("  [6] Origin: null with Sec-Fetch-Site: same-origin (spoofable? browsers do not):")
r = raw(f"POST /api/repertoire/clear HTTP/1.1\r\nHost: {HOSTHDR}\r\n"
        f"Origin: null\r\nSec-Fetch-Site: same-origin\r\nSec-Fetch-Mode: cors\r\n"
        f"Content-Type: application/json\r\nContent-Length: 2\r\n"
        f"Connection: close\r\n\r\n{{}}".encode())
print("      ", r.split(b"\r\n")[0] if r else b"<none>")

print("  [7] Origin allowed via self-origin match on a rebound host:")
for origin, host in (("http://evil.example.com", "evil.example.com"),
                     ("http://evil.example.com:39417", "evil.example.com:39417"),
                     ("http://127.0.0.1.evil.com", "127.0.0.1.evil.com"),
                     ("http://127.0.0.1:80@evil.com", "evil.com"),
                     ("http://localhost:38281", "127.0.0.1:38281")):
    print(f"      origin={origin:32} host={host:24} "
          f"allowed_origin={is_allowed_local_origin(origin, host)!s:5} "
          f"allowed_host={is_allowed_request_host(host)}")

print("\ndone.")
