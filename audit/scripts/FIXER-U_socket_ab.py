"""FIXER-U real-socket A/B: the pre-fix snapshot and the patched module, both
booted on their own throwaway uvicorn port, answering the same hostile requests.

This is the proof that the defect was real **over a socket** (not just under
`TestClient`) and that it is gone. Re-runnable and idempotent; port 38281 is
never contacted.

Usage:  /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-U_socket_ab.py
"""
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tests.test_setup  # MUST be first: points CHESS_DB_PATH at a temp DB

import uvicorn

from fixu_lib import free_port, http, post_routes

import tests.test_api_boundary_hardening as suite

RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(("[ok  ] " if ok else "[FAIL] ") + name + (("  -> " + str(detail)) if detail else ""))


# --------------------------------------------------------------------------- #
def probe_grid(module):
    """The (content-type x body) x every-POST-route sweep, over a real socket."""
    originals = (module.LichessClient, module.ContentScout,
                 module.RepertoireManager, module.GameAnalyzer)
    module.LichessClient = suite._RecordingLichess
    module.ContentScout = suite._NoNetworkScout
    module.RepertoireManager = suite._NoSideEffectsManager
    module.GameAnalyzer = suite._NoSideEffectsAnalyzer
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(module.app, host="127.0.0.1", port=port,
                                           log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 30
        while not getattr(server, "started", False) and time.time() < deadline:
            time.sleep(0.05)
        routes = post_routes(module.app)
        print(f"    [socket] {len(routes)} POST /api routes on port {port}")
        for ct_label, ct in suite.CONTENT_TYPES:
            for body_label, body in suite.HOSTILE_BODIES:
                bad, statuses = [], {}
                for path, _ in routes:
                    headers = {"Content-Length": str(len(body)), "Host": "127.0.0.1:38281"}
                    if ct:
                        headers["Content-Type"] = ct
                    status, payload, _hdrs = http(port, "POST", path, body=body,
                                                   headers=headers)
                    statuses[status] = statuses.get(status, 0) + 1
                    if status >= 500:
                        bad.append(f"{path}->{status} {payload[:80]!r}")
                report(f"{ct_label:24s} / {body_label:46s} {statuses}",
                       not bad, bad[:2])
        # The depth bomb, under every content type.
        bomb = (b"[" * 4000) + (b"]" * 4000)
        for ct_label, ct in suite.CONTENT_TYPES:
            headers = {"Content-Length": str(len(bomb)), "Host": "127.0.0.1:38281"}
            if ct:
                headers["Content-Type"] = ct
            status, payload, _hdrs = http(port, "POST", "/api/settings", body=bomb,
                                           headers=headers)
            report(f"depth bomb under {ct_label:24s} is 400", status == 400,
                   f"{status} {payload[:90]!r}")
        # ABUSE-21: a GET body.
        big = b"x" * (3 * 1024 * 1024)
        status, _payload, _hdrs = http(port, "GET", "/api/games/recent", body=big,
                                       timeout=90,
                                       headers={"Content-Type": "text/plain",
                                                "Content-Length": str(len(big)),
                                                "Host": "127.0.0.1:38281"})
        report("3 MiB body on GET /api/games/recent is refused", status in (400, 413), status)
        # ABUSE-18: a read behind a foreign write lock.
        from backend.db import get_db
        blocker = get_db()
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE games SET analyzed_at = analyzed_at "
                        "WHERE id = (SELECT id FROM games LIMIT 1)")
        out = {}

        def call():
            t0 = time.time()
            try:
                out["status"] = http(port, "GET", "/api/games/recent", timeout=60,
                                     headers={"Host": "127.0.0.1:38281"})[0]
            except Exception as exc:
                out["status"] = repr(exc)
            out["elapsed"] = time.time() - t0
        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(10.0)
        blocked = worker.is_alive()
        report("GET /api/games/recent behind a foreign write lock",
               (not blocked) and out.get("status") == 200 and out.get("elapsed", 99) < 3.0,
               "STILL BLOCKED after 10s" if blocked else out)
        blocker.rollback()
        blocker.close()
    finally:
        (module.LichessClient, module.ContentScout,
         module.RepertoireManager, module.GameAnalyzer) = originals
        server.should_exit = True
        thread.join(20)
        suite.purge_markers()


def main():
    import importlib.util
    snapshot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "api_prefix_snapshot_FIXER-U.py")
    spec = importlib.util.spec_from_file_location("pre_fix_api", snapshot_path)
    pre = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pre)

    import backend.api as post

    for label, module in (("BEFORE  (pre-fix snapshot)", pre),
                          ("AFTER   (patched backend.api)", post)):
        print(f"\n=== {label} ===")
        RESULTS.clear()
        probe_grid(module)
        bad = sum(1 for _, ok in RESULTS if not ok)
        print(f"    {len(RESULTS) - bad}/{len(RESULTS)} socket checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
