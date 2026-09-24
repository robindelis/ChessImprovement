"""FIXER-U: pin down the EXACT exception each hostile body raises, in-process."""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import tests.test_setup  # MUST be first

import backend.api as module
from fastapi.testclient import TestClient
from backend.api import app

# Never touch the network from a probe.
class _Boom:
    def __init__(self, *a, **k): pass
    def __getattr__(self, name):
        def _f(*a, **k):
            raise AssertionError("probe must not reach the network")
        return _f
module.LichessClient = _Boom
module.ContentScout = _Boom

client = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")

BODIES = {
    "lone-surrogate": b'{"name":"\xed\xa0\x80"}',
    "5-byte text": b"hello",
    "json-looking": b"{}}}}",
}
CTS = ["application/json", "text/plain", None, "application/octet-stream"]
ROUTES = ["/api/settings", "/api/repertoire/move", "/api/repertoire/check_line",
          "/api/scout/check_line", "/api/repertoire/train/recent_opening"]

print("=== 1. what does sanitize()/json.dumps() do with these inputs? ===")
import json
for label, body in BODIES.items():
    exc = {"type": "json_invalid", "loc": ("body",), "msg": "boom", "input": body}
    # raw handler behaviour: reproduce sanitize() exactly
    def sanitize(val):
        if isinstance(val, float) and (val != val or val in (float("inf"), float("-inf"))):
            return None
        if isinstance(val, dict):
            return {str(k): sanitize(v) for k, v in val.items()}
        if isinstance(val, (list, tuple)):
            return [sanitize(x) for x in val]
        return val
    try:
        json.dumps(sanitize([exc])).encode("utf-8")
        print(f"  {label:16s} -> serialises fine")
    except Exception as e:
        print(f"  {label:16s} -> {type(e).__name__}: {e}")

# a lone surrogate in a *string value*
sur = '{"name":"\ud800"}'
print(f"  lone-surrogate str  -> {sur!r}")
try:
    json.dumps({"detail": [{"input": sur}]}).encode("utf-8")
    print("     serialises fine")
except Exception as e:
    print(f"     {type(e).__name__}: {e}")

print()
print("=== 2. status per (route, content-type, body) ===")
for path in ROUTES:
    for ct in CTS:
        line = f"  {path:38s} {str(ct):26s}"
        for label, body in BODIES.items():
            headers = {"Content-Length": str(len(body))}
            if ct:
                headers["Content-Type"] = ct
            r = client.post(path, content=body, headers=headers)
            line += f" {label}={r.status_code}"
        print(line)
