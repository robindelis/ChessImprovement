"""
CODE-05 repro #5: cross-module contract diffs for /api/repertoire/tree,
/api/repertoire/train/next, /api/repertoire/train/attempt, /api/dashboard.
Prints the concrete key/type inventory so it can be diffed against frontend/*.js.

Safe: isolated temp DB only.
Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-05_contract_diff.py
"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import tests.test_setup  # noqa: E402
import chess  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
START = chess.STARTING_FEN


def bld(line):
    b = chess.Board()
    for uci, san, is_user in line:
        client.post("/api/repertoire/move", json={
            "repertoire": "white", "parent_fen": b.fen(), "move_uci": uci,
            "is_user_move": is_user, "study_name": "Anti French Defense"})
        b.push_uci(uci)


bld([("e2e4", "e4", True), ("e7e6", "e6", False), ("d2d4", "d4", True)])

tree = client.get("/api/repertoire/tree?color=white&threshold=5.0").json()
key0 = next(iter(tree["moves_by_parent"]))
print("### /api/repertoire/tree -> moves_by_parent[first] ###")
print("key (parent_fen):", repr(key0), "  <-- token count:", len(key0.split()))
print(json.dumps(tree["moves_by_parent"][key0][0], indent=2))
print("top-level keys:", sorted(tree.keys()))

print("\n### is_important: tree says? ###")
for parent, kids in tree["moves_by_parent"].items():
    for m in kids:
        print(f"  is_user_move={m.get('is_user_move')!r:<6} is_important={m.get('is_important')!r:<6} san={m.get('move_san')}")

d = client.get("/api/repertoire/train/next?color=white").json()
print("\n### /api/repertoire/train/next ###")
print("is_important =", repr(d.get("is_important")), type(d.get("is_important")).__name__)
print("arrows =", repr(d.get("arrows")))
print("last_move =", repr(d.get("last_move")))
print("path_san =", repr(d.get("path_san")))

a = client.post("/api/repertoire/train/attempt", json={
    "repertoire": "white", "current_fen": START, "attempted_uci": "e2e4"}).json()
print("\n### /api/repertoire/train/attempt ###")
print("is_important =", repr(a.get("is_important")), "| next_is_important =", repr(a.get("next_is_important")))
print("opponent_reply keys =", sorted(a["opponent_reply"].keys()))
print("opponent_reply.fen =", repr(a["opponent_reply"]["fen"]), "tokens:", len(a["opponent_reply"]["fen"].split()))

dash = client.get("/api/dashboard").json()
print("\n### /api/dashboard ###")
print("keys:", sorted(dash.keys()))
rb = dash.get("recent_blunders") or []
if rb:
    print("recent_blunders[0] move fields:", {k: rb[0][k] for k in rb[0] if "move" in k or k in ("played_san", "best_san")})
print("NOTE: /api/dashboard is never fetched by any frontend/*.js (see report CODE-05-09)")
