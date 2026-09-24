"""[ATTACKER-3] HISTORICAL/REFERENCE PROBE.
Sections that printed a bug at the time of writing were FIXED by other agents during
this audit round; see audit/reports/SYNC-INDEX.md ("Fixed during this audit round").
Kept runnable for re-test.  Authoritative live status: audit/scripts/atk3_reverify_matrix.py
"""
"""ATK3-20 : get_repertoire_tree duplicates explorer queries + engine evaluations.
ATK3-21 : AGENTS.md 4B.2/4D features that the frontend never consumes (static + API proof)."""
import os
import sys
import glob
import json
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import (hdr, q, q1, wipe_games, wipe_repertoire, wipe_explorer,  # noqa: E402
                      fake_explorer, restore_network, client, ensure_username)
import chess  # noqa: E402
import chess.engine  # noqa: E402
import backend.repertoire as rp  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

cl = client()
ensure_username()
rm = RepertoireManager.get_instance()
rm.get_explorer_game_count = lambda fen, ratings="2000,2200,2500": 100000

hdr("ATK3-20 — how much work does one GET /api/repertoire/tree actually do?")
wipe_repertoire()
wipe_explorer()
wipe_games()
calls = {"explorer": 0, "analyse": 0}


class CountingEngine:
    def analyse(self, board, limit=None):
        calls["analyse"] += 1
        return {"score": chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE), "pv": []}


stub = fake_explorer({"white": 100, "draws": 10, "black": 100,
                      "moves": [{"uci": "e7e5", "san": "e5", "white": 50, "draws": 5,
                                 "black": 50},
                                {"uci": "c7c5", "san": "c5", "white": 30, "draws": 3,
                                 "black": 30}]})
_orig_get = stub.get


def counting_get(url, params=None, headers=None, timeout=None, **kw):
    calls["explorer"] += 1
    return _orig_get(url, params=params, headers=headers, timeout=timeout, **kw)


stub.get = counting_get
_real_engine = rp.RepertoireManager._get_engine
rp.RepertoireManager._get_engine = lambda self: CountingEngine()
b = chess.Board()
m = b.parse_san("e4")
rm.add_repertoire_move("white", b.fen(), m.uci(), is_user_move=True, study_name="S")
b.push(m)
for san, user in (("e5", False), ("Nf3", True), ("Nc6", False), ("Bb5", True)):
    mm = b.parse_san(san)
    rm.add_repertoire_move("white", b.fen(), mm.uci(), is_user_move=user, study_name="S")
    b.push(mm)
print("  repertoire:", [(r["move_uci"], r["is_user_move"]) for r in
                        q("SELECT move_uci, is_user_move FROM repertoire_moves ORDER BY id")])
wipe_explorer()
calls["explorer"] = calls["analyse"] = 0
t0 = time.time()
r = cl.get("/api/repertoire/tree?color=white&min_games=30")
restore_network()
rp.RepertoireManager._get_engine = _real_engine
print(f"  HTTP {r.status_code} in {time.time() - t0:.2f}s")
print("  distinct opponent positions in the tree: 2 (after 1.e4 and after 1.e4 e5 2.Nf3)")
print("  explorer HTTP requests:", calls["explorer"], "(expected 2)")
print("  engine analyse() calls:", calls["analyse"], "(2 positions x 2 candidate moves x 2 passes = 8)")
print("  coverage_pct:", r.json().get("coverage_pct"))
print("  -> get_repertoire_tree() calls get_candidate_moves_2000() twice per opponent")
print("     position (l.1248 gaps loop + l.1270 coverage loop). The second call is served")
print("     from explorer_cache only because the first one just wrote it, so the network")
print("     is fine on a warm cache, but on a COLD cache the engine still evaluates the")
print("     same moves twice, and the two passes can disagree (pass 1 is uncached, so its")
print("     per-move `eval` values are the ones stored; pass 2 reads them back).")

hdr("ATK3-21 — AGENTS.md 4B.2 / 4D features the frontend never reads")
files = sorted(glob.glob("frontend/*.js") + glob.glob("frontend/*.html") +
               glob.glob("frontend/*.css"))


def count(token):
    hits = {os.path.basename(f): open(f, encoding="utf-8").read().count(token)
            for f in files if token in open(f, encoding="utf-8").read()}
    return hits or "NONE"


for tok in ("earlier_mistake", "is_patched", "patched_count", "unpatched_count",
            "all_blunders", "Show Patched", "showPatchedGaps", "include_patched",
            "explanation", "cpl_loss", "refutation_pv", "master_stats"):
    print(f"  {tok:<18} -> {count(tok)}")
api_keys = sorted(cl.get("/api/repertoire/game_gaps?color=white&min_games=0").json().keys())
print("  /api/repertoire/game_gaps response keys:", api_keys)
print("  -> the API computes earlier_mistake / is_patched / patched_count / unpatched_count")
print("     and the frontend references NONE of them: there is no root-cause jump and no")
print("     'Show Patched (N)' toggle, so AGENTS.md 4B.2 and 4D are unimplemented.")

hdr("ATK3-21b — blunder detail: is the stored `explanation` string surfaced anywhere?")
wipe_games()
from atk3_lib import seed_game, seed_blunder  # noqa: E402
seed_game("z1", analyzed_at=1)
seed_blunder("z1", ply=1, cpl=500, fen_before=chess.Board().fen(en_passant="fen"),
             opening_games=100000, opening_name="Italian Game", eco="C50")
d = cl.get("/api/blunders/%d" % q1("SELECT id FROM blunders LIMIT 1")["id"]).json()
print("  /api/blunders/{id} keys include:", [k for k in sorted(d) if "expl" in k or "pv" in k])
print("  explanation =", repr(d.get("explanation")), "| best_pv =", d.get("best_pv"))
