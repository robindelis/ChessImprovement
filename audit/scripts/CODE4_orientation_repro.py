#!/usr/bin/env python3
"""
CODE-4 repro: board-flip orientation used as the repertoire key (scout.js:1100).

Simulates exactly what frontend/scout.js:saveMoveNote() sends when the user has
tapped "Flip Board" (this.orientation === 'black') while sitting on a WHITE move:

    POST /api/repertoire/move_meta
    { "repertoire": "black",
      "parent_fen": "<start position, white to move>",
      "move_uci":   "e2e4",
      "comment":    "flipped-board note" }

and shows that the backend INSERTS 1.e4 as an opponent-response node at the root
of the BLACK repertoire instead of erroring out.

Runs against an isolated temp DB (tests.test_setup) - production
chess_improvement.db is never opened.

Idempotent: clears the repertoire tables on every run.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST backend-adjacent import (BRIEF hard rule 2)

import chess
from fastapi.testclient import TestClient

from backend.api import app
from backend.db import get_db

WHITE_MOVE = {"parent_fen": chess.STARTING_FEN, "move_uci": "e2e4",
              "comment": "flipped-board note"}


def dump(repertoire):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT parent_fen, move_uci, move_san, is_user_move, comment "
        "FROM repertoire_moves WHERE repertoire=? ORDER BY id", (repertoire,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def main():
    client = TestClient(app, raise_server_exceptions=False)

    conn = get_db()
    conn.execute("DELETE FROM repertoire_moves")
    conn.commit()
    conn.close()
    print("[setup] repertoire_moves cleared (temp DB)")

    # --- control: the CORRECT call (repertoire='white') -------------------
    r = client.post("/api/repertoire/move_meta",
                    json=dict(WHITE_MOVE, repertoire="white"))
    print("\n[control] repertoire=white  ->", r.status_code, r.json())
    print("  white rows:", dump("white"))

    conn = get_db()
    conn.execute("DELETE FROM repertoire_moves")
    conn.commit()
    conn.close()

    # --- the bug: board is flipped, move is still a WHITE move ------------
    r = client.post("/api/repertoire/move_meta",
                    json=dict(WHITE_MOVE, repertoire="black"))
    print("\n[BUG]    repertoire=black  ->", r.status_code, r.json())
    black = dump("black")
    print("  black rows:", black)

    polluted = [
        row for row in black
        if row["parent_fen"].split()[1] == "w" and not row["is_user_move"]
    ]
    if polluted:
        print("\n[FAIL-REPRODUCED] a White-to-move node was INSERTED into the "
              "BLACK repertoire as an opponent reply:")
        for row in polluted:
            print("   ", row)
        print("  -> /api/repertoire/train/next?color=black will now walk this "
              "branch and 'play' 1.e4 for the opponent.")
    else:
        print("\n[no-repro] black repertoire stayed clean")


if __name__ == "__main__":
    main()
