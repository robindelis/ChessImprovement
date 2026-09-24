"""ATK3-22 — duplicate blunder + drill rows when a game is re-analysed while analyzed_at = 0."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import (hdr, q, q1, seed_game, seed_blunder, wipe_games, client,  # noqa: E402
                      ensure_username)
import chess  # noqa: E402
import backend.db as dbm  # noqa: E402
import backend.analyzer as anmod  # noqa: E402
import backend.lichess_client as lc  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

cl = client()
ensure_username()
F = chess.Board().fen(en_passant="fen")
BL = {"game_id": "dup1", "move_number": 1, "ply": 1, "color": "white",
      "fen_before": F, "fen_after": F + " 0 1", "played_move": "d2d4", "played_san": "d4",
      "best_move": "e2e4", "best_san": "e4", "eval_before": 30, "eval_after": -270,
      "cpl_loss": 300, "opening_name": "Queen's Pawn", "eco": "A40", "is_book_move": 0,
      "refutation_pv": "[]", "best_pv": "[]", "explanation": "x", "strategic_idea": "y",
      "category": "Blunder", "created_at": 1700000000}


def saver(self, blunders):
    self._real_save(blunders)


hdr("ATK3-22 — re-analysing a game whose analyzed_at is still 0 duplicates its blunders")
RealSave = anmod.GameAnalyzer.save_blunders_to_db


def save_without_bestpv(self, blunders):
    """Same INSERT as the real one minus the best_pv column (see SYNC-01)."""
    conn = dbm.get_db()
    cur = conn.cursor()
    for b in blunders:
        cur.execute("""INSERT INTO blunders (game_id, move_number, ply, color, fen_before,
                       fen_after, played_move, played_san, best_move, best_san, eval_before,
                       eval_after, cpl_loss, opening_name, eco, is_book_move, refutation_pv,
                       explanation, strategic_idea, category, opening_games, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (b["game_id"], b["move_number"], b["ply"], b["color"], b["fen_before"],
                     b["fen_after"], b["played_move"], b["played_san"], b["best_move"],
                     b["best_san"], b["eval_before"], b["eval_after"], b["cpl_loss"],
                     b["opening_name"], b["eco"], b["is_book_move"], b["refutation_pv"],
                     b["explanation"], b["strategic_idea"], b["category"],
                     b.get("opening_games"), b["created_at"]))
        bid = cur.lastrowid
        cur.execute("""INSERT OR IGNORE INTO drill_reviews (blunder_id, attempts, successes,
                       last_attempt_date, next_review_date, interval_days, status)
                       VALUES (?, 0, 0, 0, ?, 1.0, 'new')""", (bid, int(__import__("time").time())))
    conn.commit()
    conn.close()


anmod.GameAnalyzer.save_blunders_to_db = save_without_bestpv
anmod.GameAnalyzer.analyze_game_openings = lambda self, gd: [dict(BL, game_id=gd["id"])]
anmod.GameAnalyzer.close = lambda self: None
rm = RepertoireManager.get_instance()
rm.get_explorer_game_count = lambda fen, ratings="2000,2200,2500": 100000
lc.LichessClient.fetch_user_games = staticmethod(lambda username, max_games=None,
                                                 since=None, perf_type="x": [
    {"id": "dup1", "speed": "b", "rated": 1, "createdAt": 1700000000000, "winner": "black",
     "opening": {}, "players": {"white": {"user": {"name": "RobiDeli"}},
                                "black": {"user": {"name": "R"}}}, "moves": "d4 d5"}])

wipe_games()
seed_game("dup1", analyzed_at=0, result="black")
seed_blunder("dup1", ply=1, cpl=300, fen_before=F)
print("  before: blunders =", q1("SELECT COUNT(*) c FROM blunders")["c"],
      "| drill rows =", q1("SELECT COUNT(*) c FROM drill_reviews")["c"],
      "| analyzed_at =", q1("SELECT analyzed_at FROM games WHERE id='dup1'")["analyzed_at"])
for i in (1, 2):
    out = rm.analyze_user_games(max_games=5)
    print(f"  analyze run {i}: {out}")
print("  after : blunders =", q1("SELECT COUNT(*) c FROM blunders")["c"],
      "| drill rows =", q1("SELECT COUNT(*) c FROM drill_reviews")["c"])
print("  rows:", [(r["id"], r["ply"], r["cpl_loss"]) for r in
                  q("SELECT id, ply, cpl_loss FROM blunders ORDER BY id")])
print("  unique index on blunders?",
      [r["name"] for r in q("PRAGMA index_list(blunders)")])
print("  -> the same (game_id, ply, played_move) appears twice and the dashboard counts 2;")
print("     /api/blunders returns it twice, /api/dashboard averages both, and the trainer")
print("     gets two drill_reviews rows for the same mistake.")
