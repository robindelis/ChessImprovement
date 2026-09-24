"""FIXER-L end-to-end verification over a REAL socket (audit ABUSE-03).

Not a `TestClient`: this starts the real ASGI app under uvicorn on a scratch
port (never 38281) with `CHESS_DB_PATH` pointed at a throwaway temp database, and
drives it with `curl`-shaped requests that carry real browser headers, so the
Host/Origin/Sec-Fetch-Site boundary is exercised exactly as a browser would.

It also proves the Electron shell keeps working: `electron/main.js`
(`applySessionHardening`) *deletes* the server CSP for the trusted main frame and
injects its own `APP_CSP`, so this script asserts the two directive sets are
byte-identical -- the server header must not weaken the shell, and the shell's
override must not be a downgrade.

Usage:
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-L_e2e_origin.py
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PYTHON = "/home/robin/miniconda3/bin/python3"
PORT = 39399  # scratch: 38281 is reserved and must never be contacted
BASE = f"http://127.0.0.1:{PORT}"
EVIL = "http://attacker.example"

failures = []


def check(label, ok, detail=""):
    print(f"[{'ok  ' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)
    return ok


def request(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Host", f"127.0.0.1:{PORT}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def main():
    if os.environ.get("CHESS_DB_PATH"):
        print("refusing to run: CHESS_DB_PATH is already set")
        return 2

    tmp_dir = tempfile.mkdtemp(prefix="chess_e2e_")
    db_path = os.path.join(tmp_dir, "e2e.db")
    env = dict(os.environ, CHESS_DB_PATH=db_path, PYTHONPATH=ROOT)
    env.pop("CHESS_PORT", None)

    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "backend.api:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(120):
            with socket.socket() as probe:
                probe.settimeout(0.5)
                if probe.connect_ex(("127.0.0.1", PORT)) == 0:
                    break
            time.sleep(0.25)
        else:
            print("server did not start")
            print(proc.stdout.read().decode(errors="replace")[-3000:])
            return 2

        print("=" * 78)
        print("1) the real ATK-5 chain, over a real socket")
        print("=" * 78)
        status, headers, body = request(
            "POST", "/api/repertoire/clear", headers={"Origin": EVIL,
                                                       "Sec-Fetch-Site": "cross-site",
                                                       "Sec-Fetch-Mode": "cors"})
        check(f"POST /api/repertoire/clear from {EVIL} is refused",
              status == 403, f"-> {status} {body[:90].decode(errors='replace')}")
        check("no Access-Control-Allow-Origin on the refusal",
              "access-control-allow-origin" not in {k.lower() for k in headers})
        check("no Access-Control-Allow-Credentials anywhere",
              "access-control-allow-credentials" not in {k.lower() for k in headers})

        status, _, body = request("GET", "/api/games/recent?limit=100",
                                  headers={"Origin": EVIL})
        check(f"GET /api/games/recent from {EVIL} is refused", status == 403,
              f"-> {status}")

        status, _, body = request(
            "OPTIONS", "/api/repertoire/clear",
            headers={"Origin": EVIL, "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "content-type"})
        check("cross-origin preflight is refused", status == 403, f"-> {status}")

        # A hostile page cannot even read the dashboard.
        status, headers, body = request("GET", "/api/dashboard", headers={"Origin": EVIL})
        check("cross-origin read of the dashboard is refused", status == 403, f"-> {status}")

        print()
        print("=" * 78)
        print("2) the app itself still works (Electron shell / browser tab)")
        print("=" * 78)
        status, headers, body = request("GET", "/",
                                        headers={"Sec-Fetch-Site": "same-origin"})
        check("GET / (the SPA shell) is 200 text/html",
              status == 200 and b"<html" in body.lower(), f"-> {status}")
        status, _, body = request("GET", "/api/dashboard",
                                  headers={"Sec-Fetch-Site": "same-origin"})
        check("same-origin GET /api/dashboard is 200", status == 200, f"-> {status}")
        status, _, body = request("GET", "/api/repertoire/tree?color=white",
                                  headers={"Sec-Fetch-Site": "same-origin"})
        check("same-origin GET /api/repertoire/tree is 200", status == 200, f"-> {status}")
        status, _, body = request("POST", "/api/repertoire/clear",
                                  headers={"Sec-Fetch-Site": "same-origin"})
        check("same-origin POST /api/repertoire/clear is 200", status == 200,
              f"-> {status} {body[:90].decode(errors='replace')}")
        status, _, body = request("POST", "/api/settings",
                                  {"lichess_username": "e2e-probe"},
                                  headers={"Sec-Fetch-Site": "same-origin"})
        check("same-origin POST /api/settings is 200", status == 200, f"-> {status}")
        status, _, body = request("POST", "/api/scout/check_line",
                                  {"repertoire": "white",
                                   "moves": [{"fen_before": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -",
                                              "uci": "e2e4", "san": "e4"}]},
                                  headers={"Sec-Fetch-Site": "same-origin"})
        check("same-origin POST /api/scout/check_line is 200", status == 200, f"-> {status}")
        # `localhost` spelling, i.e. a browser tab opened on the other loopback name.
        status, _, body = request("POST", "/api/repertoire/clear",
                                  headers={"Origin": f"http://localhost:{PORT}",
                                           "Sec-Fetch-Site": "same-origin"})
        check("POST from the localhost spelling of the origin is 200", status == 200,
              f"-> {status}")
        # A DELETE *with a JSON body* (frontend/scout.js:2101-2109): the body
        # buffering in the boundary middleware must not break it.
        status, _, body = request("DELETE", "/api/repertoire/move",
                                  {"repertoire": "white",
                                   "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -",
                                   "move_uci": "e2e4"},
                                  headers={"Sec-Fetch-Site": "same-origin"})
        check("same-origin DELETE /api/repertoire/move (JSON body) is 200", status == 200,
              f"-> {status} {body[:90].decode(errors='replace')}")
        # A GET that streams (SSE) must not be buffered or delayed.
        started = time.time()
        status, _, body = request("GET", "/api/scout/recent?limit=3",
                                  headers={"Sec-Fetch-Site": "same-origin"})
        check("GET /api/scout/recent is 200 and fast", status == 200
              and (time.time() - started) < 5.0, f"-> {status}")

        print()
        print("=" * 78)
        print("3) security headers on a real response (ABUSE-13)")
        print("=" * 78)
        status, headers, body = request("GET", "/")
        lower = {k.lower(): v for k, v in headers.items()}
        for name, needle in (("content-security-policy", "script-src 'self'"),
                             ("x-content-type-options", "nosniff"),
                             ("x-frame-options", "DENY"),
                             ("referrer-policy", "no-referrer")):
            check(f"{name} present on GET /", name in lower and needle in lower[name],
                  lower.get(name, "<ABSENT>")[:90])

        print()
        print("=" * 78)
        print("4) the Electron shell: the server CSP must not weaken APP_CSP")
        print("=" * 78)
        server_csp = lower["content-security-policy"]
        main_js = open(os.path.join(ROOT, "electron", "main.js"), encoding="utf-8").read()
        block = re.search(r"const APP_CSP = \[(.*?)\]\.join\('; '\);", main_js, re.S)
        assert block, "could not find APP_CSP in electron/main.js"
        directives = re.findall(r'"([^"]+)"', block.group(1))
        app_csp = "; ".join(directives)
        norm = lambda s: [d.strip() for d in s.split(";") if d.strip()]
        check("server CSP == electron APP_CSP (directive for directive)",
              sorted(norm(server_csp)) == sorted(norm(app_csp)),
              f"\n     server: {server_csp}\n     shell : {app_csp}")
        check("the shell deletes any server CSP for the main frame before injecting",
              "delete responseHeaders[key]" in main_js
              and "Content-Security-Policy'] = [APP_CSP]" in main_js)

        print()
        print(f"{'FAILED: ' + ', '.join(failures) if failures else 'ALL CHECKS PASSED'}")
        return 1 if failures else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        for suffix in ("", "-wal", "-shm"):
            path = db_path + suffix
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
