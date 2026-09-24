"""
CODE-05 repro #3: dump the ACTUAL response shape of the 10 most important endpoints
(against the isolated temp DB only) so the shapes can be diffed against what
frontend/*.js reads.

Safe: uses TestClient on the isolated temp DB. No network, no production DB.

Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-05_contract_dump.py
"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import tests.test_setup  # noqa: E402,F401
import chess  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"


def shape(obj, depth=0):
    if depth > 2:
        return "..."
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in list(obj.items())[:22]}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1)] if obj else []
    return type(obj).__name__


def show(label, path, method="get", **kw):
    r = client.get(path, **kw) if method == "get" else client.post(path, **kw)
    print("=" * 78)
    print(f"{method.upper()} {path}  -> HTTP {r.status_code}")
    try:
        body = r.json()
    except Exception:
        print("  <non-json>", r.text[:200])
        return None
    print(json.dumps(shape(body), indent=2)[:2600])
    return body


# Seed a small tree so tree/train endpoints are non-trivial.
b = chess.Board()
client.post("/api/repertoire/move", json={"repertoire": "white", "parent_fen": b.fen(), "move_uci": "e2e4", "is_user_move": True})
b.push_san("e4")
client.post("/api/repertoire/move", json={"repertoire": "white", "parent_fen": b.fen(), "move_uci": "e7e6", "is_user_move": False})
b.push_san("e6")
client.post("/api/repertoire/move", json={"repertoire": "white", "parent_fen": b.fen(), "move_uci": "d2d4", "is_user_move": True})

show("dashboard", "/api/dashboard")
show("settings", "/api/settings")
show("tree", "/api/repertoire/tree?color=white&threshold=5.0")
show("train_next", "/api/repertoire/train/next?color=white")
show("train_progress", "/api/repertoire/train/progress?color=white")
show("train_openings", "/api/repertoire/train/openings?color=white")
show("attempt", "/api/repertoire/train/attempt", "post", json={"repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"})
show("explore", f"/api/repertoire/explore?fen={E4.replace(' ', '%20')}")
show("game_gaps", "/api/repertoire/game_gaps?color=white&min_games=30")
show("scout_recent", "/api/scout/recent?limit=2")
show("studies", "/api/repertoire/studies")
