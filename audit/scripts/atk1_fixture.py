"""Shared fixture helpers for ATTACKER-1 (Repertoire Builder) probes."""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.test_setup  # noqa: E402,F401  (FIRST backend-adjacent import)

import chess  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402
from backend.db import get_db  # noqa: E402
from backend.repertoire import RepertoireManager, normalize_fen  # noqa: E402

CLIENT = TestClient(app, raise_server_exceptions=False)


def raw(sql, params=()):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def one(sql, params=()):
    r = raw(sql, params)
    return r[0] if r else None


def wipe():
    conn = get_db()
    cur = conn.cursor()
    for t in ("repertoire_moves", "study_moves", "repertoire_studies", "explorer_cache",
              "blunders", "games"):
        cur.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    RepertoireManager._instance = None


def add(repertoire, parent_fen, uci, **kw):
    body = {"repertoire": repertoire, "parent_fen": parent_fen, "move_uci": uci}
    body.update(kw)
    return CLIENT.post("/api/repertoire/move", json=body)


def fen_after(sans):
    """FEN (full 6-token) after replaying `sans` from the start position."""
    b = chess.Board()
    for s in sans:
        b.push(b.parse_san(s))
    return b.fen()


def parent_of(sans):
    return fen_after(sans[:-1]) if len(sans) > 1 else chess.STARTING_FEN


def add_line(repertoire, sans, study_name=None, start=None, **kw):
    """Push a list of SAN moves; returns list of (san, status, json)."""
    board = chess.Board(start) if start else chess.Board()
    out = []
    for san in sans:
        m = board.parse_san(san)
        payload = {"is_user_move": (board.turn == chess.WHITE) == (repertoire == "white")}
        if study_name:
            payload["study_name"] = study_name
        payload.update(kw)
        r = add(repertoire, board.fen(), m.uci(), **payload)
        out.append((san, r.status_code, r.json()))
        board.push(m)
    return out


def tree_rows(repertoire):
    return raw(
        "SELECT id, parent_fen, fen, move_uci, move_san, is_user_move, study_name, is_important, comment "
        "FROM repertoire_moves WHERE repertoire = ? ORDER BY id", (repertoire,))


def show_tree(repertoire, label=""):
    print(f"--- tree[{repertoire}] {label}")
    for r in tree_rows(repertoire):
        print(f"  id={r['id']:<3} {r['move_san']:<6} uci={r['move_uci']:<5} "
              f"user={r['is_user_move']} study={r['study_name']!r:<20} "
              f"parent3={' '.join(r['parent_fen'].split()[:3])}")
    return tree_rows(repertoire)


def study_rows():
    return raw("SELECT id, name, repertoire FROM repertoire_studies ORDER BY id")


def show_studies(label=""):
    print(f"--- studies {label}")
    for s in study_rows():
        n = one("SELECT COUNT(*) c FROM study_moves WHERE study_id=?", (s["id"],))["c"]
        print(f"  id={s['id']:<3} {s['name']!r:<28} {s['repertoire']:<6} study_moves={n}")
    return study_rows()


def dangling_study_moves():
    return raw("""SELECT sm.* FROM study_moves sm
                  LEFT JOIN repertoire_moves rm
                    ON rm.repertoire=sm.repertoire
                   AND rm.parent_fen=sm.parent_fen
                   AND rm.move_uci=sm.move_uci
                  WHERE rm.id IS NULL""")


def restart_app_db():
    """Simulate a backend restart (start.py -> init_db -> migrations)."""
    import backend.db as dbm
    dbm.init_db()
