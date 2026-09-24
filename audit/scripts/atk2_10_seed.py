"""Seed the throwaway UI-demo DB used by audit/scripts/atk2_10_frontend_race.mjs.

Runs against a server started with CHESS_DB_PATH=/tmp/atk2_ui.db on port 39311
(NOT 38281).  The DB is created fresh by init_db(), so nothing touches production.
"""
import sys, os, json, urllib.request

BASE = "http://127.0.0.1:39311"
START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read())


def clear():
    for col in ("white", "black"):
        post("/api/repertoire/clear?repertoire=" + col, {})


def add_line(color, study, sans):
    import chess
    b = chess.Board()
    for san in sans:
        mv = b.parse_san(san)
        is_user = (b.turn == chess.WHITE) if color == "white" else (b.turn == chess.BLACK)
        post("/api/repertoire/move", {
            "repertoire": color, "parent_fen": b.fen(), "move_uci": mv.uci(),
            "is_user_move": is_user, "study_name": study, "popularity_pct": 50.0})
        b.push(mv)


if __name__ == "__main__":
    import chess
    clear()
    add_line("white", "RaceWhite", ["e4", "e5", "Nf3", "Nc6", "Bb5"])
    add_line("black", "RaceBlack", ["e4", "c5", "d4", "d6"])
    print("studies:", get("/api/repertoire/studies?repertoire=white"))
    print("drill  :", {k: v for k, v in get("/api/repertoire/train/next?color=white").items()
                       if k in ("id", "expected_uci", "study_name", "drill_reason")})
