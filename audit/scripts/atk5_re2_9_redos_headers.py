#!/usr/bin/env python3
"""ATK-5 re2-9: remaining angles -- ReDoS in NEW regexes, header injection,
log injection, error-message path leaks.

Everything hostile here is measured for *time*, not just status, because a regex
that takes 40 s is a DoS even when it returns 200.

Isolated temp DB, idempotent.
"""
import os
import sys
import re
import time
import json
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST                    # noqa: E402
from backend.db import get_db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")
print("=" * 78)
print("ATK-5 re2-9  ReDoS / header injection / log injection")
print("=" * 78)

# ------------------------------------------------------ 1. catalogue regexes
print("\n[1] every regex literal in backend/, with a ReDoS smell test")
PATTERNS = []
for mod in ("api", "repertoire", "scout", "openings", "evaluator", "lichess_client",
            "analyzer", "db"):
    path = f"/home/robin/Projects/ChessImprovement/backend/{mod}.py"
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        continue
    for m in re.finditer(r"re\.(compile|search|match|sub|findall)\(\s*(r?[\"']((?:[^\"'\\]|\\.)*)[\"'])",
                         src):
        PATTERNS.append((mod, src[:m.start()].count("\n") + 1, m.group(2)))
print(f"    found {len(PATTERNS)} regex literals")

NASTY = [
    ("nested quantifier (a+)+", r"(a+)+$", "a" * 40 + "!"),
    ("alternation overlap", r"^(a|aa)+$", "a" * 40 + "!"),
    ("polynomial star-star", r"^(.*)*x$", "a" * 40),
    ("lazy inside greedy", r"^(a*)*b$", "a" * 40),
    ("optional repeat", r"^a?a?a?a?a?a?a?a?a?a?a?a?a?a?a?a?a?a?a?a?a?a?a?$", "a" * 30 + "b"),
]
print(f"\n    {'module':16} {'line':>5}  {'pattern':52} {'time':>9}")
SLOW = []
for mod, line, pat in PATTERNS:
    if len(pat) > 60:
        shown = pat[:57] + "..."
    else:
        shown = pat
    t0 = time.perf_counter()
    try:
        rx = re.compile(pat)
        for _n, _p, hostile in NASTY:
            try:
                rx.search(hostile)
            except re.error:
                pass
    except re.error:
        shown += "  [not a valid literal form]"
    dt = (time.perf_counter() - t0) * 1000
    if dt > 50:
        SLOW.append((mod, line, shown, dt))
    print(f"    {mod:16} {line:>5}  {shown:52} {dt:8.1f}ms")
print(f"\n    slow ones (>50ms on the 6 smell tests): {len(SLOW)}")
for s in SLOW:
    print(f"      {s[0]}:{s[1]}  {s[2]}  {s[3]:.1f}ms")

# --------------------------------------- 2. the REAL redos test: endpoint-level
print("\n" + "=" * 78)
print("[2] endpoint-level ReDoS: hostile strings against the routes that regex")
print("=" * 78)
HOSTILE = {
    "nested quantifier": "a" * 60 + "!",
    "many alternations": "a" * 60 + "!",
    "open paren storm": "(" * 200,
    "close paren storm": ")" * 200,
    "bracket storm": "[" * 200,
    "quote storm": '"' * 200,
    "newline storm": "\n" * 500,
    "nul storm": "\x00" * 200,
    "long digits": "9" * 5000,
    "long uci-ish": "e2e4" * 2000,
    "long san-ish": "Nf3 " * 2000,
    "pgn-ish": "[Event \"x\"]\n\n1. e4 " * 300,
    "combining marks": "é" * 3000,
    "rtl override": "‮" * 200 + "evil",
    "zero width": "" * 500,
}
ROUTES = [
    ("GET", "/api/scout/search", {"q": "q"}),
    ("GET", "/api/repertoire/explore", {"fen": "x"}),
    ("GET", "/api/repertoire/train/next", {"opening": "o"}),
    ("GET", "/api/repertoire/tree", {}),
    ("GET", "/api/games/recent", {"search": "s"}),
    ("GET", "/api/blunders", {"category": "c"}),
    ("GET", "/api/scout/recent", {}),
    ("GET", "/api/repertoire/studies", {}),
    ("POST", "/api/scout/check_line", {}),
    ("POST", "/api/scout/check_tree", {}),
    ("POST", "/api/repertoire/import_line", {}),
    ("POST", "/api/train/evaluate_move", {}),
    ("POST", "/api/repertoire/studies", {}),
    ("POST", "/api/repertoire/move", {}),
    ("POST", "/api/scout/import_repertoire", {}),
    ("POST", "/api/settings", {}),
]
SLOWEST = []
for method, path, tmpl in ROUTES:
    for hname, hval in HOSTILE.items():
        params = {k: hval for k in tmpl} or None
        t0 = time.time()
        try:
            if method == "GET":
                r = client.get(path, params=params)
            else:
                r = client.post(path, json={"name": hval, "repertoire": "white",
                                            "pgn": hval, "lines": hval, "moves": hval,
                                            "fen": hval, "move_uci": "e2e4",
                                            "source": hval, "color": "white",
                                            "opening_name": hval, "parent_fen": hval,
                                            "comment": hval, "description": hval})
        except Exception as e:
            SLOWEST.append((time.time() - t0, method, path, hname, f"EXC {type(e).__name__}"))
            continue
        dt = time.time() - t0
        if dt > 0.5 or r.status_code >= 500:
            SLOWEST.append((dt, method, path, hname, r.status_code))
SLOWEST.sort(reverse=True)
print(f"    {'secs':>7}  {method:5} {path:34} {'hostile':18} status")
for dt, m, p, h, st in SLOWEST[:20]:
    print(f"    {dt:7.3f}  {m:5} {p:34} {h:18} {st}")
print(f"    total slow/5xx: {len(SLOWEST)}")

# ------------------------------------------- 3. header injection / open redirect
print("\n" + "=" * 78)
print("[3] header injection: does any hostile value reach a response header?")
print("=" * 78)
CRLF = "x\r\nX-Injected: yes\r\n\r\n<script>alert(1)</script>"
CASES = [
    ("study name", "/api/repertoire/studies", {"name": CRLF, "repertoire": "white"}),
    ("settings username", "/api/settings", {"lichess_username": CRLF}),
    ("move comment", "/api/repertoire/move_meta",
     {"repertoire": "white", "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
      "move_uci": "e2e4", "comment": CRLF}),
    ("train recent_opening", "/api/repertoire/train/recent_opening", {"opening_name": CRLF}),
]
for label, path, payload in CASES:
    r = client.post(path, json=payload)
    inj = [k for k in r.headers if k.lower() in ("x-injected", "location", "set-cookie")]
    bad = any("\r" in str(v) or "\n" in str(v) for k, v in r.headers.items())
    print(f"    {label:22} {r.status_code}  injected={inj or 'no'}  "
          f"crlf_in_headers={bad}")

print("\n    Location header / open redirect:")
for path, params in (("/api/blunders/1", {}), ("/", {}), ("/index.html", {})):
    r = client.get(path, params=params, follow_redirects=False)
    loc = r.headers.get("location")
    print(f"    GET {path:20} -> {r.status_code}  location={loc!r}")

# ------------------------------------------------- 4. log injection via names
print("\n" + "=" * 78)
print("[4] log injection: can a hostile name forge a log line?")
print("=" * 78)
LOGLINE = "\nINFO audit: forged log line -- pretending to be a success record"
r = client.post("/api/repertoire/studies", json={"name": LOGLINE, "repertoire": "white"})
print(f"    POST study name -> {r.status_code}  {r.text[:120]}")
c = get_db()
row = c.execute("SELECT name FROM repertoire_studies ORDER BY id DESC LIMIT 1").fetchone()
print(f"    stored name repr: {row['name'][:70]!r}" if row else "    (no row)")
c.execute("DELETE FROM repertoire_studies WHERE name LIKE ?", ("\nINFO audit%",))
c.commit()
c.close()
print("    -> the newline IS stored; anything that logs the name unescaped will")
print("       forge a log line. (Whether it does is a frontend/ops concern; the")
print("       storage layer accepts it.)")

# ------------------------------------------- 5. path / internals in error text
print("\n" + "=" * 78)
print("[5] do error messages leak absolute paths or internals?")
print("=" * 78)
LEAKS = ["/home/robin/Projects", "chess_improvement.db", "Traceback",
         "sqlite3", "OperationalError", "no such column", "stockfish", ".py\""]
CASES5 = [
    ("POST /api/settings huge token", lambda: client.post("/api/settings", json={"lichess_token": "A" * 5000})),
    ("POST /api/repertoire/move bad fen", lambda: client.post("/api/repertoire/move", json={"repertoire": "white", "fen": "nope", "move_uci": "e2e4"})),
    ("GET /api/blunders/999999", lambda: client.get("/api/blunders/999999")),
    ("POST /api/scout/analyze junk", lambda: client.post("/api/scout/analyze", json={"source": "http://evil"})),
    ("GET /api/repertoire/explore bad", lambda: client.get("/api/repertoire/explore", params={"fen": "zzz"})),
    ("POST /api/repertoire/studies empty", lambda: client.post("/api/repertoire/studies", json={"name": "", "repertoire": "white"})),
    ("GET /api/nonexistent", lambda: client.get("/api/nonexistent")),
    ("DELETE /api/repertoire/studies/abc", lambda: client.delete("/api/repertoire/studies/abc")),
]
for label, fn in CASES5:
    r = fn()
    body = r.text[:300]
    found = [k for k in LEAKS if k.lower() in body.lower()]
    print(f"    {label:36} {r.status_code}  leaks={found or 'none'}")
    if found:
        print(f"        body: {body[:200]}")

print("\ndone.")
