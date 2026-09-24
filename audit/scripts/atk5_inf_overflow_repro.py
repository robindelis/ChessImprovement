#!/usr/bin/env python3
"""
ATK5-04 repro (corrected): raw-JSON non-finite floats + unbounded /api/blunders limit.

NOTE: the earlier "Out of range float values" EXC burst was a *test-client*
artifact (httpx refuses to ENCODE inf/NaN in the request). This repro sends RAW
bytes so python-chess/json actually parses them, i.e. what a real attacker can do.

Idempotent, isolated temp DB, network stubbed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import

import requests


class _R:
    status_code = 503
    text = "stub"

    def json(self):
        return {}

    def iter_lines(self, decode_unicode=False):
        return iter(())


requests.get = lambda *a, **k: _R()
requests.post = lambda *a, **k: _R()
import yt_dlp


class _Y:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, *a, **k):
        return None


yt_dlp.YoutubeDL = _Y

from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
client = TestClient(app, raise_server_exceptions=False)
J = {"Content-Type": "application/json"}


def post_raw(url, raw):
    r = client.post(url, content=raw, headers=J)
    return r


print("=" * 78)
print("A) raw JSON non-finite floats into pydantic models with allow_inf_nan=False")
print("=" * 78)
RAW = [
    ('Infinity in popularity_pct', f'{{"repertoire":"white","parent_fen":"{FEN}","move_uci":"e2e4","popularity_pct":Infinity}}'),
    ('NaN in popularity_pct', f'{{"repertoire":"white","parent_fen":"{FEN}","move_uci":"e2e4","popularity_pct":NaN}}'),
    ('-Infinity win_rate', f'{{"repertoire":"white","parent_fen":"{FEN}","move_uci":"e2e4","win_rate":-Infinity}}'),
    ('Infinity gap_threshold', '{"gap_threshold": Infinity}'),
    ('NaN gap_threshold', '{"gap_threshold": NaN}'),
    ('Infinity max_opening_ply', '{"max_opening_ply": Infinity}'),
    ('Infinity blunder_threshold_cp', '{"blunder_threshold_cp": Infinity}'),
    ('huge 1e999 literal', '{"max_opening_ply": 1e999}'),
]
for name, raw in RAW:
    url = "/api/repertoire/move" if "repertoire" in raw else "/api/settings"
    try:
        r = post_raw(url, raw)
        print(f"  {r.status_code}  {name:<32} -> {r.text[:220]!r}")
    except Exception as e:
        print(f"  EXC  {name:<32} -> {type(e).__name__}: {str(e)[:150]}")

print()
print("  Does any NaN/inf value PERSIST into the DB (poisoning later GETs)?")
from backend.db import get_db  # noqa: E402
conn = get_db()
cur = conn.cursor()
cur.execute("SELECT key, value FROM settings WHERE key IN ('max_opening_ply','gap_threshold','blunder_threshold_cp')")
for k, v in cur.fetchall():
    print(f"    settings[{k}] = {v!r}")
cur.execute("SELECT move_uci, popularity_pct, win_rate FROM repertoire_moves")
for row in cur.fetchall():
    print("    repertoire_moves row:", tuple(row))
    break
conn.close()
r = client.get("/api/settings")
print("  GET /api/settings after NaN writes ->", r.status_code, r.text[:300])

print()
print("=" * 78)
print("B) /api/blunders  `limit` has NO upper bound (contrast: /api/games/recent le=100)")
print("=" * 78)
for q in ["", "?limit=50", "?limit=0", "?limit=-1", "?limit=1000000",
          "?limit=9223372036854775807", "?limit=999999999999999999999",
          "?limit=1e309", "?limit=abc", "?limit=null", "?min_loss=-99999999999"]:
    try:
        r = client.get("/api/blunders" + q)
        n = len(r.json()) if r.status_code == 200 and r.text.startswith("[") else "-"
        print(f"  {r.status_code}  /api/blunders{q:<32} rows={n}  {r.text[:70]!r}")
    except Exception as e:
        print(f"  EXC  /api/blunders{q:<32} {type(e).__name__}: {str(e)[:120]}")
print()
print("  contrast /api/games/recent?limit=100000 ->",
      client.get("/api/games/recent?limit=100000").status_code,
      client.get("/api/games/recent?limit=100000").text[:90])

print()
print("=" * 78)
print("C) 422 handler: does it echo raw request content / internals?")
print("=" * 78)
r = post_raw("/api/repertoire/move",
             f'{{"repertoire":"white","parent_fen":"{FEN}","move_uci":"e2e4","popularity_pct":Infinity}}')
print("  ", r.status_code, r.text[:600])
print()
r = post_raw("/api/repertoire/move", "{ not json ")
print("  malformed json ->", r.status_code, r.text[:300])
print()
r = client.post("/api/settings", content=b"\xff\xfe\x00bad", headers=J)
print("  binary garbage ->", r.status_code, r.text[:200])

print()
print("=" * 78)
print("D) 405 (wrong method) body")
print("=" * 78)
r = client.put("/api/repertoire/clear")
print("  ", r.status_code, r.text[:200])

print()
print("DONE")
