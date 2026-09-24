"""ATK3-18 : blunders.best_pv is written by the analyzer but never created by the schema.
On a freshly migrated database NO blunder can be persisted at all."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import hdr, q, q1, wipe_games, client  # noqa: E402

cl = client()
import backend.db as dbm  # noqa: E402
from backend.analyzer import GameAnalyzer  # noqa: E402

hdr("ATK3-18a — columns created by backend.db.init_db() for `blunders`")
cols = [r["name"] for r in q("PRAGMA table_info(blunders)")]
print("  ", cols)
print("  'best_pv' present:", "best_pv" in cols)
print("  analyzer.save_blunders_to_db() INSERT list (backend/analyzer.py:243-257):")
src = open("backend/analyzer.py", encoding="utf-8").read()
blk = src[src.index("INSERT INTO blunders"):src.index("VALUES (?, ?, ?")]
print("   ", " ".join(blk.split()))

hdr("ATK3-18b — actually try to persist one blunder on this database")
an = GameAnalyzer()
blunders = [{
    "game_id": "gX", "move_number": 3, "ply": 5, "color": "white",
    "fen_before": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
    "fen_after": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKB1R b KQkq - 0 2",
    "played_move": "g1f3", "played_san": "Nf3", "best_move": "f1b5", "best_san": "Bb5",
    "eval_before": 20, "eval_after": -280, "cpl_loss": 300,
    "opening_name": "Ruy Lopez", "eco": "C60", "is_book_move": 0,
    "refutation_pv": "[]", "best_pv": "[]", "explanation": "x", "strategic_idea": "y",
    "category": "Blunder", "created_at": 1700000000,
}]
c = dbm.get_db()
c.execute("INSERT OR REPLACE INTO games (id, user_color, result, moves_san, analyzed_at) "
          "VALUES ('gX','white','black','e4 e5 Nf3 Nc6 Bb5',1)")
c.commit()
c.close()
try:
    an.save_blunders_to_db(blunders)
    print("  save_blunders_to_db() -> OK, blunders rows =",
          q1("SELECT COUNT(*) c FROM blunders")["c"])
except Exception as e:
    print("  save_blunders_to_db() RAISED %s: %s" % (type(e).__name__, e))
    print("  blunders rows =", q1("SELECT COUNT(*) c FROM blunders")["c"])

hdr("ATK3-18c — end-to-end: does a real /api/sync ever record a blunder?")
import backend.analyzer as anmod  # noqa: E402


def finding_analyze(self, gd):
    return [dict(blunders[0], game_id=gd["id"])]


anmod.GameAnalyzer.analyze_game_openings = finding_analyze
import backend.lichess_client as lc  # noqa: E402
lc.LichessClient.fetch_user_games = staticmethod(
    lambda self, username, max_games=None, since=None, perf_type="x": [{
        "id": "sync1", "speed": "blitz", "rated": True, "createdAt": 1700000000000,
        "winner": "black", "opening": {"name": "Ruy Lopez", "eco": "C60"},
        "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1800},
                    "black": {"user": {"name": "Rival"}, "rating": 1800}},
        "moves": "e4 e5 Nf3 Nc6 Bb5"}])
dbm.set_setting("lichess_username", "RobiDeli")
wipe_games()
r = cl.post("/api/sync?max_games=15")
print("  POST /api/sync ->", r.status_code, r.text[:300])
print("  games rows      :", q("SELECT id, analyzed_at FROM games"))
print("  blunders rows   :", q1("SELECT COUNT(*) c FROM blunders")["c"])
print("  drill_reviews   :", q1("SELECT COUNT(*) c FROM drill_reviews")["c"])

r2 = cl.post("/api/repertoire/sync_games?max_games=5")
print("  POST /api/repertoire/sync_games ->", r2.status_code, r2.text[:200])
print("  blunders rows   :", q1("SELECT COUNT(*) c FROM blunders")["c"])

hdr("ATK3-18d — the read path also expects the column")
print("  backend/api.py:819-825 and :1020-1027 do `blunder['best_pv']` / `blunder.get('best_pv')`")
print("  /api/blunders response keys for a real row:", sorted(
    (cl.get("/api/blunders").json() or [{}])[0].keys()) if cl.get("/api/blunders").json() else "n/a")
print("  -> any row that predates the column removal still works (SELECT b.*), but a row")
print("     written by the current analyzer cannot exist because the INSERT always fails.")
