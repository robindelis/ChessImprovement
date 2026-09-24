#!/usr/bin/env python3
"""ATK-5-21: can a lone UTF-16 surrogate be PERSISTED, permanently poisoning a read?

If any field that reaches SQLite goes through a TEXT column, the surrogate may
land in the row; every later GET that re-serialises it would then 500 forever
(a stored, unauthenticated, self-inflicted denial of the study list).

Isolated temp DB, idempotent. Uses a dedicated throwaway DB state per section.
"""
import os
import sys
import json
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.api import app  # noqa: E402
from backend.db import get_db, get_setting, set_setting  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

soft = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")
JS = {"Content-Type": "application/json"}
SUR = b'"\xed\xa0\x80"'

print("=" * 78)
print("ATK-5-21  surrogate persistence / read poisoning")
print("=" * 78)


def body_with(field_json):
    return b'{' + field_json + b'}'


# Each tuple: (label, route, raw JSON body bytes)
STORAGE = [
    ("study name", "/api/repertoire/studies",
     b'{"name":"' + SUR + b'BAD","repertoire":"white"}'),
    ("study description", "/api/repertoire/studies",
     b'{"name":"ok' + SUR + b'","repertoire":"white","description":"' + SUR + b'"}'),
    ("settings lichess_username", "/api/settings",
     b'{"lichess_username":"' + SUR + b'"}'),
    ("settings recent_learned_opening", "/api/settings",
     b'{"recent_learned_opening":"' + SUR + b'"}'),
    ("train/recent_opening", "/api/repertoire/train/recent_opening",
     b'{"opening_name":"' + SUR + b'"}'),
    ("move comment", "/api/repertoire/move",
     b'{"repertoire":"white","fen":"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",'
     b'"move_uci":"e2e4","comment":"' + SUR + b'"}'),
    ("move arrows", "/api/repertoire/move",
     b'{"repertoire":"white","fen":"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",'
     b'"move_uci":"e2e4","arrows":"[{\\"from\\":\\"e2\\",\\"to\\":\\"e4\\",\\"color\\":\\"' + SUR + b'\\"}]"}'),
]

print("\n[1] try to persist a lone surrogate through each storage field")
stored = []
for label, route, payload in STORAGE:
    r = soft.post(route, content=payload, headers=JS)
    status = r.status_code
    flag = "   <<<< 500" if status >= 500 else ""
    print(f"    {label:36} {route:44} -> {status}{flag}")
    if status < 500:
        stored.append((label, route, payload))

print("\n[2] what actually landed in the DB?  (raw bytes, no Python str decoding)")
conn = get_db()
try:
    for table, cols in (("repertoire_studies", ("id", "name", "description")),
                        ("settings", ("key", "value")),
                        ("repertoire_moves", ("id", "comment", "arrows"))):
        for row in conn.execute(f"SELECT {','.join(cols)} FROM {table}"):
            for c in cols:
                v = row[c]
                if isinstance(v, str):
                    has_sur = any(0xD800 <= ord(ch) <= 0xDFFF for ch in v)
                    if has_sur:
                        print(f"    !! {table}.{c} row={row[cols[0]]} CONTAINS a lone surrogate: "
                              f"{v.encode('utf-8', 'surrogateescape')!r}")
                        stored.append((f"{table}.{c}", table, None))
finally:
    conn.close()

print("\n[3] re-read every read endpoint that would serialise it")
READS = [
    ("studies", "/api/repertoire/studies", None),
    ("settings", "/api/settings", None),
    ("tree white", "/api/repertoire/tree", {"repertoire": "white"}),
    ("train/progress", "/api/repertoire/train/progress", {"repertoire": "white"}),
    ("train/openings", "/api/repertoire/train/openings", None),
    ("dashboard", "/api/dashboard", None),
    ("blunders", "/api/blunders", None),
    ("games/recent", "/api/games/recent", None),
    ("scout/recent", "/api/scout/recent", None),
    ("train/next", "/api/repertoire/train/next", {"repertoire": "white"}),
]
for label, path, params in READS:
    r = soft.get(path, params=params)
    flag = "   <<<< 500" if r.status_code >= 500 else ""
    print(f"    {r.status_code}  GET {path}{flag}  {r.text[:80] if r.status_code >= 500 else ''}")

print("\n[4] is the surrogate value readable AT ALL (i.e. truly poisoned, or just skipped)?")
r = soft.get("/api/settings")
print("    GET /api/settings ->", r.status_code)
try:
    print("    lichess_username repr:",
          repr(json.loads(r.text).get("lichess_username")) if r.status_code == 200 else "<n/a>")
except Exception as e:
    print("    could not parse:", e)
r = soft.get("/api/repertoire/studies")
print("    GET /api/repertoire/studies ->", r.status_code)
if r.status_code == 200:
    for s in json.loads(r.text):
        print("    study name repr:", repr(s["name"]))
else:
    print("    body:", r.text[:150])

print("\n[5] same probe straight through set_setting() (bypasses pydantic entirely)")
try:
    set_setting("recent_learned_opening", "\ud800BAD")
    print("    set_setting accepted it; reading back from sqlite:")
    c2 = sqlite3.connect(os.environ["CHESS_DB_PATH"])
    row = c2.execute("SELECT value FROM settings WHERE key='recent_learned_opening'").fetchone()
    print("    stored:", repr(row[0]) if row else None)
    c2.close()
except Exception as e:
    print(f"    set_setting raised {type(e).__name__}: {e}")

print("\ndone.")
