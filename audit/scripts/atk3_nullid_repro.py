"""[ATTACKER-3] HISTORICAL/REFERENCE PROBE.
Sections that printed a bug at the time of writing were FIXED by other agents during
this audit round; see audit/reports/SYNC-INDEX.md ("Fixed during this audit round").
Kept runnable for re-test.  Authoritative live status: audit/scripts/atk3_reverify_matrix.py
"""
"""ATK3-07 : games with a NULL primary key -> dedup is impossible, unbounded row growth,
             and their blunders become unreachable (INNER JOIN on NULL)."""
import os
import sys
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import hdr, wipe_games, q, q1, seed_blunder, fake_ndjson, restore_network, client  # noqa: E402

cl = client()
import backend.analyzer as an  # noqa: E402


def g(gid):
    return {"id": gid, "speed": "blitz", "rated": True, "createdAt": 1700000000000,
            "winner": "white", "opening": {"name": "Italian Game", "eco": "C50"},
            "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1800},
                        "black": {"user": {"name": "Rival"}, "rating": 1800}},
            "moves": "e4 e5 Nf3 Nc6"}


def fake_analyze(self, gd):
    if not gd.get("id"):
        return [{
            "game_id": gd.get("id"), "move_number": 2, "ply": 3, "color": "white",
            "fen_before": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "fen_after": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKB1R b KQkq - 0 2",
            "played_move": "g1f3", "played_san": "Nf3", "best_move": "f1c4", "best_san": "Bc4",
            "eval_before": 10, "eval_after": -270, "cpl_loss": 280,
            "opening_name": "Italian Game", "eco": "C50", "is_book_move": 0,
            "refutation_pv": "[]", "best_pv": "[]", "explanation": "x", "strategic_idea": "y",
            "category": "Blunder", "created_at": 1700000000}]
    return []


an.GameAnalyzer.analyze_game_openings = fake_analyze
an.GameAnalyzer._get_engine = lambda self: object()
an.GameAnalyzer.close = lambda self: None

hdr("ATK3-07 — POST /api/sync five times with a game that has no 'id'")
wipe_games()
for i in range(5):
    fake_ndjson([json.dumps(g(None))])
    r = cl.post("/api/sync?max_games=20")
    restore_network()
    print(f"  run {i+1}: HTTP {r.status_code} {r.text[:60]}  games={q1('SELECT COUNT(*) c FROM games')['c']}"
          f"  null-id={q1('SELECT COUNT(*) c FROM games WHERE id IS NULL')['c']}"
          f"  blunders={q1('SELECT COUNT(*) c FROM blunders')['c']}")

print("\n  duplicate identical rows now in `games`:")
for r in q("SELECT COUNT(*) n, played_at, moves_san FROM games GROUP BY played_at, moves_san"):
    print("   ", r)

print("\n  /api/blunders (INNER JOIN games ON b.game_id = g.id):")
print("   ", cl.get("/api/blunders").json() and len(cl.get("/api/blunders").json()), "rows,",
      "despite", q1("SELECT COUNT(*) c FROM blunders")["c"], "blunders in the table")
print("   /api/dashboard total_blunders:",
      cl.get("/api/dashboard").json()["total_blunders"], "(counts the orphans)")

hdr("ATK3-07b — import_game_opening for the NULL id")
print("  ", cl.post("/api/repertoire/import_game_opening",
                   json={"game_id": "None", "repertoire": "white"}).text[:200])
