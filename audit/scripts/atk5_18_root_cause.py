#!/usr/bin/env python3
"""ATK-5-18: root-cause the two 500s in FIXER-L's new boundary/validation layer.

  (1) ABUSE-08 regression: the JSON-depth guard is gated on Content-Type, so a
      non-JSON (or absent) Content-Type reaches Starlette's json() -> RecursionError.
  (2) FIXER-L's own `validation_exception_handler` -> JSONResponse can itself raise
      `TypeError: Object of type bytes is not JSON serializable`, turning a 422
      into a 500.

Isolated temp DB, idempotent.
"""
import os
import sys
import json
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                       # noqa: E402
from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

DEEP = "[" * 4000 + "]" * 4000

print("=" * 78)
print("ATK-5-18  root cause")
print("=" * 78)

# ------------------------------------------------------------------ (1)
print("\n(1) Real exception behind the 500 (raise_server_exceptions=True)")
raw = TestClient(app, raise_server_exceptions=True, base_url="http://testserver")
for ct in (None, "text/plain"):
    kw = {"content": DEEP}
    if ct:
        kw["headers"] = {"Content-Type": ct}
    try:
        r = raw.post("/api/scout/check_tree", **kw)
        print(f"    CT={ct!r:12} -> {r.status_code} (no exception)")
    except Exception as e:
        tb = traceback.format_exc().strip().splitlines()
        print(f"    CT={ct!r:12} -> {type(e).__name__}: {str(e)[:80]}")
        for line in tb[-8:]:
            print("        ", line)

# ------------------------------------------------- (2) bytes-in-422 handler
print("\n(2) validation_exception_handler raising on non-JSON-serialisable `input`")
soft = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")
CASES = [
    ("no content-type, 2 MB body", {"content": json.dumps({"comment": "a" * (2 * 1024 * 1024 - 64)})}),
    ("no ct, 4 KB body", {"content": json.dumps({"comment": "a" * 4000})}),
    ("text/plain, 4 KB body", {"content": json.dumps({"comment": "a" * 4000}),
                               "headers": {"Content-Type": "text/plain"}}),
    ("no ct, tiny body", {"content": "{}"}),
    ("ct json, tiny body", {"content": "{}", "headers": {"Content-Type": "application/json"}}),
    ("ct json, wrong type", {"content": json.dumps({"comment": {"a": 1}}),
                             "headers": {"Content-Type": "application/json"}}),
    ("ct json, list where obj", {"content": json.dumps([1, 2, 3]),
                                 "headers": {"Content-Type": "application/json"}}),
    ("ct json, bare string", {"content": json.dumps("x" * 5000),
                              "headers": {"Content-Type": "application/json"}}),
    ("ct json, bare 2MB string", {"content": json.dumps("x" * (2 * 1024 * 1024 - 64)),
                                  "headers": {"Content-Type": "application/json"}}),
    ("ct json, invalid utf8", {"content": b'{"comment": "\xff\xfe"}',
                               "headers": {"Content-Type": "application/json"}}),
    ("ct json, latin1 bytes", {"content": b'{"comment": "caf\xe9"}',
                               "headers": {"Content-Type": "application/json"}}),
    ("ct json, bare int", {"content": "12345", "headers": {"Content-Type": "application/json"}}),
]
for label, kw in CASES:
    r = soft.post("/api/repertoire/move_meta", **kw)
    flag = "   <<<< 500" if r.status_code >= 500 else ""
    print(f"    {label:28} -> {r.status_code}{flag}   {r.text[:120]}")

print("\n(2b) Which exception escapes? (raise_server_exceptions=True)")
for label, kw in CASES:
    if r_is_5xx := (soft.post("/api/repertoire/move_meta", **kw).status_code >= 500):
        try:
            raw.post("/api/repertoire/move_meta", **kw)
            print(f"    {label:28} -> no exception raised?!")
        except Exception as e:
            print(f"    {label:28} -> {type(e).__name__}: {str(e)[:100]}")

# ------------------------------------------------- (3) which routes are hit
print("\n(3) How many POST-JSON routes are reachable with the bypass?")
POSTS = [
    "/api/settings", "/api/sync", "/api/games/sync_recent",
    "/api/repertoire/import_game_opening",
    "/api/repertoire/move", "/api/repertoire/studies", "/api/repertoire/move_meta",
    "/api/repertoire/seed", "/api/repertoire/clear",
    "/api/repertoire/train/attempt", "/api/repertoire/train/recent_opening",
    "/api/repertoire/import_line", "/api/repertoire/check_line",
    "/api/repertoire/check_tree",
    "/api/scout/analyze", "/api/scout/import_repertoire", "/api/scout/check_line",
    "/api/scout/check_tree", "/api/scout/favorite",
    "/api/train/evaluate_move",
]
n500 = 0
for p in POSTS:
    r = soft.post(p, content=DEEP, headers={"Content-Type": "text/plain"})
    if r.status_code >= 500:
        n500 += 1
    print(f"    {r.status_code}  {p}")
print(f"    -> {n500}/{len(POSTS)} routes return 5xx")
