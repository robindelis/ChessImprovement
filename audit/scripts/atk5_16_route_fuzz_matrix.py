#!/usr/bin/env python3
"""ATK-5 breadth probe: hostile-input matrix against EVERY route in backend/api.py.

Goal: after the wave-1/wave-2 fixers rewrote api.py, find *new* 500s, crashes,
hangs, and resource blowups on routes I had not previously fuzzed.

Idempotent, re-runnable, isolated temp DB. Never touches production.
"""
import os
import sys
import time
import json
import signal
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST -- isolated temp DB            # noqa: E402
import backend.db  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402
import chess  # noqa: E402

print("=" * 78)
print("ATK-5-16  ROUTE FUZZ MATRIX")
print("=" * 78)
print("CHESS_DB_PATH =", os.environ.get("CHESS_DB_PATH"))

client = TestClient(app, raise_server_exceptions=False, base_url="http://testserver")

# Neutralise outbound network so nothing leaves the box.
import backend.repertoire as rep  # noqa: E402
import backend.evaluator as ev  # noqa: E402
import backend.scout as sc  # noqa: E402

_net_calls = {"n": 0}
_orig_explorer = None


def _fake_explorer(*a, **k):
    _net_calls["n"] += 1
    return {"moves": [], "total_games": 0, "white_total": 0, "draws_total": 0, "black_total": 0}


for mod, name in ((rep, "get_explorer_moves"), (ev, "get_explorer_moves")):
    if hasattr(mod, name):
        setattr(mod, name, _fake_explorer)

# Kill engine acquisition entirely (no Stockfish spawns in a fuzz run).
class _Boom:
    def analyse(self, *a, **k):
        raise RuntimeError("fuzz: engine disabled")

    def configure(self, *a, **k):
        return None

    def quit(self):
        return None


for holder in ("_engine", "engine"):
    if hasattr(rep.RepertoireManager, holder):
        try:
            setattr(rep.RepertoireManager, holder, _Boom())
        except Exception:
            pass

START = chess.Board().fen()
AFTER_E4 = chess.Board(); AFTER_E4.push_san("e4")
AFTER_E4E5 = chess.Board(); AFTER_E4E5.push_san("e4"); AFTER_E4E5.push_san("e5")

HUGE_INT = 10 ** 30
NEG = -10 ** 9
INF_JSON = '{"x": Infinity}'
NAN_JSON = '{"x": NaN}'

MATRIX = []


def M(method, path, label, **kw):
    MATRIX.append((method, path, label, kw))


# ---------------------------------------------------------------- GET routes
M("GET", "/api/account", "plain")
M("GET", "/api/settings", "plain")
M("GET", "/api/settings", "no-store?", headers={"Cache-Control": "no-cache"})
M("GET", "/api/dashboard", "plain")
M("GET", "/api/dashboard", "neg color", params={"color": "purple"})
M("GET", "/api/games/recent", "limit huge", params={"limit": HUGE_INT})
M("GET", "/api/games/recent", "limit neg", params={"limit": NEG})
M("GET", "/api/games/recent", "limit inf", params={"limit": "Infinity"})
M("GET", "/api/games/recent", "limit nan", params={"limit": "NaN"})
M("GET", "/api/games/recent", "limit 1e400", params={"limit": "1e400"})
M("GET", "/api/games/recent", "limit hex", params={"limit": "0x10"})
M("GET", "/api/games/recent", "limit list", params={"limit": ["1", "2"]})
M("GET", "/api/blunders", "limit huge", params={"limit": HUGE_INT})
M("GET", "/api/blunders", "limit neg", params={"limit": NEG})
M("GET", "/api/blunders", "category sql", params={"category": "' OR 1=1--"})
M("GET", "/api/blunders", "category wild", params={"category": "%"})
M("GET", "/api/blunders", "id 1e400", params={"blunder_id": "1e400"})
M("GET", "/api/blunders/1", "absent")
M("GET", "/api/blunders/99999999999999999999", "huge id")
M("GET", "/api/blunders/-1", "neg id")
M("GET", "/api/blunders/abc", "str id")
M("GET", "/api/train/paths", "plain")
M("GET", "/api/train/paths", "color evil", params={"color": "w\x00hite"})
M("GET", "/api/repertoire/explore", "no fen")
M("GET", "/api/repertoire/explore", "fen start", params={"fen": START})
M("GET", "/api/repertoire/explore", "fen empty", params={"fen": ""})
M("GET", "/api/repertoire/explore", "fen junk", params={"fen": "not a fen"})
M("GET", "/api/repertoire/explore", "fen 1MB", params={"fen": "x" * 1000000})
M("GET", "/api/repertoire/explore", "fen newline", params={"fen": START + "\nposition startpos moves e2e4"})
M("GET", "/api/repertoire/explore", "fen NUL", params={"fen": START + "\x00"})
M("GET", "/api/repertoire/explore", "ratings", params={"fen": START, "ratings": "1" * 5000})
M("GET", "/api/repertoire/tree", "white")
M("GET", "/api/repertoire/tree", "evil color", params={"color": "w'hite"})
M("GET", "/api/repertoire/tree", "study 1e400", params={"study_id": "1e400"})
M("GET", "/api/repertoire/studies", "plain")
M("GET", "/api/repertoire/train/next", "white")
M("GET", "/api/repertoire/train/next", "evil color", params={"color": "white'; DROP TABLE repertoire_moves;--"})
M("GET", "/api/repertoire/train/next", "opening wild", params={"opening": "%"})
M("GET", "/api/repertoire/train/next", "opening huge", params={"opening": "A" * 100000})
M("GET", "/api/repertoire/train/openings", "plain")
M("GET", "/api/repertoire/train/progress", "evil color", params={"color": "x" * 5000})
M("GET", "/api/repertoire/eval_stream", "bad fen", params={"fen": "junk"})
M("GET", "/api/repertoire/eval_stream", "empty", params={"fen": ""})
M("GET", "/api/repertoire/eval_stream", "1MB fen", params={"fen": "x" * 1000000})
M("GET", "/api/repertoire/game_gaps", "plain")
M("GET", "/api/repertoire/game_gaps", "huge min", params={"min_games": HUGE_INT})
M("GET", "/api/repertoire/game_gaps", "neg min", params={"min_games": NEG})
M("GET", "/api/scout/search", "empty q", params={"q": ""})
M("GET", "/api/scout/search", "no q")
M("GET", "/api/scout/search", "q 1MB", params={"q": "a" * 1000000})
M("GET", "/api/scout/search", "q inj", params={"q": "' OR ''='"})
M("GET", "/api/scout/search", "q url", params={"q": "http://169.254.169.254/latest/meta-data/"})
M("GET", "/api/scout/search", "q file", params={"q": "file:///etc/passwd"})
M("GET", "/api/scout/search", "q 5k", params={"q": "a" * 5000})
M("GET", "/api/scout/recent", "plain")
M("GET", "/api/scout/recent", "limit huge", params={"limit": HUGE_INT})
M("GET", "/api/scout/recent", "source_type", params={"source_type": "'; DROP TABLE scouted_content;--"})
M("GET", "/api/games/sync_recent_stream", "plain")
M("GET", "/api/repertoire/sync_games_stream", "plain")

# --------------------------------------------------------------- POST routes
M("POST", "/api/settings", "no body")
M("POST", "/api/settings", "null fields", json={})
M("POST", "/api/settings", "1MB token", json={"lichess_token": "lip_" + "A" * 1000000})
M("POST", "/api/settings", "token 4MB", json={"lichess_token": "A" * 4000000})
M("POST", "/api/settings", "username 1MB", json={"lichess_username": "u" * 1000000})
M("POST", "/api/settings", "neg max_ply", json={"max_opening_ply": -999999})
M("POST", "/api/settings", "huge max_ply", json={"max_opening_ply": HUGE_INT})
M("POST", "/api/settings", "str max_ply", json={"max_opening_ply": "abc"})
M("POST", "/api/settings", "float max_ply", json={"max_opening_ply": 1.5})
M("POST", "/api/settings", "null max_ply", json={"max_opening_ply": None})
M("POST", "/api/settings", "list token", json={"lichess_token": ["a", "b"]})
M("POST", "/api/settings", "dict token", json={"lichess_token": {"a": 1}})
M("POST", "/api/settings", "bool token", json={"lichess_token": True})
M("POST", "/api/settings", "nan ply", content=json.dumps({"max_opening_ply": float("nan")}),
  headers={"Content-Type": "application/json"})
M("POST", "/api/settings", "inf ply", content='{"max_opening_ply": Infinity}',
  headers={"Content-Type": "application/json"})
M("POST", "/api/settings", "deep json", content="[" * 3000 + "]" * 3000,
  headers={"Content-Type": "application/json"})
M("POST", "/api/sync", "max_games 1e6", json={"max_games": 1000000})
M("POST", "/api/sync", "max_games neg", json={"max_games": -1})
M("POST", "/api/sync", "username hostile", json={"username": "'; DROP TABLE games;--"})
M("POST", "/api/games/sync_recent", "huge", json={"max_games": HUGE_INT})
M("POST", "/api/repertoire/move", "no body")
M("POST", "/api/repertoire/move", "empty", json={})
M("POST", "/api/repertoire/move", "repertoire evil", json={"repertoire": "white'; DROP TABLE repertoire_moves;--"})
M("POST", "/api/repertoire/move", "uci huge", json={"repertoire": "white", "fen": START, "move_uci": "a" * 100000})
M("POST", "/api/repertoire/move", "fen 1MB", json={"repertoire": "white", "fen": "x" * 1000000, "move_uci": "e2e4"})
M("POST", "/api/repertoire/move", "comment 4MB", json={"repertoire": "white", "fen": START, "move_uci": "e2e4", "comment": "c" * 4000000})
M("POST", "/api/repertoire/move", "arrows junk", json={"repertoire": "white", "fen": START, "move_uci": "e2e4", "arrows": "notjson"})
M("POST", "/api/repertoire/move", "arrows obj", json={"repertoire": "white", "fen": START, "move_uci": "e2e4", "arrows": {"a": 1}})
M("POST", "/api/repertoire/move_meta", "no body")
M("POST", "/api/repertoire/move_meta", "arrows 1MB", json={"repertoire": "white", "fen": START, "move_uci": "e2e4", "arrows": "[" * 500000})
M("POST", "/api/repertoire/move_meta", "comment 1MB", json={"repertoire": "white", "fen": START, "move_uci": "e2e4", "comment": "c" * 1000000})
M("POST", "/api/repertoire/studies", "no body")
M("POST", "/api/repertoire/studies", "empty name", json={"name": "", "repertoire": "white"})
M("POST", "/api/repertoire/studies", "1MB name", json={"name": "n" * 1000000, "repertoire": "white"})
M("POST", "/api/repertoire/studies", "name 4MB", json={"name": "N" * 4000000, "repertoire": "white"})
M("POST", "/api/repertoire/studies", "evil rep", json={"name": "x", "repertoire": "w'hite"})
M("POST", "/api/repertoire/studies", "desc 1MB", json={"name": "x", "repertoire": "white", "description": "d" * 1000000})
M("POST", "/api/repertoire/seed", "force=1")
M("POST", "/api/repertoire/seed", "force=true")
M("POST", "/api/repertoire/seed", "force=maybe")
M("POST", "/api/repertoire/clear", "plain")
M("POST", "/api/repertoire/train/attempt", "no body")
M("POST", "/api/repertoire/train/attempt", "empty", json={})
M("POST", "/api/repertoire/train/attempt", "huge id", json={"move_id": HUGE_INT})
M("POST", "/api/repertoire/train/attempt", "neg id", json={"move_id": -1})
M("POST", "/api/repertoire/train/attempt", "str id", json={"move_id": "abc"})
M("POST", "/api/repertoire/train/attempt", "unicode", json={"move_id": 1, "user_move": "éè"})
M("POST", "/api/repertoire/train/recent_opening", "no body")
M("POST", "/api/repertoire/train/recent_opening", "1MB name", json={"opening_name": "x" * 1000000})
M("POST", "/api/repertoire/import_line", "no body")
M("POST", "/api/repertoire/import_line", "pgn 1MB", json={"pgn": "1. e4 " * 100000, "repertoire": "white"})
M("POST", "/api/repertoire/import_game_opening", "no body")
M("POST", "/api/repertoire/import_game_opening", "game_id hostile", json={"game_id": "' OR 1=1--"})
M("POST", "/api/repertoire/check_line", "moves huge list", json={"moves": [{"a": 1}] * 50000})
M("POST", "/api/repertoire/check_tree", "lines huge", json={"lines": [{"moves": [1]}] * 20000})
M("POST", "/api/scout/check_line", "no body")
M("POST", "/api/scout/check_tree", "no body")
M("POST", "/api/scout/favorite", "no body")
M("POST", "/api/scout/favorite", "id huge", json={"id": HUGE_INT})
M("POST", "/api/scout/favorite", "id -1", json={"id": -1})
M("POST", "/api/scout/import_repertoire", "no body")
M("POST", "/api/scout/import_repertoire", "pgn 1MB", json={"pgn": "x" * 1000000})
M("POST", "/api/train/evaluate_move", "no body")
M("POST", "/api/train/evaluate_move", "empty", json={})
M("POST", "/api/train/evaluate_move", "fen junk", json={"fen": "junk", "move_uci": "e2e4"})
M("POST", "/api/train/evaluate_move", "uci 1MB", json={"fen": START, "move_uci": "a" * 1000000})

# ------------------------------------------------------------- DELETE routes
M("DELETE", "/api/repertoire/move", "no params")
M("DELETE", "/api/repertoire/move", "repertoire only", params={"repertoire": "white"})
M("DELETE", "/api/repertoire/move", "huge fen", params={"repertoire": "white", "fen": "x" * 1000000, "move_uci": "e2e4"})
M("DELETE", "/api/repertoire/move", "evil rep", params={"repertoire": "w'hite", "fen": START, "move_uci": "e2e4"})
M("DELETE", "/api/repertoire/studies/1", "absent")
M("DELETE", "/api/repertoire/studies/999999", "absent big")
M("DELETE", "/api/repertoire/studies/abc", "str id")
M("DELETE", "/api/repertoire/studies/-1", "neg")

# ------------------------------------------------------------------- runner
BUDGET = float(os.environ.get("ATK5_BUDGET", "6.0"))
results = []
print(f"\n{len(MATRIX)} cases, per-case budget {BUDGET}s\n" + "-" * 78)

for method, path, label, kw in MATRIX:
    t0 = time.time()
    status = "TIMEOUT"
    body = ""
    extra = ""

    def _to(*a, **k):
        raise TimeoutError("budget")

    old = signal.signal(signal.SIGALRM, _to)
    signal.alarm(int(BUDGET) + 1)
    try:
        r = client.request(method, path, **kw)
        status = r.status_code
        ct = r.headers.get("content-type", "")
        if "text/event-stream" in ct:
            try:
                body = r.text[:200]
            except Exception:
                body = "<sse>"
        else:
            try:
                body = json.dumps(r.json())[:400]
            except Exception:
                body = r.text[:400].replace("\n", "\\n")
    except TimeoutError:
        status = "HANG"
    except Exception as e:
        status = "EXC"
        body = f"{type(e).__name__}: {e}"[:300]
    finally:
        signal.alarm(0)
        try:
            signal.signal(signal.SIGALRM, old)
        except Exception:
            pass

    dt = time.time() - t0
    flag = ""
    if status in ("HANG", "EXC") or (isinstance(status, int) and status >= 500):
        flag = "  <<<<<< PROBLEM"
    if dt > 2.0:
        flag += "  <<<<<< SLOW"
    print(f"{status!s:>7} {dt:6.2f}s  {method:6} {path:44} | {label:22}{flag}")
    if flag:
        print(f"         -> {body}")
    results.append((method, path, label, status, dt, body, flag))

print("-" * 78)
bad = [r for r in results if r[6]]
print(f"PROBLEMS: {len(bad)} / {len(results)}")
for r in bad:
    print(f"  {r[0]} {r[1]} [{r[2]}] -> {r[3]} in {r[4]:.2f}s :: {r[5][:200]}")

print("\nexplorer calls made during fuzz:", _net_calls["n"])
