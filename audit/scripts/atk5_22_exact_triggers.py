#!/usr/bin/env python3
"""ATK-5-22: exact minimal triggers for the 422-handler 500 (ATK-5-19/20/21).

Two independent defects in FIXER-L's new error layer, plus a third that is
new: the same UnicodeEncodeError is reachable from a **GET query parameter**,
which is prefetch/cacheable and needs no body at all.

Isolated temp DB, idempotent.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

soft = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")
raw = TestClient(app, raise_server_exceptions=True, base_url="http://testserver")
JS = {"Content-Type": "application/json"}
S = b'"\xed\xa0\x80"'          # lone high surrogate as a JSON string
DEEP = "[" * 4000 + "]" * 4000

print("=" * 78)
print("ATK-5-22  exact triggers")
print("=" * 78)

print("\n[A] POST, Content-Type: application/json -- what top-level shape triggers it?")
SHAPES = [
    ("bare string surrogate", S),
    ("list of surrogate", b"[" + S + b"]"),
    ("int in list", b"[1]"),
    ("bare string plain", b'"hello"'),
    ("bare int", b"5"),
    ("bare true", b"true"),
    ("bare null", b"null"),
    ("empty list", b"[]"),
    ("empty object", b"{}"),
    ("object w/ surrogate field", b'{"a":' + S + b"}"),
    ("object w/ surrogate + ok", b'{"source":' + S + b',"repertoire":"white"}'),
    ("surrogate as object KEY", b'{' + S + b':1}'),
    ("nested list of surrogate", b"[[" + S + b"]]"),
]
for label, b in SHAPES:
    r = soft.post("/api/scout/check_tree", content=b, headers=JS)
    print(f"    {label:26} {len(b):>3}B -> {r.status_code}"
          f"{'   <<<< 500' if r.status_code >= 500 else ''}   {r.text[:70]}")

print("\n[B] POST, lying / absent Content-Type -> `input` arrives as bytes")
for label, ct in (("no content-type", None), ("text/plain", "text/plain"),
                  ("application/octet-stream", "application/octet-stream"),
                  ("form-urlencoded", "application/x-www-form-urlencoded")):
    for blabel, b in (("1 byte 0xff", b"\xff"), ("surrogate", S),
                      ("deep nest", DEEP.encode())):
        kw = {"content": b}
        if ct:
            kw["headers"] = {"Content-Type": ct}
        r = soft.post("/api/scout/check_tree", **kw)
        print(f"    {label:26} {blabel:14} -> {r.status_code}"
              f"{'   <<<< 500' if r.status_code >= 500 else ''}")

print("\n[C] THE NEW ONE: GET query parameter, no body at all")
GETS = [
    ("/api/blunders", {"category": "\ud800"}),
    ("/api/blunders", {"category": "a\ud800b"}),
    ("/api/blunders", {"category": "\ud83d"}),
    ("/api/dashboard", {"color": "\ud800"}),
    ("/api/repertoire/train/next", {"color": "\ud800"}),
    ("/api/repertoire/train/next", {"opening": "\ud800"}),
    ("/api/repertoire/train/progress", {"color": "\ud800"}),
    ("/api/repertoire/explore", {"fen": "\ud800"}),
    ("/api/repertoire/explore", {"ratings": "\ud800"}),
    ("/api/scout/search", {"q": "\ud800"}),
    ("/api/scout/recent", {"source_type": "\ud800"}),
    ("/api/repertoire/game_gaps", {"color": "\ud800"}),
    ("/api/games/recent", {"limit": "\ud800"}),
    ("/api/train/paths", {"color": "\ud800"}),
    ("/api/repertoire/tree", {"repertoire": "\ud800"}),
]
n500 = 0
for path, params in GETS:
    r = soft.get(path, params=params)
    if r.status_code >= 500:
        n500 += 1
    print(f"    {r.status_code}  GET {path} {json.dumps(params)}"
          f"{'   <<<< 500' if r.status_code >= 500 else ''}")
print(f"    -> {n500}/{len(GETS)} GET routes 500 on a percent-encoded surrogate")

print("\n[D] does the raw client see the exception for the GET case?")
for path, params in (("/api/repertoire/explore", {"fen": "\ud800"}),
                     ("/api/scout/search", {"q": "\ud800"})):
    try:
        r = raw.get(path, params=params)
        print(f"    {path} -> {r.status_code} (no exception)")
    except Exception as e:
        print(f"    {path} -> {type(e).__name__}: {str(e)[:90]}")

print("\n[E] how deep does the surrogate nest have to be to matter? (a normal value is safe)")
for label, v in (("lone surrogate", "\ud800"),
                 ("valid astral char", "\U0001F600"),
                 ("surrogate pair written as two escapes", "\ud83d\ude00"),
                 ("BMP char", "é")):
    r = soft.get("/api/repertoire/explore", params={"fen": v})
    print(f"    {label:38} -> {r.status_code}")
