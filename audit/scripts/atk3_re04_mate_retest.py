"""[ATK3-RETEST] SYNC-06: the mate boundary of `normalize_score()` /
`describe_score_mate()` / `_generate_tactical_explanation()`, pushed hard.

Covered: mate in 1/2/3 from BOTH sides, the user's colour black, a plain
`Cp(-9500)` and other huge non-mate scores, the saturation point, `MateGiven`
(mate 0), and the "moves vs plies" wording correction.

    /home/robin/miniconda3/bin/python3 audit/scripts/atk3_re04_mate_retest.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from atk3_lib import hdr  # noqa: E402

import chess  # noqa: E402
import chess.engine  # noqa: E402
from backend.analyzer import (GameAnalyzer, normalize_score, describe_score_mate,  # noqa: E402
                              mate_distance_moves, centipawn_loss, MAX_CPL_LOSS)
from backend.evaluator import MATE_SCORE_CP  # noqa: E402

RESULTS = []


def verdict(name, ok, detail=""):
    RESULTS.append((name, "HOLDS" if ok else "BROKEN", detail))
    print("   -> %-6s %-52s %s" % ("HOLDS" if ok else "BROKEN", name, detail))


hdr("RE04-0  the saturation sentinel")
print("   MATE_SCORE_CP =", MATE_SCORE_CP, "  MAX_CPL_LOSS =", MAX_CPL_LOSS)

# ---------------------------------------------------------------------------
hdr("RE04-A  normalize_score(): the ONLY mate signal is the `mate` field")
# ---------------------------------------------------------------------------
# Fool's mate: 1.f3 e5 2.g4 Qh4#  -> black to move, mated in 0 (MateGiven), and
# from White's POV a forced mate in 1.
FOOLS = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
BEFORE_BLACK_MATES = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2")

ROWS = [
    ("Cp(+0) white", chess.engine.Cp(0), chess.WHITE),
    ("Cp(-9500) white  <- the SYNC-06 false positive", chess.engine.Cp(-9500), chess.WHITE),
    ("Cp(-8999)", chess.engine.Cp(-8999), chess.WHITE),
    ("Cp(-9001)", chess.engine.Cp(-9001), chess.WHITE),
    ("Cp(-100000) beyond any sentinel", chess.engine.Cp(-100000), chess.WHITE),
    ("Cp(+100000)", chess.engine.Cp(100000), chess.WHITE),
    ("Mate(1) white", chess.engine.Mate(1), chess.WHITE),
    ("Mate(2) white", chess.engine.Mate(2), chess.WHITE),
    ("Mate(3) white", chess.engine.Mate(3), chess.WHITE),
    ("Mate(50) white", chess.engine.Mate(50), chess.WHITE),
    ("Mate(-1) white (white is being mated)", chess.engine.Mate(-1), chess.WHITE),
    ("Mate(-2) white", chess.engine.Mate(-2), chess.WHITE),
    ("Mate(0) white = MateGiven / checkmated", chess.engine.Mate(0), chess.WHITE),
    ("Cp(30) seen from BLACK", chess.engine.Cp(30), chess.BLACK),
    ("Mate(1) seen from BLACK", chess.engine.Mate(1), chess.BLACK),
    ("Mate(-1) seen from BLACK", chess.engine.Mate(-1), chess.BLACK),
    ("Mate(0) seen from BLACK", chess.engine.Mate(0), chess.BLACK),
    ("Cp(-9500) seen from BLACK", chess.engine.Cp(-9500), chess.BLACK),
]
print("   %-46s %-8s %-8s %-6s %s" % ("score", "pov", "cp", "mate", "phrase"))
for label, score, pov in ROWS:
    cp, mate = normalize_score(score, pov)
    print("   %-46s %-8s %-8s %-6s %r" % (
        label, "w" if pov else "b", cp, mate, describe_score_mate(mate)))

verdict("Cp(-9500) is NOT described as a mate", describe_score_mate(
    normalize_score(chess.engine.Cp(-9500), chess.WHITE)[1]) == "",
    describe_score_mate(normalize_score(chess.engine.Cp(-9500), chess.WHITE)[1]))
verdict("Cp(-100000) is NOT described as a mate", describe_score_mate(
    normalize_score(chess.engine.Cp(-100000), chess.WHITE)[1]) == "",
    describe_score_mate(normalize_score(chess.engine.Cp(-100000), chess.WHITE)[1]))
verdict("Mate(1)/Mate(2)/Mate(3) each keep their own distance",
        [describe_score_mate(m) for _, m in
         [(0, normalize_score(chess.engine.Mate(1), chess.WHITE)[1]),
          (0, normalize_score(chess.engine.Mate(2), chess.WHITE)[1]),
          (0, normalize_score(chess.engine.Mate(3), chess.WHITE)[1])]]
        == ["reaches a forced mate in 1 for you", "reaches a forced mate in 2 for you",
            "reaches a forced mate in 3 for you"],
        str([describe_score_mate(normalize_score(chess.engine.Mate(k), chess.WHITE)[1])
             for k in (1, 2, 3)]))
verdict("Mate(0) is 'already on the board', not 'in 1'",
        describe_score_mate(0) == "checkmate is already on the board",
        describe_score_mate(0))
verdict("the phrase is asymmetric in the right direction",
        describe_score_mate(-2) == "allows checkmate in 2"
        and describe_score_mate(2) == "reaches a forced mate in 2 for you",
        "%r / %r" % (describe_score_mate(-2), describe_score_mate(2)))

# ---------------------------------------------------------------------------
hdr("RE04-B  the sign is derived from the score's OWN pov, and pov() flips it")
# ---------------------------------------------------------------------------
s = chess.engine.PovScore(chess.engine.Mate(2), chess.WHITE)
print("   python-chess %s: PovScore attrs = %s" % (
    chess.__version__, [a for a in dir(chess.engine.PovScore) if not a.startswith("_")]))
print("   PovScore(Mate(2),WHITE).white().mate()   =", s.white().mate())
print("   PovScore(Mate(2),WHITE).pov(BLACK)       =", s.pov(chess.BLACK), type(s.pov(chess.BLACK)).__name__)
print("   PovScore(Mate(2),WHITE).pov(BLACK).mate() =", s.pov(chess.BLACK).mate())
print("   PovScore(Cp(30),WHITE).pov(BLACK).score() =",
      chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE).pov(chess.BLACK).score())
verdict("pov() converts PovScore -> Mate/Cp and flips the mate sign",
        s.pov(chess.BLACK).mate() == -2
        and chess.engine.PovScore(chess.engine.Mate(2), chess.BLACK).pov(chess.WHITE).mate() == -2,
        "%s / %s" % (s.mate() if hasattr(s, "mate") else "no .mate() on PovScore",
                     s.pov(chess.BLACK).mate()))
# THE TRAP: `normalize_score(x, color=None)` does NOT call pov(), and a raw
# PovScore has neither .mate() nor .score() -> the function reports a MATE.
cp, mate = normalize_score(chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE), None)
print("   normalize_score(PovScore(Cp(+30)), color=None) -> cp=%s mate=%s %r" % (
    cp, mate, describe_score_mate(mate)))
verdict("normalize_score(x, color=None) on a raw PovScore is not a false mate", False,
        "returns (cp=%s, mate=%s) -> %r  (PovScore has neither .mate() nor .score(); "
        "the 'MateGiven' fallback swallows it).  NOT reachable from "
        "analyze_game_openings (it always passes a colour) but a live trap for any "
        "new caller." % (cp, mate, describe_score_mate(mate)))

# ---------------------------------------------------------------------------
hdr("RE04-C  the real positions: the Fool's-mate line, both colours")
# ---------------------------------------------------------------------------
b = chess.Board()
line = ["f3", "e5", "g4", "Qh4#"]
for san in line:
    b.push_san(san)
print("   final FEN:", b.fen())
print("   is checkmate:", b.is_checkmate(), " side to move:", "white" if b.turn else "black")
for pov, who in ((chess.WHITE, "white (delivering)"), (chess.BLACK, "black (mated)")):
    for score in (chess.engine.Mate(0), chess.engine.Mate(1), chess.engine.Mate(2)):
        cp, mate = normalize_score(score, pov)
        print("   pov=%-30s %-10s -> cp=%-7s mate=%-4s %r" % (
            who, score, cp, mate, describe_score_mate(mate)))

# ---------------------------------------------------------------------------
hdr("RE04-D  end-to-end: the explanation a user actually reads")
# ---------------------------------------------------------------------------
a = GameAnalyzer.__new__(GameAnalyzer)
opener = chess.Board()
cases = [
    ("white walks into mate in 2", 30, -2, 400),
    ("white blunders into mate in 1", 30, -1, 300),
    ("white throws away a mate in 3", 3, 3, 200),
    ("plain -95.00, no mate at all", 30, None, 12500),
    ("plain -250 cp, no mate", 20, None, 270),
    ("white had mate 1, now +30", 1, None, 150),
]
print("   %-34s %-6s %-8s %-8s %s" % ("case", "cpl", "mate_aft", "mate_bef", "explanation"))
for label, cp_before, mate_after, cpl in cases:
    mate_before = 2 if "throws away a mate" in label else (
        1 if "had mate 1" in label else None)
    cp_after = -MATE_SCORE_CP if mate_after is not None else (
        -9500 if "no mate at all" in label else -240)
    why, idea, cat = a._generate_tactical_explanation(
        opener, chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4"),
        [], cpl, "Italian Game", score_after=cp_after,
        mate_after=mate_after, mate_before=mate_before)
    print("   %-34s %-6s %-8s %-8s %r" % (label, cpl, mate_after, mate_before, why))
    print("   %-34s category=%r  idea=%r" % ("", cat, idea))

verdict("a -95.00 evaluation is never called a checkmate",
        not any("heckmate" in a._generate_tactical_explanation(
            opener, chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4"), [], 12500,
            "X", score_after=-9500, mate_after=None, mate_before=None)[0]
            for _ in (0,)),
        a._generate_tactical_explanation(
            opener, chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4"), [], 12500,
            "X", score_after=-9500, mate_after=None, mate_before=None)[0])
phr = a._generate_tactical_explanation(
    opener, chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4"), [], 400, "X",
    score_after=-MATE_SCORE_CP, mate_after=-2, mate_before=None)[0]
verdict("mate-after wording says 'allows checkmate in 2'", "allows checkmate in 2" in phr, phr)
phr2 = a._generate_tactical_explanation(
    opener, chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4"), [], 200, "X",
    score_after=0, mate_after=None, mate_before=3)[0]
verdict("throwing away a mate 3 says 'forced mate in 3 for you before'",
        "forced mate in 3 for you before" in phr2, phr2)

# ---------------------------------------------------------------------------
hdr("RE04-E  cpl_loss arithmetic with the sentinel")
# ---------------------------------------------------------------------------
for before, after in ((30, -MATE_SCORE_CP), (-MATE_SCORE_CP, MATE_SCORE_CP),
                      (MATE_SCORE_CP, -MATE_SCORE_CP), (30, 100), (100, 30),
                      (-MATE_SCORE_CP, -MATE_SCORE_CP)):
    cpl = centipawn_loss(before, after)
    print("   cpl_loss(%-7s, %-7s) = %-7s (clamped to MAX_CPL_LOSS=%s)" % (
        before, after, cpl, MAX_CPL_LOSS))
    assert cpl <= MAX_CPL_LOSS and cpl >= 0, "cpl_loss out of range"
verdict("cpl_loss always lands in [0, MAX_CPL_LOSS]",
        all(0 <= centipawn_loss(x, y) <= MAX_CPL_LOSS
            for x, y in ((30, -MATE_SCORE_CP), (-MATE_SCORE_CP, MATE_SCORE_CP),
                         (MATE_SCORE_CP, -MATE_SCORE_CP), (30, 100))),
        "so a mate can no longer own the top of ORDER BY cpl_loss DESC forever")

# ---------------------------------------------------------------------------
hdr("RE04-F  the 'moves not plies' correction, on the boundary")
# ---------------------------------------------------------------------------
for k in (-1, 0, 1, 2, 3, 99):
    print("   mate=%-4s -> %-40r (moves=%s)" % (
        k, describe_score_mate(k), mate_distance_moves(k)))
# In chess terms a mate that lands on the opponent's NEXT move is "mate in 1".
# python-chess reports that as mate 0 from the LOSING side / mate 1 from the
# WINNING side, so the winning-side wording must read 1, not 0.
verdict("a mate landing on the opponent's next move reads as 'mate in 1'",
        "mate in 1" in describe_score_mate(1),
        describe_score_mate(1))
verdict("'already on the board' is reserved for mate 0",
        describe_score_mate(0) == "checkmate is already on the board"
        and "already" not in describe_score_mate(1), describe_score_mate(1))

# ---------------------------------------------------------------------------
hdr("RE04-G  hunt: any wording that is still WRONG")
# ---------------------------------------------------------------------------
hunts = [
    ("negative centipawn that is not a mate", normalize_score(chess.engine.Cp(-200), chess.WHITE),
     "an ordinary -2.00 must not be a mate"),
    ("Mate(-0)", normalize_score(chess.engine.Mate(0), chess.WHITE),
     "int(-0) is 0 -> 'already on the board'"),
    ("Mate(+1) from the MATED side",
     normalize_score(chess.engine.Mate(1), chess.BLACK),
     "the same engine score read from the other side must read 'allows'"),
    ("no colour at all (color=None)",
     normalize_score(chess.engine.Mate(2), None),
     "color=None skips pov(); the sign is then whatever the score carries"),
    ("no colour, Mate(-2)",
     normalize_score(chess.engine.Mate(-2), None),
     "same"),
    ("no colour, Cp(-9500)",
     normalize_score(chess.engine.Cp(-9500), None),
     "same"),
]
for label, (cp, mate), note in hunts:
    print("   %-36s cp=%-8s mate=%-6s phrase=%-40r" % (label, cp, mate, describe_score_mate(mate)))

# What actually reaches _generate_tactical_explanation from analyze_game_openings?
# mate_after  = normalize_score(info_after.score, not board.turn)  <- user POV
# mate_before = normalize_score(info_before.score,  board.turn)    <- user POV
print("\n   in analyze_game_openings both mates are already in the USER's pov, so")
print("   mate_after < 0 means 'you are getting mated' -> the only reachable phrasing")
print("   of a lost mate is 'allows checkmate in N'.  Check the real call sites:")
import inspect  # noqa: E402
import backend.analyzer as _an  # noqa: E402
src = inspect.getsource(_an.GameAnalyzer.analyze_game_openings)
for ln in src.splitlines():
    if "normalize_score" in ln or "cpl_loss = " in ln or "score_before <" in ln:
        print("     ", ln.strip())

# A lost mate with cpl_loss BELOW the threshold is never recorded at all, so the
# explanation text for it is unreachable.  Quantify.
print("\n   a move that loses a forced mate: cp_before=+30, mate_after=-2 -> stored cp="
      f"{centipawn_loss(30, -MATE_SCORE_CP)} (>= any threshold -> recorded)")

# ---------------------------------------------------------------------------
hdr("SUMMARY")
# ---------------------------------------------------------------------------
for name, status, detail in RESULTS:
    print("  %-8s %-52s %s" % (status, name, detail))
print("\n%d/%d hold" % (sum(1 for r in RESULTS if r[1] == "HOLDS"), len(RESULTS)))
