"""ATK3-02 : /api/sync marks games analyzed BEFORE analysis runs -> permanent silent data loss on failure.
ATK3-03 : changing blunder_threshold_cp / max_opening_ply never re-analyzes already-synced games.

!! ATK3-02 is FIXED during this audit round (analyzed_at is now stamped only after a
!! successful analysis, with per-game error isolation and try/finally).  This script now
!! aborts with SYNC-01's `no column named best_pv`, which is itself the proof that the
!! error path is reached.  ATK3-03 survives but is unreachable until SYNC-01 is fixed
!! (see audit/reports/SYNC-15.md).
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import hdr, wipe_games, seed_game, q, q1, fake_lichess_games, client  # noqa: E402

cl = client()
import backend.analyzer as an  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402


def game(gid):
    return {"id": gid, "speed": "blitz", "rated": True, "createdAt": 1700000000000,
            "winner": "white", "opening": {"name": "Italian Game", "eco": "C50"},
            "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1800},
                        "black": {"user": {"name": "Rival"}, "rating": 1800}},
            "moves": "e4 e5 Nf3 Nc6"}


hdr("ATK3-02a — engine raises on the 2nd game (simulated engine crash / UCI desync)")
fake_lichess_games([game("g1"), game("g2"), game("g3")])
calls = {"n": 0}
quits = []


def exploding_analyze(self, gd):
    calls["n"] += 1
    if gd["id"] == "g2":
        raise chess_engine_error("engine pipe closed")
    return []


def chess_engine_error(msg):
    class E(Exception):
        pass
    return E(msg)


an.GameAnalyzer.analyze_game_openings = exploding_analyze
an.GameAnalyzer._get_engine = lambda self: object()
an.GameAnalyzer.close = lambda self: quits.append("quit")

wipe_games()
r = cl.post("/api/sync?max_games=15")
print("  HTTP", r.status_code, r.text[:160])
print("  analyze_game_openings called", calls["n"], "times (expected 3)")
print("  games rows:", [(g["id"], g["analyzed_at"]) for g in q("SELECT id, analyzed_at FROM games")])
print("  engine close()/quit() called:", quits, "<-- never called on the error path")

hdr("ATK3-02b — retry the same sync: are the un-analyzed games ever retried?")
calls["n"] = 0
r2 = cl.post("/api/sync?max_games=15")
print("  HTTP", r2.status_code, r2.text[:160])
print("  analyze called", calls["n"], "times ->", "0 means permanently marked analyzed" if calls["n"] == 0 else "retried")

hdr("ATK3-02c — g2 got a row + analyzed_at but ZERO blunders; move_number/ply coverage lost forever")
print("  g2 row:", q1("SELECT id, analyzed_at, moves_san FROM games WHERE id='g2'"))

# ------------------------------------------------------------------ ATK3-03
hdr("ATK3-03 — lowering blunder_threshold_cp does not re-analyze already-synced games")
import backend.db as dbm  # noqa: E402
wipe_games()
dbm.set_setting("blunder_threshold_cp", "150")
dbm.set_setting("max_opening_ply", "24")
fake_lichess_games([game("gA")])
new_blunders = []


def finding_analyze(self, gd):
    # A stricter threshold WOULD flag this move as a blunder
    b = {"game_id": gd["id"], "move_number": 2, "ply": 3, "color": "white",
         "fen_before": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
         "fen_after": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKB1R b KQkq - 0 2",
         "played_move": "g1f3", "played_san": "Nf3", "best_move": "f1c4", "best_san": "Bc4",
         "eval_before": 10, "eval_after": -70, "cpl_loss": 80,
         "opening_name": "Italian Game", "eco": "C50", "is_book_move": 0,
         "refutation_pv": "[]", "best_pv": "[]", "explanation": "x", "strategic_idea": "y",
         "category": "Inaccuracy", "created_at": 1700000000}
    new_blunders.append(b)
    return [b]


an.GameAnalyzer.analyze_game_openings = finding_analyze
an.GameAnalyzer.close = lambda self: None
rm = RepertoireManager.get_instance()
print("  1st analyze_user_games ->", rm.analyze_user_games(max_games=5))
print("  blunders stored:", q1("SELECT COUNT(*) c FROM blunders")["c"])

dbm.set_setting("blunder_threshold_cp", "50")   # user makes the engine more sensitive
print("  threshold now:", dbm.get_setting("blunder_threshold_cp"))
print("  2nd analyze_user_games ->", rm.analyze_user_games(max_games=5))
print("  blunders stored:", q1("SELECT COUNT(*) c FROM blunders")["c"])
print("  analyze_game_openings invocations (only the first should count):", len(new_blunders))
print("  => the newly-in-scope 80cp inaccuracy is never surfaced, even after re-sync:")
r = cl.get("/api/blunders")
print("     /api/blunders ->", r.status_code, "rows:", len(r.json()) if r.status_code == 200 else r.text[:80])
