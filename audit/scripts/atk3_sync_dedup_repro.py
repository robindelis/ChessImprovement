"""ATK3-01 : sync idempotency / dedup — does re-syncing clobber analyzed_at and duplicate blunders?

!! HISTORICAL - STEP 2/STEP 3 expectations below were FIXED during this audit round
!! (POST /api/sync now uses ON CONFLICT DO UPDATE, stamps analyzed_at only AFTER a
!! successful analysis, isolates per-game errors and always closes engine + connection).
!! Kept for re-test only.  The LIVE duplicate-blunder finding is SYNC-15
!! (audit/scripts/atk3_dup_blunder_repro.py).
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import (hdr, wipe_games, seed_game, seed_blunder, q, q1, fake_lichess_games,
                      client)  # noqa: E402

hdr("STEP 0 — raw SQLite semantics of INSERT OR REPLACE ... COALESCE((SELECT ... FROM games),0)")
c = __import__("backend.db", fromlist=["get_db"]).get_db()
c.execute("CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY, v INTEGER)")
c.execute("INSERT OR REPLACE INTO t VALUES ('a', 777)")
c.execute("""INSERT OR REPLACE INTO t (id, v) VALUES ('a', COALESCE((SELECT v FROM t WHERE id='a'), 0))""")
print("  after re-insert with COALESCE subquery -> v =", c.execute("SELECT v FROM t WHERE id='a'").fetchone()[0])
c.execute("DROP TABLE t")
c.commit()
c.close()

hdr("STEP 1 — sync_recent_games() over a game that was ALREADY analyzed (analyzed_at>0)")
wipe_games()
seed_game("g1", analyzed_at=1700009999, result="white")
seed_blunder("g1", ply=1, cpl=300)
print("  before: analyzed_at =", q1("SELECT analyzed_at FROM games WHERE id='g1'")["analyzed_at"],
      "| blunders =", q1("SELECT COUNT(*) c FROM blunders")["c"])

fake_lichess_games([{
    "id": "g1", "speed": "blitz", "rated": True, "createdAt": 1700000000000,
    "winner": "white", "opening": {"name": "Italian Game", "eco": "C50"},
    "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1800},
                "black": {"user": {"name": "Rival"}, "rating": 1800}},
    "moves": "e4 e5 Nf3 Nc6",
}])
from backend.repertoire import RepertoireManager  # noqa: E402
rm = RepertoireManager.get_instance()
print("  sync_recent_games ->", rm.sync_recent_games(max_games=5))
print("  after : analyzed_at =", q1("SELECT analyzed_at FROM games WHERE id='g1'")["analyzed_at"],
      "| blunders =", q1("SELECT COUNT(*) c FROM blunders")["c"])

hdr("STEP 2 — POST /api/sync over the same game (mocked fetch), repeated 3x")
fake_lichess_games([{
    "id": "g1", "speed": "blitz", "rated": True, "createdAt": 1700000000000,
    "winner": "white", "opening": {"name": "Italian Game", "eco": "C50"},
    "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1800},
                "black": {"user": {"name": "Rival"}, "rating": 1800}},
    "moves": "e4 e5 Nf3 Nc6",
}])
cl = client()
import backend.lichess_client as lc  # noqa: E402

# Neutralise Stockfish: analyze_game_openings will be monkeypatched to a deterministic stub
import backend.analyzer as an  # noqa: E402
import backend.api as api  # noqa: E402


def fake_analyze(self, game_dict):
    return [{
        "game_id": game_dict["id"], "move_number": 1, "ply": 1, "color": "white",
        "fen_before": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "fen_after": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1",
        "played_move": "d2d4", "played_san": "d4", "best_move": "e2e4", "best_san": "e4",
        "eval_before": 30, "eval_after": -270, "cpl_loss": 300,
        "opening_name": "Italian Game", "eco": "C50", "is_book_move": 0,
        "refutation_pv": "[]", "best_pv": "[]", "explanation": "x",
        "strategic_idea": "y", "category": "Blunder", "created_at": 1700000000,
    }]


an.GameAnalyzer.analyze_game_openings = fake_analyze
an.GameAnalyzer.close = lambda self: None

wipe_games()
seed_game("g1", analyzed_at=0)
for i in range(3):
    r = cl.post("/api/sync?max_games=15")
    print(f"  sync #{i+1}: HTTP {r.status_code} -> {r.text[:120]}")
    print("      games rows =", q1("SELECT COUNT(*) c FROM games")["c"],
          "| blunder rows =", q1("SELECT COUNT(*) c FROM blunders")["c"],
          "| drill rows =", q1("SELECT COUNT(*) c FROM drill_reviews")["c"])

hdr("STEP 3 — re-analysis when analyzed_at is 0 (the sync_recent_games state)")
wipe_games()
seed_game("g1", analyzed_at=0)
seed_blunder("g1", ply=1, cpl=300)
seed_blunder("g1", ply=3, cpl=400)
print("  before: blunders =", q1("SELECT COUNT(*) c FROM blunders")["c"])
r = cl.post("/api/sync?max_games=15")
print("  POST /api/sync ->", r.status_code, r.text[:100])
print("  after : blunders =", q1("SELECT COUNT(*) c FROM blunders")["c"],
      [(b["ply"], b["cpl_loss"]) for b in q("SELECT ply, cpl_loss FROM blunders ORDER BY id")])
