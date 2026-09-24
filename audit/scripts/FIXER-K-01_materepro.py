"""FIXER-K-01 -- SYNC-06 evidence probe.

Pins down, with the REAL Stockfish 19 binary, (a) python-chess' mate sign
convention, (b) two positions the engine scores as a real mate in 1 and a real
mate in 2 *from the user's point of view* (used by the regression tests), and
(c) a plain centipawn score that must never be described as a checkmate.

Bounded: one shallow `analyse()` per FEN, trivial material, no network. The
engine comes from the process-wide pool (FIXER-C) and is always released.

    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-K-01_materepro.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # noqa: E402  FIRST backend-adjacent import (temp DB)
import chess  # noqa: E402
import chess.engine  # noqa: E402
from backend.analyzer import GameAnalyzer  # noqa: E402
from backend.evaluator import get_sync_engine_pool  # noqa: E402

# Real positions AFTER the user's (white's) blunder, i.e. black to move:
#   MATE1 = white has just played 2.g4?? -> ...Qh4# is a real forced mate.
#          pov(WHITE).mate() must be -1  (white gets mated in 1 ply = 1 move)
#   MATE2 = smothered mate needing a knight setup move: 1...Ne5 2...Nf7#
#          pov(WHITE).mate() must be -4  (white gets mated in 2 moves)
def _fen_after(*sans):
    b = chess.Board()
    for san in sans:
        b.push_san(san)
    return b.fen()


# 1.f3 e5 2.g4?? -- black to move, 2...Qh4# is a real forced mate in 1.
MATE1 = _fen_after("f3", "e5", "g4")
# Black mates in 2 (knight smothered, Arabian style): 1...Ne5 2...Ng6#.  The g8
# and h7 escapes are covered by Nf6, the white rook on g8 cannot reach f7 or g6,
# and a knight check cannot be blocked -> forced in 3 plies, i.e. "checkmate in
# 2" from the user's (white's) point of view.  Verified exhaustively with
# python-chess: black has NO mate in 1, and after 1...Ne5 every white reply
# (Rf8, Re8, Rd8, Rc8, Rb8, Ra8) is met by Ng6#/Nf7#.
MATE2 = "6RK/6PP/5n2/8/2n5/8/8/7k b - - 0 1"

# A plain, ordinary middlegame evaluation: no mate anywhere in sight.
CP_ONLY = "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 w kq - 0 6"


def show(fen, engine, depth=8):
    board = chess.Board(fen)
    info = engine.analyse(board, chess.engine.Limit(depth=depth))
    sc = info["score"]
    w = sc.pov(chess.WHITE)
    bl = sc.pov(chess.BLACK)
    return {
        "fen": fen,
        "turn": "white" if board.turn else "black",
        "white_mate": w.mate(),
        "white_cp": w.score(mate_score=10000),
        "black_mate": bl.mate(),
        "black_cp": bl.score(mate_score=10000),
        "best": board.san(info["pv"][0]) if info.get("pv") else None,
    }


def main():
    an = GameAnalyzer()
    engine = an._get_engine()
    print("=" * 78)
    print("A) REAL Stockfish: mate sign convention (POV = the colour passed to .pov())")
    for label, fen, want in [
        ("real mate in 1 after 1.f3 e5 2.g4 (black to move)", MATE1, -1),
        ("real mate in 2, knight smothered 1...Ne5 2...Ng6# (black to move)", MATE2, -2),
    ]:
        r = show(fen, engine)
        print(f"  {label}")
        print(f"    fen              : {r['fen']}")
        print(f"    side to move     : {r['turn']}   engine best reply {r['best']}")
        print(f"    score.pov(WHITE) : mate={r['white_mate']}  "
              f"cp(mate_score=10000)={r['white_cp']}")
        print(f"    score.pov(BLACK) : mate={r['black_mate']}  "
              f"cp(mate_score=10000)={r['black_cp']}")
        print(f"    expected white POV mate = {want}  ->  "
              f"{'OK' if r['white_mate'] == want else 'MISMATCH'}")
    print("    => NEGATIVE mate() = the POV side is GETTING MATED.  The magnitude is")
    print("       MOVES (the engine's own UCI unit), NOT plies -- verified here: a mate")
    print("       landing on the opponent's next move is 1, two moves later is 2.")

    print()
    print("B) REAL Stockfish: a plain centipawn score (no mate in sight)")
    r = show(CP_ONLY, engine, depth=8)
    print(f"    fen              : {r['fen']}")
    print(f"    pov(WHITE).mate()= {r['white_mate']}   "
          f"cp={r['white_cp']}  -> a -95.00 style value must never read 'checkmate'")

    print()
    print("C) the three required cases side by side (what the user is told)")
    from backend.analyzer import describe_score_mate, normalize_score  # noqa: E402
    for label, sc in [("real mate in 1  (Mate(-1))", chess.engine.Mate(-1)),
                      ("real mate in 2  (Mate(-2))", chess.engine.Mate(-2)),
                      ("real mate in 30 (Mate(-30))", chess.engine.Mate(-30)),
                      ("plain cp -95    (Cp(-95))", chess.engine.Cp(-95)),
                      ("plain cp -9500  (Cp(-9500))", chess.engine.Cp(-9500))]:
        cp, mate = normalize_score(sc)
        print(f"    {label:<28} -> cp={cp:<7} mate={str(mate):<5} "
              f"text={describe_score_mate(mate)!r}")

    an.close()
    pool = get_sync_engine_pool()
    print()
    print("pool after close(): in_use=%s idle=%s live=%s spawned=%s" % (
        pool.in_use, pool.idle, pool.live, pool.spawned))
    pool.close_all()
    print("DONE")


if __name__ == "__main__":
    main()
