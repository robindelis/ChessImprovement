"""[ATTACKER-3] HISTORICAL/REFERENCE PROBE.
Sections that printed a bug at the time of writing were FIXED by other agents during
this audit round; see audit/reports/SYNC-INDEX.md ("Fixed during this audit round").
Kept runnable for re-test.  Authoritative live status: audit/scripts/atk3_reverify_matrix.py
"""
"""ATK3-10 : get_game_gaps — opening cutoff boundaries, the opening_games back-fill (N+1 network
             calls + permanent poisoning on network failure), root-cause selection, is_patched."""
import os
import sys
import json
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import hdr, wipe_games, wipe_repertoire, q, q1, seed_game, seed_blunder, client  # noqa: E402

cl = client()
import chess  # noqa: E402
import backend.repertoire as rep  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402
rm = RepertoireManager.get_instance()

FENS = [chess.Board().fen()]
for sans in (["e4"], ["e4", "e5"], ["e4", "e5", "Nf3"], ["e4", "e5", "Nf3", "Nc6"],
             ["e4", "e5", "Nf3", "Nc6", "Bc4"], ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]):
    b = chess.Board()
    for s in sans:
        b.push_san(s)
    FENS.append(b.fen(en_passant="fen"))


class Exploder:
    """Counts calls, returns a fixed count, or fails like a dead network."""

    def __init__(self, count=10 ** 6, fail=False, delay=0.0):
        self.count, self.fail, self.delay = count, fail, delay
        self.calls = []

    def __call__(self, fen, ratings="2000,2200,2500"):
        self.calls.append(fen)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise OSError("network unreachable")
        return self.count


hdr("ATK3-10a — min_games cutoff boundaries (AGENTS.md 4B.3)")
wipe_games()
wipe_repertoire()
seed_game("gg", user_color="white", result="black", analyzed_at=1)
for i, f in enumerate(FENS):
    for og in (29, 30, 31):
        seed_blunder(f"gg{i}", ply=i * 2 + 1, move_number=i + 1, cpl=100 + i,
                     opening_games=og, fen_before=f, best_move="e2e4",
                     played_move="d2d4")
rm.get_explorer_game_count = Exploder(10 ** 6)
for mg in (0, 10, 20, 29, 30, 31, 50, 100):
    out = rm.get_game_gaps("white", min_games=mg)
    ogs = sorted({g["opening_games"] for g in out["gaps"]})
    print(f"  min_games={mg:<4} -> total_gaps={out['total_gaps']:<3} returned={len(out['gaps']):<3} "
          f"distinct opening_games in result={ogs}")
print("  (expected: 29 excluded / 30 and 31 included for min_games=30)")
r = cl.get("/api/repertoire/game_gaps?color=white&min_games=30")
print("  HTTP /api/repertoire/game_gaps:", r.status_code,
      "min_games field =", r.json()["min_games"], "gaps =", len(r.json()["gaps"]))
for bad in (-1, 0):
    print(f"  min_games={bad} -> HTTP", cl.get(f"/api/repertoire/game_gaps?min_games={bad}").status_code)

hdr("ATK3-10b — opening_games back-fill: N+1 sequential HTTP calls, done inside a GET")
wipe_games()
seed_game("bf", user_color="white", result="black", analyzed_at=1)
for i, f in enumerate(FENS):
    seed_blunder(f"bf{i}", ply=i * 2 + 1, move_number=i + 1, cpl=100 + i,
                 fen_before=f, opening_games=None)   # NULL -> triggers back-fill
ex = Exploder(10 ** 6, delay=0.05)
rm.get_explorer_game_count = ex
t0 = time.time()
out = rm.get_game_gaps("white", min_games=30)
print(f"  {len(FENS)} blunders with opening_games=NULL -> {len(ex.calls)} explorer HTTP calls "
      f"in {time.time() - t0:.2f}s (real timeout is 6s each => up to {len(FENS) * 6}s of hang)")
print("  stored opening_games:", [g["opening_games"] for g in q("SELECT opening_games FROM blunders ORDER BY id")])
print("  returned gaps:", len(out["gaps"]))

hdr("ATK3-10c — NETWORK FAILURE poisons opening_games=0 forever (no negative caching, no retry)")
wipe_games()
seed_game("nf", user_color="white", result="black", analyzed_at=1)
seed_blunder("nf1", ply=1, move_number=1, cpl=500, fen_before=FENS[2], opening_games=None)
seed_blunder("nf2", ply=3, move_number=2, cpl=400, fen_before=FENS[4], opening_games=None)
rm.get_explorer_game_count = Exploder(count=0)   # real impl returns 0 on any failure
out = rm.get_game_gaps("white", min_games=30)
print("  rows now:", q("SELECT id, ply, opening_games FROM blunders ORDER BY id"))
print("  gaps returned:", len(out["gaps"]), "-> both blunders are invisible")

rm.get_explorer_game_count = Exploder(10 ** 6)     # network healed
out = rm.get_game_gaps("white", min_games=30)
print("  after the network heals, re-request: gaps =", len(out["gaps"]),
      "(still 0: opening_games is no longer NULL so the back-fill never runs again)")
print("  rows:", q("SELECT id, opening_games FROM blunders ORDER BY id"))

hdr("ATK3-10d — does opening_games come from the explorer or from the user's own games?")
wipe_games()
seed_game("cnt", user_color="white", result="black", analyzed_at=1)
seed_blunder("c1", ply=1, move_number=1, cpl=500, fen_before=FENS[2], opening_games=None)
calls = []
rm.get_explorer_game_count = lambda fen, ratings="2000,2200,2500": (calls.append(fen) or 4242)
out = rm.get_game_gaps("white", min_games=30)
print("  explorer called for FENs:", len(calls), "-> value used:", out["gaps"][0]["opening_games"])
print("  -> the value is whatever the explorer returned; there is NO sanity bound.")
wipe_games()
seed_game("cnt2", user_color="white", result="black", analyzed_at=1)
seed_blunder("c2", ply=1, move_number=1, cpl=500, fen_before=FENS[2], opening_games=None)
rm.get_explorer_game_count = lambda fen, ratings="2000,2200,2500": 10 ** 12
out = rm.get_game_gaps("white", min_games=30)
print("  hostile explorer value 1e12 -> opening_games stored:",
      q1("SELECT opening_games FROM blunders LIMIT 1")["opening_games"],
      "| gaps:", len(out["gaps"]), "-> cutoff trivially bypassed by untrusted data")
