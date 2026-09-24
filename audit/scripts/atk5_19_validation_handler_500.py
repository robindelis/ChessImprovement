#!/usr/bin/env python3
"""ATK-5-19: minimise the 500 in FIXER-L's new RequestValidationError handler.

Hypothesis: `validation_exception_handler` puts `exc.errors()` straight into a
JSONResponse. When pydantic cannot parse the body at all it reports
`'input': b'<raw body>'` (bytes), `sanitize()` passes bytes through untouched,
and `json.dumps` raises TypeError -> the 422 handler itself 500s.

If true this needs only a handful of bytes, not a 2 KB nesting bomb.

Isolated temp DB, idempotent.
"""
import os
import sys
import json
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

soft = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")
raw = TestClient(app, raise_server_exceptions=True, base_url="http://testserver")

POSTS = [
    "/api/settings", "/api/sync", "/api/games/sync_recent",
    "/api/repertoire/import_game_opening", "/api/repertoire/move",
    "/api/repertoire/studies", "/api/repertoire/move_meta",
    "/api/repertoire/train/attempt", "/api/repertoire/train/recent_opening",
    "/api/repertoire/import_line", "/api/repertoire/check_line",
    "/api/repertoire/check_tree", "/api/scout/analyze",
    "/api/scout/import_repertoire", "/api/scout/check_line",
    "/api/scout/check_tree", "/api/scout/favorite",
    "/api/train/evaluate_move",
]

print("=" * 78)
print("ATK-5-19  minimal 500 in the 422 handler")
print("=" * 78)

# The absolute smallest bodies that make pydantic bail before it has a model.
TINY = [
    ("invalid utf-8 (2 bytes)", b'\xff', "application/json"),
    ("lone surrogate escape", b'"\xed\xa0\x80"', "application/json"),
    ("truncated json", b'{"a":', "application/json"),
    ("empty body", b'', "application/json"),
    ("just a comma", b',', "application/json"),
    ("utf16 BOM", b'\xff\xfe{\x00', "application/json"),
    ("invalid utf-8, ct json+ld", b'\xff', "application/vnd.api+json"),
    ("invalid utf-8, no ct", b'\xff', None),
    ("invalid utf-8, text/plain", b'\xff', "text/plain"),
    ("invalid utf-8, +octet", b'\xff', "application/octet-stream"),
    ("bare '\\xff' no quotes", b'{"a":\xff}', "application/json"),
    ("nul byte in string", b'{"a":"\x00"}', "application/json"),
]

print("\n[1] single route /api/scout/check_tree, tiny hostile bodies")
for label, body, ct in TINY:
    kw = {"content": body}
    if ct:
        kw["headers"] = {"Content-Type": ct}
    r = soft.post("/api/scout/check_tree", **kw)
    flag = "  <<<< 500" if r.status_code >= 500 else ""
    print(f"    {label:26} {str(body)[:20]:22} ct={str(ct):24} -> {r.status_code}{flag}")

print("\n[2] sweep every POST route with a 1-byte invalid-UTF-8 body")
n500 = 0
for p in POSTS:
    r = soft.post(p, content=b'\xff', headers={"Content-Type": "application/json"})
    mark = "  <<<< 500" if r.status_code >= 500 else ""
    if r.status_code >= 500:
        n500 += 1
    print(f"    {r.status_code}  {p}{mark}")
print(f"    -> {n500}/{len(POSTS)} return 5xx from a ONE-BYTE request")

print("\n[3] the escaping exception")
try:
    raw.post("/api/scout/check_tree", content=b'\xff',
             headers={"Content-Type": "application/json"})
    print("    no exception (unexpected)")
except Exception as e:
    print(f"    {type(e).__name__}: {e}")

print("\n[4] is it really `bytes` in exc.errors()['input']?")
from fastapi.routing import APIRoute  # noqa: E402
# Reproduce pydantic's error shape directly.
try:
    json.loads(b'\xff')
except Exception as e:
    print(f"    json.loads(b'\\xff') -> {type(e).__name__}: {e}")
try:
    json.loads("[".replace("[", "\x00" * 1))
except Exception as e:
    print(f"    (control) {type(e).__name__}")

print("\n[5] does the same 500 also hit a *valid* JSON body that is the wrong shape?")
for label, body in (
    ("bare list", b'[1,2,3]'),
    ("bare string", b'"hello"'),
    ("bare int", b'5'),
    ("bare true", b'true'),
    ("json null", b'null'),
):
    r = soft.post("/api/scout/check_tree", content=body,
                  headers={"Content-Type": "application/json"})
    print(f"    {label:12} -> {r.status_code}  {r.text[:80]}")

print("\n[6] server-side log volume: does each of these log a full traceback?")
print("    (see the WARNING/ERROR stream above; every 500 logs a stack trace)")
