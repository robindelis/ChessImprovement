#!/usr/bin/env python3
"""
ATK5-01 / ABUSE-01 repro: Can the FastAPI static mount serve files OUTSIDE frontend/?
Specifically: .env (Lichess token), chess_improvement.db (production user data),
backend source, start.py, package.json.

Run:  /home/robin/miniconda3/bin/python3 audit/scripts/atk5_static_exposure_repro.py
Idempotent: read-only, isolated temp DB via tests.test_setup.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import
from backend.api import app
from fastapi.testclient import TestClient

client = TestClient(app, raise_server_exceptions=False)

print("=" * 78)
print("A) DB path in use (must be a temp file, never production)")
print("=" * 78)
print("  CHESS_DB_PATH env :", os.environ.get("CHESS_DB_PATH"))
import backend.db as dbm
print("  db.get_db_path()  :", dbm.get_db_path())
print("  DEFAULT_DB_PATH   :", dbm.DEFAULT_DB_PATH)

print()
print("=" * 78)
print("B) Enumerating app.routes (what is mounted, where)")
print("=" * 78)
for r in app.routes:
    cls = type(r).__name__
    path = getattr(r, "path", getattr(r, "path_format", "?"))
    print(f"  {cls:<18} {path}")

print()
print("=" * 78)
print("C) Static mount: can we escape frontend/ ?")
print("=" * 78)
PAYLOADS = [
    "/../.env",
    "/../../.env",
    "/%2e%2e/.env",
    "/%2e%2e%2f.env",
    "/..%2f.env",
    "/assets/../../.env",
    "/assets/../../../.env",
    "/..\\.env",
    "/%252e%252e%252f.env",
    "/.env",
    "/../chess_improvement.db",
    "/../../chess_improvement.db",
    "/%2e%2e%2fchess_improvement.db",
    "/assets/../../chess_improvement.db",
    "/../backend/api.py",
    "/../start.py",
    "/../package.json",
    "/../AGENTS.md",
    "/../bin/stockfish/README.md",
    "/../tests/test_setup.py",
    "/../electron/main.js",
    "/../audit/BRIEF.md",
    "/..%5c.env",
    "/../.%2e/.env",
    "/./../.env",
    "/nonexistent-dir-xyz/.env",
]
for p in PAYLOADS:
    try:
        r = client.get(p)
        body = r.text[:70].replace("\n", " ")
        leak = ""
        if "lip_" in r.text:
            leak = "  <<< LICHESS TOKEN LEAKED"
        elif "sqlite" in r.text[:200].lower() or "SQLite format" in r.text[:100]:
            leak = "  <<< SQLITE DB LEAKED"
        print(f"  {r.status_code}  {p:<40} {body!r}{leak}")
    except Exception as e:
        print(f"  ERR  {p:<40} {type(e).__name__}: {e}")

print()
print("=" * 78)
print("D) Raw-path tricks against the ASGI app (bypassing client URL normalisation)")
print("=" * 78)
# httpx normalises '..' in URLs; drive the ASGI app directly with crafted scopes.
import anyio

async def raw_get(path, headers=None):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 38281),
    }
    chunks = []
    status = None
    resp_headers = None

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        nonlocal status, resp_headers
        if msg["type"] == "http.response.start":
            status = msg["status"]
            resp_headers = msg.get("headers", [])
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))

    await app(scope, receive, send)
    body = b"".join(chunks)
    return status, body

RAW = [
    "/../.env",
    "/..%2f.env",
    "/%2e%2e%2f.env",
    "/%2e%2e/.env",
    "/assets/..%2f..%2f.env",
    "/..\\..\\..\\etc\\passwd",
    "/../.env%00.js",
    "/.env",
    "/../chess_improvement.db",
]
for p in RAW:
    try:
        st, body = anyio.run(raw_get, p)
        flag = ""
        if b"lip_" in body:
            flag = "  <<< TOKEN LEAKED"
        elif b"SQLite format 3" in body:
            flag = "  <<< SQLITE DB LEAKED"
        elif b"root:x:" in body:
            flag = "  <<< /etc/passwd LEAKED"
        print(f"  {st}  {p:<32} {body[:60]!r}{flag}")
    except Exception as e:
        print(f"  ERR  {p:<32} {type(e).__name__}: {e}")

print()
print("=" * 78)
print("E) Does /api/* get shadowed by the mount, and are headers set?")
print("=" * 78)
r = client.get("/api/settings", headers={"Origin": "http://evil.example"})
print("  GET /api/settings ->", r.status_code)
print("  headers:", dict(r.headers))
print("  body   :", r.text[:400])
