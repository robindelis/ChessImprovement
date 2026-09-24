"""ATK3-09 : analyzer blunder detection — mate scores, cpl arithmetic, the eval_before>=-200 filter,
             threshold handling, and the root-cause 'earliest mistake' selection."""
import os
import sys
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import hdr, wipe_games, q, q1, seed_game, seed_blunder, client  # noqa: E402

cl = client()
import chess  # noqa: E402
import chess.engine  # noqa: E402
from backend.analyzer import GameAnalyzer  # noqa: E402


class FakeEngine:
    """Scripted engine.

    `fn(board)` returns (PovScore-compatible score already expressed from the
    *user's* colour, [legal pv moves]).  Constructing the PovScore with the user's
    colour means both `pov(board.turn)` (before) and `pov(not board.turn)` (after)
    yield exactly the value we scripted — i.e. the fake mimics a real engine.
    """

    def __init__(self, fn, user_chess_color=chess.WHITE):
        self.fn = fn
        self.uc = user_chess_color
        self.calls = []

    def analyse(self, board, limit=None):
        self.calls.append(board.fen())
        sc, pv = self.fn(board)
        return {"score": chess.engine.PovScore(sc, self.uc), "pv": pv}

    def quit(self):
        pass


def pv_of(board, sans):
    b = board.copy()
    out = []
    for s in sans:
        try:
            m = b.parse_san(s)
        except Exception:
            legal = list(b.legal_moves)
            if not legal:
                break
            m = legal[0]
        out.append(m)
        b.push(m)
    return out


hdr("ATK3-09a — mate handling: user is getting mated in the opening")
g = {"id": "mate1", "user_color": "white", "moves": "f3 e5 g4 Qh4#",
     "opening": {"name": "Fool's Mate", "eco": "A00"}}

an = GameAnalyzer()
an._engine = FakeEngine(lambda b: (chess.engine.Mate(-1), pv_of(b, ["e5"])))
bl = an.analyze_game_openings(dict(g))
for b in bl:
    print(f"  played {b['played_san']:<5} eval_before={b['eval_before']:<8} "
          f"eval_after={b['eval_after']:<8} cpl_loss={b['cpl_loss']:<8} cat={b['category']}")
    print(f"    explanation: {b['explanation']}")
if not bl:
    print("  (no blunder recorded)")

hdr("ATK3-09b — a move that IMPROVES a winning position (cpl should be <= 0 -> not a blunder)")
g = {"id": "good1", "user_color": "white", "moves": "e4 e5 Nf3 Nc6 Bc4",
     "opening": {"name": "Italian", "eco": "C50"}}
st = {"n": 0}


def improving(b):
    st["n"] += 1
    return (chess.engine.Cp(40 if st["n"] % 2 else 120), pv_of(b, ["Nf6"]))


an._engine = FakeEngine(improving)
bl = an.analyze_game_openings(dict(g))
print("  before=+40 after=+120 -> blunders:",
      [(b["played_san"], b["eval_before"], b["eval_after"], b["cpl_loss"]) for b in bl],
      "(correctly none)")

hdr("ATK3-09c — the eval_before >= -200 filter (AGENTS.md 4B.1)")
for before, after, label in [(-199, -900, "eval_before=-199 (must be KEPT)"),
                             (-200, -900, "eval_before=-200 (boundary, must be KEPT)"),
                             (-201, -900, "eval_before=-201 (must be DROPPED)"),
                             (-900, 30, "already lost, then recovers (must be DROPPED)")]:
    an._engine = FakeEngine(lambda b, be=before, af=after: (chess.engine.Cp(be),
                                                            pv_of(b, ["Nf6"])))
    # the second analyse (after the move) must return `after`; emulate by ply position
    st = {"n": 0}

    def fn(b, be=before, af=after):
        st["n"] += 1
        return (chess.engine.Cp(be if st["n"] % 2 == 1 else af), pv_of(b, ["Nf6"]))

    an._engine = FakeEngine(fn)
    bl = an.analyze_game_openings({"id": f"f{before}", "user_color": "white",
                                  "moves": "e4 e5 Nf3", "opening": {}})
    print(f"  {label:<45} -> {len(bl)} blunder(s) "
          f"{[(b['played_san'], b['eval_before'], b['cpl_loss']) for b in bl]}")

hdr("ATK3-09d — threshold setting: is blunder_threshold_cp honoured exactly?")
import backend.db as dbm  # noqa: E402
for thresh in (150, 160, 400, 100000):
    dbm.set_setting("blunder_threshold_cp", str(thresh))
    st = {"n": 0}

    def fixed400(b, st=st):
        st["n"] += 1
        return (chess.engine.Cp(0 if st["n"] % 2 else -400), pv_of(b, ["Nf6"]))

    an._engine = FakeEngine(fixed400)
    bl = an.analyze_game_openings({"id": f"t{thresh}", "user_color": "white",
                                   "moves": "e4 e5 Nf3", "opening": {}})
    print(f"  threshold={thresh:<7} (cpl=400) -> {len(bl)} blunder(s) "
          f"cpl={[b['cpl_loss'] for b in bl]}")
dbm.set_setting("blunder_threshold_cp", "160")

hdr("ATK3-09e — cpl_loss when the engine returns a MATE (mate_score=10000 sentinel)")
st = {"n": 0}


def mated(b):
    st["n"] += 1
    return (chess.engine.Mate(5) if st["n"] % 2 else chess.engine.Mate(-1),
            pv_of(b, ["Nf6"]))


an._engine = FakeEngine(mated)
bl = an.analyze_game_openings({"id": "mb", "user_color": "white",
                               "moves": "e4 e5 Nf3", "opening": {}})
for b in bl:
    print(f"  {b['played_san']}: eval_before={b['eval_before']} eval_after={b['eval_after']} "
          f"cpl_loss={b['cpl_loss']} category={b['category']}")
    print(f"    explanation: {b['explanation']}")
    print(f"    strategic_idea: {b['strategic_idea']}")
print("  -> a MATE IN 1 that the user walks into is stored as cpl_loss =",
      bl[0]["cpl_loss"] if bl else "n/a",
      "and the entire mate distance is thrown away (mate_score=10000 collapse).")
print("     The eval bar / gap table therefore shows '-9999' style sentinels, not 'mate in 1'.")

st = {"n": 0}


def lostmate(b):
    st["n"] += 1
    return (chess.engine.Mate(-1) if st["n"] % 2 else chess.engine.Cp(30),
            pv_of(b, ["Nf6"]))


an._engine = FakeEngine(lostmate)
bl = an.analyze_game_openings({"id": "lm", "user_color": "white",
                               "moves": "e4 e5 Nf3", "opening": {}})
print("  already-mated (eval_before=-9999) ->", len(bl), "blunders (correctly filtered)")

st = {"n": 0}


def rescuemate(b):
    st["n"] += 1
    return (chess.engine.Cp(-100 if st["n"] % 2 else -10000), pv_of(b, ["Nf6"]))


an._engine = FakeEngine(rescuemate)
bl = an.analyze_game_openings({"id": "rs", "user_color": "white",
                               "moves": "e4 e5 Nf3", "opening": {}})
print("  'loses a mate': eval_before=-100, eval_after=-10000 ->",
      [(b["played_san"], b["eval_before"], b["eval_after"], b["cpl_loss"]) for b in bl])
print("     -> cpl_loss is 9900 for what is a 1-move slip; it dominates the whole")
print("        'BIGGEST BLUNDER' ranking and cannot be distinguished from a real")
print("        99-pawn collapse.")

hdr("ATK3-09h — the explanation text: is 'Checkmate in 1' ever accurate?")
for after_label, after in [("plain centipawns, -95.00", chess.engine.Cp(-9500)),
                           ("mate in 1", chess.engine.Mate(-1)),
                           ("mate in 30", chess.engine.Mate(-30)),
                           ("mate in 99", chess.engine.Mate(-99))]:
    st = {"n": 0}

    def sc2(b, after=after, st=st):
        st["n"] += 1
        if st["n"] % 2 == 0:
            return (after, pv_of(b, ["Nf6"]))
        return (chess.engine.Cp(0), pv_of(b, ["Nf6"]))

    an._engine = FakeEngine(sc2)
    bl = an.analyze_game_openings({"id": "ex", "user_color": "white",
                                   "moves": "e4 e5 Nf3", "opening": {}})
    for b in bl[:1]:
        print(f"  eval_after is {after_label:<26} -> explanation: {b['explanation']!r} "
              f"| cpl={b['cpl_loss']}")

hdr("ATK3-09f — a real game fed to the analyzer: end-to-end sanity + ply accounting")
st.clear()


def real(b):
    e = chess.engine.SimpleEngine
    return (chess.engine.Cp(0), pv_of(b, ["Nf6"]))


an._engine = FakeEngine(real)
bl = an.analyze_game_openings({"id": "sanity", "user_color": "black",
                               "moves": "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7",
                               "opening": {"name": "Ruy Lopez", "eco": "C60"}})
print("  black blunders:", [(b["move_number"], b["ply"], b["played_san"], b["cpl_loss"])
                            for b in bl])
print("  engine.analyse() calls:", len(an._engine.calls), "(2 per user ply evaluated)")
print("  plies in game:", 10, "-> black plies 2,4,6,8,10 = 5 turns")

hdr("ATK3-09g — max_opening_ply: what actually limits the scan?")
import backend.db as db  # noqa: E402
db.set_setting("max_opening_ply", "4")
st2 = {"n": 0}
an._engine = FakeEngine(lambda b: (chess.engine.Cp(0), pv_of(b, ["Nf6"])))
an.analyze_game_openings({"id": "mp", "user_color": "white",
                          "moves": "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6", "opening": {}})
print("  analyse calls with max_opening_ply=4:", len(an._engine.calls))
db.set_setting("max_opening_ply", "24")
