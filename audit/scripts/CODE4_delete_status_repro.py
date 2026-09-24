#!/usr/bin/env python3
"""
CODE-4 repro: CODE-09 -- DELETE /api/repertoire/move always answers
{"status":"ok"} regardless of whether anything was deleted, so the Scout's
"Remove from Rep" / "Remove line" flows always report success.

Also demonstrates CODE-01's consequence: deleting with the WRONG `repertoire`
(the one chosen by the board-flip toggle in scout.js) silently removes nothing.

Isolated temp DB via tests.test_setup. Production DB never touched.
Idempotent: wipes repertoire_moves on every run.
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

client = TestClient(app, raise_server_exceptions=False)
START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"


def reset():
    conn = get_db()
    conn.execute("DELETE FROM repertoire_moves")
    conn.commit()
    conn.close()


def rows(rep):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT move_san FROM repertoire_moves WHERE repertoire=? "
                "ORDER BY id", (rep,))
    out = [r["move_san"] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return out


def norm(f):
    return " ".join(f.split()[:4])


def main():
    reset()
    print("[setup] temp DB cleaned\n")

    # Seed 1.e4 into the WHITE repertoire only.
    r = client.post("/api/repertoire/import_line",
                    json={"repertoire": "white",
                          "moves": [{"fen_before": norm(START), "uci": "e2e4",
                                     "san": "e4", "opening_name": "King's Pawn",
                                     "eco": "C20"}],
                          "study_name": "S"})
    print("[seed] white import ->", r.status_code, r.json().get("imported_count"))
    print("       white rows =", rows("white"), " black rows =", rows("black"))

    # ---- (a) delete with the WRONG repertoire (board flipped) ------------
    print("\n(a) DELETE with repertoire='black' for a move that only exists in white")
    r = client.request("DELETE", "/api/repertoire/move",
                      json={"repertoire": "black",
                            "parent_fen": START, "move_uci": "e2e4"})
    print("    ->", r.status_code, r.json())
    print("    white rows =", rows("white"), " black rows =", rows("black"))
    if r.json().get("status") == "ok" and rows("white") == ["e4"]:
        print("  [FAIL-REPRODUCED] the API answered status='ok' but deleted "
              "NOTHING (white still holds e4).")
        print("  -> frontend/scout.js:1860 shows '✓ Removed from Black "
              "Repertoire' + playSuccess() and then calls undoUserMove().")
    else:
        print("  [no-repro] the API reported the failure")

    # ---- (b) delete a move that does not exist at all --------------------
    print("\n(b) DELETE for a move that was never in the repertoire")
    r = client.request("DELETE", "/api/repertoire/move",
                      json={"repertoire": "white",
                            "parent_fen": norm(chess.STARTING_FEN),
                            "move_uci": "b1c3"})
    print("    ->", r.status_code, r.json())
    if r.json().get("status") == "ok":
        print("  [FAIL-REPRODUCED] status='ok' for a no-op delete; the client "
              "cannot distinguish it from a real removal.")

    # ---- (c) the only failing case the API does report --------------------
    print("\n(c) DELETE with an invalid FEN (the one case that errors)")
    r = client.request("DELETE", "/api/repertoire/move",
                      json={"repertoire": "white",
                            "parent_fen": "not-a-fen", "move_uci": "e2e4"})
    print("    ->", r.status_code, r.json())
    print("    -> but frontend/scout.js:1869 only console.error()s this; the "
          "user sees nothing at all.")


if __name__ == "__main__":
    main()
