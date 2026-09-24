#!/usr/bin/env python3
"""ATK-5-20: ATK-5-19 case (1) -- the 4-byte lone-surrogate 500 -- at full reach,
plus the escalation: can a lone surrogate be *stored* and then permanently break
a read endpoint?

Isolated temp DB, idempotent.
"""
import os
import sys
import json
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                       # noqa: E402
from backend.api import app  # noqa: E402
from backend.db import get_db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

soft = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")
raw = TestClient(app, raise_server_exceptions=True, base_url="http://testserver")

JS = {"Content-Type": "application/json"}
SUR = b'"\xed\xa0\x80"'          # a lone UTF-16 surrogate, JSON-escaped. 4 bytes.

POSTS = [
    "/api/settings", "/api/sync", "/api/games/sync_recent",
    "/api/repertoire/import_game_opening", "/api/repertoire/move",
    "/api/repertoire/studies", "/api/repertoire/move_meta",
    "/api/repertoire/seed", "/api/repertoire/clear",
    "/api/repertoire/train/attempt", "/api/repertoire/train/recent_opening",
    "/api/repertoire/import_line", "/api/repertoire/check_line",
    "/api/repertoire/check_tree", "/api/scout/analyze",
    "/api/scout/import_repertoire", "/api/scout/check_line",
    "/api/scout/check_tree", "/api/scout/favorite",
    "/api/train/evaluate_move",
]
GETS = ["/api/account", "/api/settings", "/api/dashboard", "/api/games/recent",
        "/api/blunders", "/api/repertoire/explore?fen=" + "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR%20w%20KQkq%20-%200%201",
        "/api/repertoire/tree", "/api/repertoire/studies",
        "/api/repertoire/train/next", "/api/repertoire/train/progress",
        "/api/scout/recent", "/api/repertoire/game_gaps"]

print("=" * 78)
print("ATK-5-20  4-byte lone-surrogate 500, full reach + storage escalation")
print("=" * 78)
print("payload:", SUR, f"({len(SUR)} bytes)  Content-Type: application/json\n")

print("[1] every POST route, body = the 4-byte surrogate")
n500 = 0
for p in POSTS:
    r = soft.post(p, content=SUR, headers=JS)
    if r.status_code >= 500:
        n500 += 1
    print(f"    {r.status_code}  {p}{'   <<<< 500' if r.status_code >= 500 else ''}")
print(f"    -> {n500}/{len(POSTS)} return 5xx from a 4-byte request")

print("\n[2] adjacent surrogate variants on /api/scout/check_tree")
VARIANTS = [
    ("lone high surrogate", b'"\xed\xa0\x80"'),
    ("lone low surrogate", b'"\xed\xb0\x80"'),
    ("surrogate in object", b'{"a":"\xed\xa0\x80"}'),
    ("surrogate + valid field", b'{"source":"\xed\xa0\x80","repertoire":"white"}'),
    ("2 surrogates", b'"\xed\xa0\x80\xed\xa0\x80"'),
    ("surrogate + \\u escape", b'"\\ud800"'),
    ("literal \\ud800 escape", b'"\\ud800"'),
    ("escaped pair (valid)", b'"\\ud83d\\ude00"'),
    ("valid emoji", b'"\xf0\x9f\x8e\xb3"'),
    ("surrogate inside escape", b'"a\\ud800b"'),
]
for label, body in VARIANTS:
    r = soft.post("/api/scout/check_tree", content=body, headers=JS)
    print(f"    {label:26} -> {r.status_code}{'   <<<< 500' if r.status_code >= 500 else ''}"
          f"   {r.text[:90]}")

print("\n[3] escaping exception")
try:
    raw.post("/api/scout/check_tree", content=SUR, headers=JS)
    print("    no exception")
except Exception as e:
    print(f"    {type(e).__name__}: {e}")

print("\n[4] ESCALATION: store a lone surrogate, then read it back")
r = soft.post("/api/repertoire/studies",
              content=b'{"name":"\xed\xa0\x80BAD","repertoire":"white"}', headers=JS)
print(f"    POST /api/repertoire/studies -> {r.status_code}  {r.text[:140]}")
r2 = soft.post("/api/repertoire/studies",
               content=b'{"name":"\xf0\x9f\x8e\xb3 Chess","repertoire":"black"}', headers=JS)
print(f"    POST (valid emoji control)   -> {r2.status_code}  {r2.text[:140]}")

conn = get_db()
try:
    rows = conn.execute("SELECT id, name FROM repertoire_studies").fetchall()
    for row in rows:
        nm = row["name"]
        print(f"    stored study id={row['id']} name={nm!r} "
              f"surrogate={any(0xD800 <= ord(c) <= 0xDFFF for c in nm)}")
    if rows:
        print("\n    -> GET /api/repertoire/studies now:")
        g = soft.get("/api/repertoire/studies")
        print(f"       {g.status_code}  {g.text[:200]}")
        print("    -> GET /api/repertoire/tree?repertoire=white now:")
        g2 = soft.get("/api/repertoire/tree", params={"repertoire": "white"})
        print(f"       {g2.status_code}  {g2.text[:200]}")
finally:
    conn.close()

print("\n[5] same question for GET routes (query params are str, so probably safe)")
for g in GETS:
    url = g.split("?")[0]
    params = {"q": "\ud800", "opening": "\ud800", "color": "\ud800"} if "?" not in g else None
    r = soft.get(g, params=params) if params else soft.get(g)
    print(f"    {r.status_code}  {url}{'   <<<< 500' if r.status_code >= 500 else ''}")

print("\n[6] does a 500 leak anything to the client?")
r = soft.post("/api/scout/check_tree", content=SUR, headers=JS)
print("    body:", r.text[:300])
print("    headers:", dict(r.headers))
