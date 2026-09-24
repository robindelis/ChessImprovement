"""Shared fixture helpers for ATTACKER-2 (TRAINER) audit probes.

HARD RULES (see audit/BRIEF.md):
  * `import tests.test_setup` is the FIRST backend-adjacent import -> temp DB only.
  * Never touch chess_improvement.db, never hit http://127.0.0.1:38281.
  * Only TestClient(app) is used.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST  (isolated temp SQLite, atexit cleanup)

import chess
from fastapi.testclient import TestClient

from backend.api import app
from backend.db import get_db

START = chess.STARTING_FEN


def fresh():
    """Wipe the repertoire tables on the temp DB and return a TestClient."""
    conn = get_db()
    conn.execute("DELETE FROM repertoire_moves")
    conn.execute("DELETE FROM study_moves")
    conn.execute("DELETE FROM repertoire_studies")
    conn.execute("DELETE FROM drill_reviews")
    conn.execute("DELETE FROM blunders")
    conn.execute("DELETE FROM games")
    conn.commit()
    conn.close()
    return TestClient(app, raise_server_exceptions=False)


def add(client, color, parent_fen, uci, is_user, study=None, **kw):
    """POST /api/repertoire/move (goes through all API validation)."""
    payload = {
        "repertoire": color,
        "parent_fen": parent_fen,
        "move_uci": uci,
        "is_user_move": is_user,
    }
    if study is not None:
        payload["study_name"] = study
    payload.update(kw)
    r = client.post("/api/repertoire/move", json=payload)
    if r.status_code != 200:
        raise AssertionError(f"add failed {r.status_code}: {r.text}")
    return r.json()


def add_line(client, color, study, sans, base=START, popularity=1.0, important_at=()):
    """Add a whole PGN-ish SAN line.  `sans` are SAN moves in order; is_user_move is
    derived from the side to move vs. the repertoire colour (same rule the builder uses)."""
    b = chess.Board(base)
    out = []
    for i, san in enumerate(sans):
        mv = b.parse_san(san)
        uci = mv.uci()
        is_user = (b.turn == chess.WHITE) if color == "white" else (b.turn == chess.BLACK)
        out.append(add(
            client, color, b.fen(), uci, is_user, study=study,
            popularity_pct=popularity,
            is_important=(i in important_at) or None,
        ))
        b.push(mv)
    return b, out


def fens(sans, base=START):
    """Return [fen_before, fen_after, ...] for a SAN line."""
    b = chess.Board(base)
    res = [b.fen()]
    for san in sans:
        b.push_san(san)
        res.append(b.fen())
    return res


def row(move_uci, repertoire=None):
    conn = get_db()
    q = "SELECT * FROM repertoire_moves WHERE move_uci = ?"
    args = [move_uci]
    if repertoire:
        q += " AND repertoire = ?"
        args.append(repertoire)
    r = conn.execute(q, args).fetchone()
    conn.close()
    return dict(r) if r else None


def hdr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)
