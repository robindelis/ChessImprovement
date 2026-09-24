#!/usr/bin/env python3
"""
FIXER-E-01 — CSP compatibility harness.

Serves frontend/ as static files with the exact APP_CSP string from
electron/main.js injected as a response header (the same mechanism
applySessionHardening() uses in the Electron main process), so a browser can
tell us whether the policy actually lets the app run.

Port is deliberately NOT 38281 (reserved) and this server never touches
chess_improvement.db -- it only reads files from frontend/.

    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-E-01_csp_server.py 39411
"""
import os
import re
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend")
MAIN_JS = os.path.join(os.path.dirname(FRONTEND), "electron", "main.js")


def read_csp() -> str:
    src = open(MAIN_JS, encoding="utf-8").read()
    m = re.search(r"const APP_CSP = \[(.*?)\]\.join\('; '\);", src, re.S)
    if not m:
        raise SystemExit("could not extract APP_CSP from electron/main.js")
    parts = re.findall(r'"([^"]+)"', m.group(1))
    return "; ".join(parts)


CSP = read_csp()


HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=FRONTEND, **kw)

    def translate_path(self, path):
        # Two fixture files are served out of audit/scripts/ instead of
        # frontend/ so nothing in the real app tree has to be touched.
        clean = path.split("?", 1)[0]
        fixtures = {
            "/__fixer_e_probe.html": "FIXER-E-01_csp_fixture.html",
            "/__fixer_e_probe.js": "FIXER-E-01_csp_fixture.js",
        }
        if clean in fixtures:
            return os.path.join(HERE, fixtures[clean])
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("[csp-server] %s\n" % (fmt % args))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 39411
    print(f"CSP under test:\n  {CSP}\n")
    print(f"Serving {FRONTEND} on http://127.0.0.1:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
