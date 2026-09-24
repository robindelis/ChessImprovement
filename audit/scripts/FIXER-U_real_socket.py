"""FIXER-U real-socket repro: ABUSE-16 / ABUSE-18 / ABUSE-21 over a REAL
uvicorn socket (not TestClient).

Booted on a scratch port against a throwaway temp DB. Port 38281 is never
contacted. `import tests.test_setup` is the first backend-adjacent import.

Usage:  /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-U_real_socket.py
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import tests.test_setup  # MUST be first

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixu_lib import free_port, http, post_routes  # noqa: E402

import uvicorn  # noqa: E402
import backend.api as module  # noqa: E402
from backend.api import app  # noqa: E402

PORT = free_port()
RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[ok  ] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))


# --------------------------------------------------------------------------- #
# 1. ABUSE-16: hostile body x content type x every POST route
# --------------------------------------------------------------------------- #
FIVE_BYTE_BODIES = [
    ("5-byte raw text", b"hello"),
    ("1-byte NUL", b"\x00"),
    ("lone surrogate", b'{"name":"\xed\xa0\x80"}'),      # CESU-8 / WTF-8 lone surrogate
    ("lone surrogate in query-shaped field", b"\xff\xfe\x00"),
    ("invalid utf-8", b"\xc3\x28"),
    ("json-looking but not json", b"{}}}}"),
]
CONTENT_TYPES = [
    ("application/json", "application/json"),
    ("text/plain", "text/plain"),
    ("no content-type", None),
    ("application/octet-stream", "application/octet-stream"),
]


def probe_bodies():
    routes = post_routes(app)
    print(f"    ({len(routes)} POST /api routes)")
    grid = []
    for ct_label, ct in CONTENT_TYPES:
        for body_label, body in FIVE_BYTE_BODIES:
            worst = []
            for path, _ in routes:
                headers = {"Content-Length": str(len(body))}
                if ct:
                    headers["Content-Type"] = ct
                status, data, _ = http(PORT, "POST", path, body=body, headers=headers)
                if status >= 500:
                    worst.append(f"{path}->{status} {data[:90]!r}")
            ok = not worst
            grid.append((ct_label, body_label, ok, worst))
            report(f"ABUSE-16 {ct_label:24s} / {body_label:34s} no 5xx on all POST routes",
                   ok, worst[:3])
    return grid


def probe_depth_bomb():
    bomb = (b"[" * 4000) + (b"]" * 4000)
    for ct_label, ct in CONTENT_TYPES:
        headers = {"Content-Length": str(len(bomb))}
        if ct:
            headers["Content-Type"] = ct
        status, data, _ = http(PORT, "POST", "/api/settings", body=bomb, headers=headers)
        ok = status == 400
        report(f"ABUSE-16 depth bomb under {ct_label:24s} is 400", ok, f"{status} {data[:110]!r}")


def probe_get_body_cap():
    """ABUSE-21: the 2 MiB cap / depth guard must apply to GET too."""
    payload = b"x" * (3 * 1024 * 1024)
    status, _, _ = http(PORT, "GET", "/api/games/recent", body=payload,
                        headers={"Content-Type": "text/plain",
                                 "Content-Length": str(len(payload))}, timeout=60)
    report("ABUSE-21 3 MiB body on a GET is refused (413/400)", status in (400, 413), status)
    bomb = (b"[" * 4000) + (b"]" * 4000)
    status, _, _ = http(PORT, "GET", "/api/dashboard", body=bomb,
                        headers={"Content-Type": "application/json",
                                 "Content-Length": str(len(bomb))}, timeout=60)
    report("ABUSE-21 depth bomb on a GET is refused (400)", status == 400, status)


def probe_read_route_under_foreign_lock():
    """ABUSE-18: a read must not need the write lock."""
    from backend.db import get_db
    blocker = get_db()
    blocker.execute("BEGIN IMMEDIATE")          # take the DB write lock
    blocker.execute("UPDATE games SET analyzed_at = analyzed_at WHERE id = (SELECT id FROM games LIMIT 1)")
    out = {}

    def call():
        t0 = time.time()
        try:
            out["status"] = http(PORT, "GET", "/api/games/recent", timeout=60)[0]
        except Exception as exc:                # a lock timeout would surface here
            out["status"] = repr(exc)
        out["elapsed"] = time.time() - t0

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(8.0)
    if worker.is_alive():
        report("ABUSE-18 GET /api/games/recent does not block behind a foreign write lock",
               False, "still blocked after 8s")
    else:
        report("ABUSE-18 GET /api/games/recent does not block behind a foreign write lock",
               out.get("status") == 200 and out["elapsed"] < 2.0,
               f"status={out.get('status')} elapsed={out.get('elapsed'):.2f}s")
    blocker.rollback()
    blocker.close()


def probe_boundary_still_holds():
    """ABUSE-03/13 re-run: FIXER-L's boundary must not regress."""
    probes = [
        ("POST /api/repertoire/clear from attacker origin",
         "POST", "/api/repertoire/clear", {"Origin": "http://attacker.example",
                                           "Host": "127.0.0.1:38281"}, None, 403),
        ("GET /api/games/recent from attacker origin",
         "GET", "/api/games/recent", {"Origin": "http://attacker.example",
                                      "Host": "127.0.0.1:38281"}, None, 403),
        ("Origin: null refused",
         "POST", "/api/repertoire/clear", {"Origin": "null", "Host": "127.0.0.1:38281"}, None, 403),
        ("duplicate Origin header refused",
         "POST", "/api/repertoire/clear", {"Origin": "http://127.0.0.1:38281",
                                           "Host": "127.0.0.1:38281"}, None, 200),
        ("Sec-Fetch-Site: cross-site refused",
         "POST", "/api/repertoire/clear", {"Sec-Fetch-Site": "cross-site",
                                           "Host": "127.0.0.1:38281"}, None, 403),
        ("rebound Host refused",
         "GET", "/api/dashboard", {"Origin": "http://127.0.0.1:38281",
                                   "Sec-Fetch-Site": "same-origin",
                                   "Host": "evil.test:38281"}, None, 403),
        ("same-origin GET is served",
         "GET", "/api/dashboard", {"Origin": "http://127.0.0.1:38281",
                                   "Sec-Fetch-Site": "same-origin",
                                   "Host": "127.0.0.1:38281"}, None, 200),
        ("SPA shell still served", "GET", "/", {}, None, 200),
    ]
    for name, method, path, headers, body, expect in probes:
        status, data, _ = http(PORT, method, path, body=body, headers=headers)
        report(f"boundary: {name}", status == expect, f"{status} {data[:80]!r}")
    # no ACAO/ACAC reflection on a refused cross-origin read
    _, _, hdrs = http(PORT, "GET", "/api/dashboard", headers={"Origin": "http://attacker.example",
                                                             "Host": "127.0.0.1:38281"})
    report("boundary: no CORS reflection on refusal",
           "access-control-allow-origin" not in {k.lower() for k in hdrs}, hdrs)
    status, _, hdrs = http(PORT, "GET", "/api/dashboard",
                           headers={"Origin": "http://127.0.0.1:38281", "Host": "127.0.0.1:38281"})
    report("boundary: security headers present",
           status == 200 and "content-security-policy" in {k.lower() for k in hdrs}, hdrs)


def main():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    print(f"# real uvicorn socket on 127.0.0.1:{PORT}, DB={os.environ.get('CHESS_DB_PATH')}")
    try:
        probe_bodies()
        probe_depth_bomb()
        probe_get_body_cap()
        probe_read_route_under_foreign_lock()
        probe_boundary_still_holds()
    finally:
        server.should_exit = True
        thread.join(10)
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed on {module.__name__}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
