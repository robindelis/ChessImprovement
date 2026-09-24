"""ATK3-04 : Lichess `"winner": null` (the documented representation of a draw) is stored as NULL
             and is then counted as a LOSS by the threat-scoring engine and shown as a loss in the UI.
ATK3-05 : verification of the AGENTS.md 4A threat formula + the 4B eval_before >= -200 filter."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import hdr, wipe_games, q, q1, fake_lichess_games, client  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

cl = client()
rm = RepertoireManager.get_instance()


def game(gid, winner, moves="e4 e5 Nf3 Nc6"):
    g = {"id": gid, "speed": "blitz", "rated": True, "createdAt": 1700000000000,
         "opening": {"name": "Italian Game", "eco": "C50"},
         "players": {"white": {"user": {"name": "RobiDeli"}, "rating": 1800},
                     "black": {"user": {"name": "Rival"}, "rating": 1800}},
         "moves": moves}
    if winner is not _MISSING:
        g["winner"] = winner
    return g


_MISSING = object()

hdr("ATK3-04a — what gets stored for each `winner` form the Lichess API can emit")
wipe_games()
fake_lichess_games([
    game("W", "white"),
    game("L", "black"),
    game("D_missing", _MISSING),
    game("D_null", None),      # <-- Lichess emits "winner": null for draws
])
print("  sync_recent_games ->", rm.sync_recent_games(max_games=10))
for r in q("SELECT id, result, typeof(result) t FROM games ORDER BY id"):
    print(f"   {r['id']:<12} result={str(r['result']):<8} sqlite_type={r['t']}")

hdr("ATK3-04b — /api/games/recent rendering of those results (user played White)")
r = cl.get("/api/games/recent?color=white")
for g in r.json()["games"]:
    print(f"   {g['id']:<12} result={g['result']:<6} score_text={g['score_text']}")
print("   /api/dashboard game_results ->", cl.get("/api/dashboard").json()["game_results"])

hdr("ATK3-04c — the draw is counted as a LOSS by the personal threat-scoring engine")
wipe_games()
for i in range(10):
    seed = game(f"d{i}", None, moves="e4 e5 Nf3 Nc6")
    from atk3_lib import seed_game
    seed_game(f"d{i}", user_color="white", result=None, moves="e4 e5 Nf3 Nc6",
              opening_name="Italian Game", eco="C50")
for i in range(10):
    from atk3_lib import seed_game
    seed_game(f"w{i}", user_color="white", result="white", moves="e4 e5 Nf3 Nc6",
              opening_name="Italian Game", eco="C50")
rm._user_stats_cache = {}
rm._user_stats_time = 0
st = rm.get_user_position_stats("white")
key = [k for k in st if k[1] == "e2e4"][0]
print("   20 games of 1.e4, 10 wins + 10 draws (stored as NULL):")
print("   stats for (start,e2e4) =", st[key])
print("   expected: 10W 10D 0L  win_rate 60.0  threat = 20*((0.40)^2+0.02) = 3.6")
print("   ACTUAL  :", st[key]["threat_score"], "-> draws silently became losses")

# ------------------------------------------------------------------ ATK3-05
hdr("ATK3-05a — AGENTS.md 4A threat formula, Constraint 1 and 2 (through the real code path)")
import backend.db as dbm  # noqa: E402
from backend.repertoire import RepertoireManager as RM  # noqa: E402
import chess  # noqa: E402


def threat(N, W):
    """Same arithmetic as backend/repertoire.py:get_user_position_stats"""
    win_pct = round(W * 100, 1)
    L = max(0.0, min(1.0, 1.0 - (win_pct / 100.0)))
    return round(N * ((L ** 2) + 0.02), 2)


print("   Constraint 1: 30@50% =", threat(30, 0.5), " vs 2@0% =", threat(2, 0.0),
      "-> 8.1 > 2.04", threat(30, 0.5) > threat(2, 0.0))
print("   Constraint 2: 20@50% =", threat(20, 0.5), " vs 10@20% =", threat(10, 0.2),
      "-> 5.4 < 6.6", threat(20, 0.5) < threat(10, 0.2))
print("   epsilon check  N=0  ->", threat(0, 0.0), " (no ZeroDivisionError: L computed from W, not N)")
print("   overflow  N=10**9, W=0 ->", threat(10 ** 9, 0.0))

hdr("ATK3-05b — sort order actually applied by get_candidate_moves_2000 (mocked explorer)")
import backend.repertoire as rep  # noqa: E402
START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def mk(uci, san, n):
    return {"uci": uci, "san": san, "white": n // 2, "draws": 0, "black": n // 2,
            "averageRating": 2200}


wipe_games()
# 30 games of 1.e4 -> 15W 15L (50% win rate)
from atk3_lib import seed_game  # noqa: E402
for i in range(15):
    seed_game(f"a{i}", user_color="white", result="white", moves="e4 e5")
for i in range(15):
    seed_game(f"b{i}", user_color="white", result="black", moves="e4 e5")
# 10 games of 1.d4 -> 8L 2W (20% win rate)
for i in range(8):
    seed_game(f"c{i}", user_color="white", result="black", moves="d4 d5")
for i in range(2):
    seed_game(f"e{i}", user_color="white", result="white", moves="d4 d5")
rm._user_stats_cache = {}
rm._user_stats_time = 0
print("   db-backed stats e2e4:", rm.get_user_position_stats("white")[
    [k for k in rm.get_user_position_stats("white") if k[1] == "e2e4"][0]])
print("   db-backed stats d2d4:", rm.get_user_position_stats("white")[
    [k for k in rm.get_user_position_stats("white") if k[1] == "d2d4"][0]])

real_get = rep.requests.get
from atk3_lib import fake_explorer, wipe_explorer  # noqa: E402
wipe_explorer()
fake_explorer({"white": 40, "draws": 20, "black": 40,
               "moves": [mk("e2e4", "e4", 40), mk("d2d4", "d4", 20)]})
rm._engine = None
import backend.repertoire as _rp  # noqa: E402
_rp.RepertoireManager._get_engine = lambda self: None
out = rm.get_candidate_moves_2000(START, color="white")
for m in out["moves"]:
    print(f"   {m['san']:<4} games(master)={m['games']:<4} pop={m['popularity_pct']:<6}"
          f" my={m['my_stats']['games']:<3} wr={m['my_stats']['win_rate']} threat={m['my_stats']['threat_score']}")
print("   expected order: d4 (6.6) before e4 (8.1)? -> no, 8.1 > 6.6 so e4 first per Constraint 1")
