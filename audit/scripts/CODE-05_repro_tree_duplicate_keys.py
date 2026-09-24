"""
CODE-05 repro #6: /api/repertoire/tree returns DUPLICATE parent_fen keys for the same
position — some keys are 3 FEN tokens, some are 4 (en-passant field included).
The frontend keys its line tree off moves_by_parent, so every move shows up twice.

Safe: isolated temp DB only.
Run: /home/robin/miniconda3/bin/python3 audit/scripts/CODE-05_repro_tree_duplicate_keys.py
"""
import os
import sys
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import tests.test_setup  # noqa: E402
import chess  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)

# Single 3-ply line: 1. e4 e6 2. d4  (no en passant square exists at any of these nodes)
b = chess.Board()
for uci, is_user in (("e2e4", True), ("e7e6", False), ("d2d4", True)):
    parent = b.fen()
    print(f"posting parent_fen tokens={len(parent.split())}  {' '.join(parent.split()[3:])}  move={uci}")
    client.post("/api/repertoire/move", json={"repertoire": "white", "parent_fen": parent,
                                             "move_uci": uci, "is_user_move": is_user})
    b.push_uci(uci)

tree = client.get("/api/repertoire/tree?color=white").json()
mbp = tree["moves_by_parent"]
print(f"\n### moves_by_parent: {len(mbp)} keys for 3 moves ###")
for k, v in mbp.items():
    print(f"  tokens={len(k.split())}  {k}")
    for m in v:
        print(f"       -> {m['move_san']:<4} id={m['id']} parent_fen_tokens={len(m['parent_fen'].split())}")

sans = [m["move_san"] for v in mbp.values() for m in v]
print("\nflattened SAN list:", sans)
print("total_moves field  :", tree["total_moves"])
dupes = {s: c for s, c in Counter(sans).items() if c > 1}
print("DUPLICATED SANs     :", dupes if dupes else "none")

# Show the frontend side: what repertoire_builder.js does with these keys
print("\n### frontend consumer ###")
