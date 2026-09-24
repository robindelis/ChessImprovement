#!/usr/bin/env python3
"""
ATK5-03 / ABUSE-03+ repro: structured route fuzzer.

For every route in backend/api.py, fire a matrix of hostile inputs:
wrong types, missing fields, extra fields, wrong HTTP methods, absurd values,
unicode fuzz strings, oversized values, deep nesting.

Reports EVERY distinct 500 / traceback. Also compares
raise_server_exceptions=False (real 500) vs True (route "works" in tests
only because TestClient re-raises -> latent crash under uvicorn).

NETWORK IS STUBBED: requests.get/post and yt_dlp are monkeypatched so no real
external call happens.
Idempotent. Isolated temp DB via tests.test_setup.
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import

# ---- stub the network BEFORE importing the app modules that use it -------
import requests


class _FakeResp:
    status_code = 503
    text = "stubbed"

    def json(self):
        return {}

    def iter_lines(self, decode_unicode=False):
        return iter(())


def _fake_get(*a, **k):
    return _FakeResp()


def _fake_post(*a, **k):
    return _FakeResp()


requests.get = _fake_get
requests.post = _fake_post
requests.Session.request = lambda self, *a, **k: _FakeResp()

import yt_dlp  # noqa: E402


class _FakeYDL:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, *a, **k):
        return None


yt_dlp.YoutubeDL = _FakeYDL

from backend.api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
client_raise = TestClient(app, raise_server_exceptions=True)

START = __import__("chess").STARTING_FEN
FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

UNICODE_FUZZ = [
    "𝕬𝕭𝕮",         # math bold
    "‮evil‬",              # bidi override
    "💥' OR 1=1 --",                 # emoji + classic sqli
    "../../../etc/passwd",
    "'; DROP TABLE repertoire_moves; --",
    "' UNION SELECT value FROM settings --",
    "%00",
    "ＡＢＣ",              # fullwidth
    "a" * 5000,
    "é" * 500,
    "\ud800",                          # lone surrogate
]

DEEP_JSON = {"a": None}
_d = DEEP_JSON
for _i in range(400):
    _d["a"] = {"a": None}
    _d = _d["a"]


def routes():
    out = []
    for r in app.routes:
        if type(r).__name__ != "APIRoute":
            continue
        methods = sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))
        out.append((sorted(methods), r.path, r))
    return out


def path_params(path):
    import re
    return re.findall(r"\{([^}:]+)", path)


CASES = []


def add(method, path, **kw):
    CASES.append((method, path, kw))


# ---------------------------------------------------------------- GET routes
for methods, path, _r in routes():
    pp = path_params(path)
    concrete = path
    for p in pp:
        concrete = concrete.replace("{" + p + "}", "1")

    if "GET" in methods:
        # absurd / hostile query values
        add("GET", path + "?limit=999999999999", _=None, note="huge limit (no upper bound?)") if False else None
        for q in [
            "limit=-1",
            "limit=0",
            "limit=999999999999999999999",
            "offset=-1",
            "color=../../etc",
            "repertoire=%27%20OR%201%3D1%20--",
            "study=%27%3B%20DROP%20TABLE%20repertoire_moves%3B--",
            "name=" + UNICODE_FUZZ[7],
            "search=" + UNICODE_FUZZ[8],
            "fen=" + FEN,
            "fen=" + "8" * 4000 + "/" * 7 + " w - - 0 1",
            "fen=" + "P" * 20000 + "/" * 7 + " w - - 0 1",
            "fen=" + FEN.replace(" ", "%20"),
            "depth=30",
            "depth=1000000",
            "min_games=0",
            "threshold=1e308",
        ]:
            sep = "&" if "?" in concrete else "?"
            add("GET", concrete + sep + q)
        add("GET", concrete)
    # wrong HTTP method
    for bad in ("PUT", "PATCH", "DELETE", "POST"):
        if bad not in methods:
            add(bad, concrete)

# ------------------------------------------------------------- POST routes
JSON_CASES = [
    {},
    {"repertoire": "white"},
    {"repertoire": "../../etc/passwd"},
    {"repertoire": "white", "parent_fen": FEN, "move_uci": "e2e4"},
    {"repertoire": "white", "parent_fen": "not-a-fen", "move_uci": "e2e4"},
    {"repertoire": "white", "parent_fen": FEN, "move_uci": None},
    {"repertoire": "white", "parent_fen": FEN, "move_uci": 12345},
    {"repertoire": "white", "parent_fen": FEN, "move_uci": "e2e4", "extra_unknown": "x"},
    {"repertoire": "white", "parent_fen": FEN, "move_uci": "e2e4", "popularity_pct": "NaN"},
    {"repertoire": "white", "parent_fen": FEN, "move_uci": "e2e4", "popularity_pct": 1e400},
    {"repertoire": "white", "parent_fen": FEN, "move_uci": "e2e4", "win_rate": float("nan")},
    {"blunder_id": "abc", "attempted_uci": "e2e4"},
    {"blunder_id": 999999999, "attempted_uci": "e2e4"},
    {"repertoire": "white", "current_fen": FEN, "attempted_uci": "zzzz"},
    {"repertoire": "white", "current_fen": FEN, "attempted_uci": "e2e4", "study_name": UNICODE_FUZZ[0]},
    {"repertoire": "white", "current_fen": FEN, "attempted_uci": "e2e4", "study_name": "'; DROP TABLE repertoire_moves; --"},
    {"source": ""},
    {"source": UNICODE_FUZZ[1]},
    {"source": "1. e4 e5 2. Nf3"},
    {"repertoire": "white", "moves": "notalist"},
    {"repertoire": "white", "moves": [1, 2, 3]},
    {"repertoire": "white", "moves": DEEP_JSON},
    {"repertoire": "white", "lines": [{"moves": [{"uci": "e2e4", "fen_before": FEN}]}] * 3},
    {"repertoire": "white", "moves": [{"fen_before": FEN, "uci": "e2e4"}]},
    {"repertoire": "white", "moves": [{"fen_before": "bad", "uci": "zz"}]},
    {"source_id": UNICODE_FUZZ[4]},
    {"name": UNICODE_FUZZ[7], "repertoire": "white"},
    {"name": None, "repertoire": "white"},
    {"game_id": "../../../etc/passwd", "repertoire": "white"},
    {"game_id": None, "repertoire": "white"},
    {"lichess_token": UNICODE_FUZZ[7]},
    {"max_opening_ply": 0},
    {"max_opening_ply": 10 ** 12},
    {"gap_threshold": "abc"},
    {"name": "study", "opening_name": "x"},
    None,                      # literally null body
    [1, 2, 3],                # array body
    "a string body",
    12345,
]

for methods, path, _r in routes():
    if "POST" not in methods:
        continue
    concrete = path
    for p in path_params(path):
        concrete = concrete.replace("{" + p + "}", "1")
    if path in ("/api/sync", "/api/games/sync_recent", "/api/repertoire/sync_games"):
        continue  # skipped: would hit lichess (stubbed anyway) / long engine runs
    if path == "/api/repertoire/clear":
        continue  # keep for a separate controlled test
    if path == "/api/repertoire/seed":
        continue  # guarded separately
    for jc in JSON_CASES:
        add("POST", concrete, json=jc)

# extra: raw text bodies (content-type text/plain) on JSON routes
for methods, path, _r in routes():
    if "POST" not in methods or path.startswith("/api/sync"):
        continue
    concrete = path
    for p in path_params(path):
        concrete = concrete.replace("{" + p + "}", "1")
    add("POST", concrete, content=b"not json at all", headers={"Content-Type": "application/json"})
    add("POST", concrete, content=b'{"a": ' + b"[" * 500 + b"]" * 500 + b"}",
        headers={"Content-Type": "application/json"})

print("=" * 78)
print(f"FUZZ: {len(CASES)} requests against {len(routes())} routes")
print("=" * 78)

statuses = {}
internal = {}   # method+path -> (status, body[:400], traceback)
for i, (method, url, kw) in enumerate(CASES):
    try:
        r = client.request(method, url, **kw)
        statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
        if r.status_code >= 500:
            key = f"{method} {url}"
            internal.setdefault(key, (r.status_code, r.text[:500], None))
    except Exception as e:  # pragma: no cover
        statuses["EXC"] = statuses.get("EXC", 0) + 1
        internal.setdefault(f"{method} {url}", ("EXC", str(e)[:500], traceback.format_exc()))

print("status histogram:", dict(sorted(statuses.items(), key=lambda kv: str(kv[0]))))
print()
if not internal:
    print("NO 5xx / unhandled exceptions observed.")
else:
    print(f"### {len(internal)} distinct 500-class responses")
    for k, (st, body, tb) in internal.items():
        print("-" * 78)
        print(f"  {k}  -> {st}")
        print(f"  body: {body!r}")

# ---------------------------------------------------------------- 500 bodies
print()
print("=" * 78)
print("Do any 500 bodies leak internals? (traceback / file paths / SQL / secrets)")
print("=" * 78)
leak_re = __import__("re").compile(
    r"(Traceback|/home/robin|ChessImprovement/backend|sqlite3\.|OperationalError|"
    r"SELECT |INSERT |UPDATE |lip_|File \"|line \d+, in )")
found = False
for k, (st, body, tb) in internal.items():
    m = leak_re.search(body or "")
    if m:
        found = True
        print(f"  LEAK in {k}: matched {m.group(0)!r}")
        print(f"    {body[:400]!r}")
if not found:
    print("  no internals matched in 500 bodies (checked traceback/paths/SQL/token)")

# ------------------------------------------------ raise_server_exceptions delta
print()
print("=" * 78)
print("TestClient(raise_server_exceptions=True) vs False  -- routes that only")
print("'work' because TestClient re-raises the exception:")
print("=" * 78)
for k in list(internal.keys()):
    method, url = k.split(" ", 1)
    try:
        client_raise.request(method, url)
        print(f"  OK under raise=True  : {k}")
    except Exception as e:
        print(f"  RAISES under raise=True: {k}  -> {type(e).__name__}: {str(e)[:120]}")

print()
print("DONE")
