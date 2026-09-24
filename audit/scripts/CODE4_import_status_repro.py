#!/usr/bin/env python3
"""
CODE-4 repro: the line-import success path is driven only by the HTTP envelope,
so a total per-move failure is reported as a successful import (CODE-04), and
the documented "replaces your prepared move" warning is never shown.

Exercises POST /api/scout/import_repertoire with:
  (a) a fully valid line                      -> success, imported_count == N
  (b) a line whose every move is illegal      -> status STILL "success",
                                                imported_count == 0,
                                                errors non-empty
  (c) a line that diverges from a prepared user move -> the replacement
                                                warning exists in `message`
                                                but the client never displays it

Isolated temp DB via tests.test_setup. Production DB never touched.
Idempotent: wipes scouted_content + repertoire_moves on every run.
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
    for table in ("repertoire_moves", "scouted_content"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()


def fen_after(fen, uci):
    b = chess.Board(fen)
    b.push_uci(uci)
    return " ".join(b.fen().split()[:4])


def san_of(fen, uci):
    return chess.Board(fen).san(chess.Move.from_uci(uci))


def import_line(moves, study):
    r = client.post("/api/scout/import_repertoire",
                    json={"repertoire": "white", "moves": moves,
                          "study_name": study})
    return r.status_code, r.json()


def main():
    reset()
    print("[setup] temp DB cleaned\n")

    # ---------- (a) happy path -------------------------------------------
    moves = []
    f = START
    for uci in ("e2e4", "e7e5"):
        moves.append({"fen_before": f, "uci": uci, "san": san_of(f, uci),
                      "opening_name": "King's Pawn", "eco": "C20",
                      "comment": "", "is_important": False, "arrows": ""})
        f = fen_after(f, uci)
    code, data = import_line(moves, "StudyA")
    print("(a) valid 2-ply line ->", code,
          {k: data.get(k) for k in
           ("status", "imported_count", "already_present", "overlap_status",
            "errors")})

    # ---------- (b) every move illegal -----------------------------------
    reset()
    bad = [{"fen_before": START, "uci": "e2e5", "san": "e5"},   # illegal
           {"fen_before": START, "uci": "h1h3", "san": "h3"}]   # illegal
    code, data = import_line(bad, "StudyB")
    print("\n(b) 2 illegal moves  ->", code,
          {k: data.get(k) for k in
           ("status", "imported_count", "errors", "overlap_status")})
    if data.get("status") == "success" and data.get("imported_count") == 0:
        print("  [FAIL-REPRODUCED] server answers status='success' with "
              "imported_count=0 and errors=%r" % (data.get("errors"),))
        print("  -> frontend/scout.js:1663 `if (data.status === 'success')` "
              "renders '✓ Added to White Repertoire' + playSuccess().")

    # ---------- (c) divergence / replacement warning ----------------------
    reset()
    f = START
    prep = []
    for uci in ("e2e4", "c7c5", "g1f3"):   # user's PREPARED line: 1.e4 c5 2.Nf3
        prep.append({"fen_before": f, "uci": uci, "san": san_of(f, uci),
                     "opening_name": "Sicilian", "eco": "B20",
                     "comment": "prepared", "is_important": False, "arrows": ""})
        f = fen_after(f, uci)
    code, data = import_line(prep, "StudyC")
    print("\n(c0) prepared line   ->", code,
          {k: data.get(k) for k in ("status", "imported_count")})

    # import a line that follows 1.e4 c5 but plays 2.Nc3 where the
    # repertoire already has the *user* move 2.Nf3 prepared
    f = START
    alt = []
    for uci in ("e2e4", "c7c5", "b1c3"):
        alt.append({"fen_before": f, "uci": uci, "san": san_of(f, uci),
                    "opening_name": "Sicilian", "eco": "B20",
                    "comment": "", "is_important": False, "arrows": ""})
        f = fen_after(f, uci)
    code, data = import_line(alt, "StudyC")
    print("(c1) divergent line  ->", code,
          {k: data.get(k) for k in
           ("status", "overlap_status", "matched_count", "new_count",
            "imported_count")})
    print("    server message   :", data.get("message"))
    print("    divergence_info  :", (data.get("overlap_details") or {})
          .get("divergence_info"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT move_san FROM repertoire_moves ORDER BY id")
    sans = [r["move_san"] for r in cur.fetchall()]
    cur.close()
    conn.close()
    print("    repertoire now   :", sans)
    if "replaces your prepared" in (data.get("message") or ""):
        print("  [FAIL-REPRODUCED] server says %r" % data.get("message"))
        print("      and the prepared Nf3 was %s from the DB." % (
            "DESTROYED" if "Nf3" not in sans else "kept"))
        print("      But frontend/scout.js only ever writes that message into "
              "a title= tooltip\n      (lines 1227/1230/1239/1242, 1568/1571) "
              "and then overwrites it on the\n      next debounced "
              "checkTreeOverlap(); the toast says only '✓ Added to White "
              "Repertoire'.")


if __name__ == "__main__":
    main()
