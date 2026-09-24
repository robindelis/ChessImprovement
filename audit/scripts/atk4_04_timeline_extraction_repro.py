"""
SCOUT-04 repro: adversarial transcripts / chapter metadata through the video
move-extraction pipeline.

NOTE ON REACHABILITY: `_extract_chess_moves_and_lines` currently has **no call site**
(verified: the only occurrence of the symbol in the repo is its own `def`, plus the
definition of `normalize_chess_speech` inside it).  The probe therefore drives the
function directly so the *data* it would produce can be graded; SCOUT-04 also proves
that the public API can never produce a non-empty timeline.
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk4_mocks import banner  # noqa: E402

import chess  # noqa: E402
from backend.scout import ContentScout, normalize_chess_speech  # noqa: E402


def audit(timeline, label):
    """Grade a produced timeline: legality, timestamps, duplicates, ply sanity."""
    print(f"\n--- {label}")
    print(f"    timeline entries      : {len(timeline)}")
    if not timeline:
        return {}
    bad_legal, ts, plies = [], [], []
    for mv in timeline:
        try:
            b = chess.Board(mv["fen_before"])
            u = chess.Move.from_uci(mv["uci"])
            if u not in b.legal_moves:
                bad_legal.append(mv["san"])
        except Exception as e:
            bad_legal.append(f"{mv.get('san')}!({e})")
        ts.append(mv["timestamp"])
        plies.append(mv["ply"])
    dup_ts = len(ts) - len(set(ts))
    pairs = [(m["fen_before"], m["uci"]) for m in timeline]
    dup_moves = len(pairs) - len(set(pairs))
    non_mono = sum(1 for a, b in zip(ts, ts[1:]) if b < a)
    print(f"    illegal/invalid moves : {len(bad_legal)} {bad_legal[:6]}")
    print(f"    duplicate timestamps  : {dup_ts}   (moves sharing a video timestamp)")
    print(f"    duplicate (fen,uci)   : {dup_moves}")
    print(f"    non-monotonic ts steps: {non_mono}")
    print(f"    min/max timestamp     : {min(ts)} / {max(ts)}")
    print(f"    max ply               : {max(plies)}")
    print(f"    first 12 sans         : {[m['san'] for m in timeline[:12]]}")
    return {"n": len(timeline), "illegal": len(bad_legal), "dup_ts": dup_ts,
            "dup_moves": dup_moves, "non_mono": non_mono}


def main():
    banner("SCOUT-04  adversarial transcript / chapter metadata -> move extraction")
    s = ContentScout.get_instance()

    # 0. speech normaliser on hostile / realistic text
    print("\nnormalize_chess_speech() on realistic prose:")
    for raw in ["e four", "pawn to e four", "castling kingside", "castle short",
                "the 5 pawn is strong", "I could be 4 moves ahead", "he said bee 5",
                "knight f three takes on e five", "ｅ４", "e\u200b4", "e\u22124"]:
        print("   %-34r -> %r" % (raw, normalize_chess_speech(raw)))

    # 1. same move repeated hundreds of times
    t0 = time.time()
    tl, va = s._extract_chess_moves_and_lines(
        [{"start": 10.0, "text": "e4 e4 e4 e4 " * 200}], "", [], "Test", 600)
    a1 = audit(tl, "200x 'e4' in one snippet (timeline spam?)")
    print(f"    elapsed                : {time.time()-t0:.2f}s")

    # 2. a move named 400 times in separate snippets -> many variations?
    snips = [{"start": float(i), "text": "e4 e5 Nf3 Nc6"} for i in range(400)]
    t0 = time.time()
    tl2, va2 = s._extract_chess_moves_and_lines(snips, "", [], "Test", 600)
    a2 = audit(tl2, "400 snippets x 'e4 e5 Nf3 Nc6'")
    print(f"    variations produced   : {len(va2)}")
    print(f"    elapsed                : {time.time()-t0:.2f}s")

    # 3. two different moves in a single snippet (identical timestamps)
    tl3, va3 = s._extract_chess_moves_and_lines(
        [{"start": 33.0, "text": "e4 e5"}], "", [], "Test", 600)
    audit(tl3, "one snippet, two moves")
    print("    timestamps             :", [m["timestamp"] for m in tl3])

    # 4. hostile timestamps: negative, out of order, past the video duration
    tl4, va4 = s._extract_chess_moves_and_lines(
        [{"start": 900.0, "text": "e4 e5"},
         {"start": -50.0, "text": "Nf3"},
         {"start": 5.0, "text": "Nc6"},
         {"start": 12.0, "text": "d4"}], "", [], "Test", 120)
    a4 = audit(tl4, "negative / out-of-order / past-duration timestamps (video duration=120)")
    print("    variations             :", [(v["name"], v["start_time"], v["start_formatted"]) for v in va4])

    # 5. duplicate + missing + unordered chapters
    chs = [{"title": "A", "start_time": 50.0, "end_time": 60.0, "start_formatted": "00:50"},
           {"title": "A dup", "start_time": 50.0, "end_time": 60.0, "start_formatted": "00:50"},
           {"title": "backwards", "start_time": 5.0, "end_time": 10.0, "start_formatted": "00:05"},
           {"title": "neg", "start_time": -100.0, "end_time": -50.0, "start_formatted": "00:00"}]
    tl5, va5 = s._extract_chess_moves_and_lines(
        [{"start": 55.0, "text": "e4 e5 Nf3"}, {"start": 56.0, "text": "Nc6 d4"}],
        "", chs, "Test", 120)
    audit(tl5, "duplicate / backwards / negative chapters")
    print("    chapters ->", [(v["name"], v["start_formatted"]) for v in va5])

    # 6. unicode / homoglyph "moves" (should extract nothing, must not crash)
    # NB: the title deliberately matches no opening so the step-4 title fallback
    #     cannot manufacture a variation and mask the result.
    NOTITLE = "zzqxx-no-such-opening-zzqxx"
    for label, txt in [("fullwidth", "\uff45\uff14 \uff45\uff15"),
                       ("zero-width", "e\u200b4 e\u200b5"),
                       ("unicode minus", "e\u22124"),
                       ("cyrillic e", "\u04354"),
                       ("plain e4 e5", "e4 e5")]:
        try:
            tlx, vax = s._extract_chess_moves_and_lines([{"start": 1.0, "text": txt}], "", [], NOTITLE, 60)
            print("    %-15s -> %d moves %s  variations=%s" % (
                label, len(tlx), [m["san"] for m in tlx][:8], [v["pgn"] for v in vax]))
        except Exception as e:
            print("    %-15s -> EXCEPTION %s: %s" % (label, type(e).__name__, e))

    # 6b. the step-4 title fallback manufactures a line for an *unmatched* transcript
    tlz, vaz = s._extract_chess_moves_and_lines([{"start": 1.0, "text": "zzz"}], "", [], "Italian", 60)
    print("\n    title='Italian', transcript='zzz' (no moves said) ->",
          [v["pgn"] for v in vaz], " timeline:", [m["san"] for m in tlz])

    # 6c. ORDER BUG: the textual order of the spoken moves is ignored
    print("\n--- 6c. transcript 'e4, e5, Nf3, Nc6' (a perfectly normal sentence)")
    tlo, vao = s._extract_chess_moves_and_lines(
        [{"start": 2.0, "text": "so first e4, and then e5, then Nf3, then Nc6"}],
        "", [], NOTITLE, 60)
    print("    extracted sans        :", [m["san"] for m in tlo])
    print("    plies                 :", [m["ply"] for m in tlo])
    print("    resulting line PGN    :", [v["pgn"] for v in vao])
    b = chess.Board()
    for m in tlo:
        b.push_uci(m["uci"])
    want = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b kq - 1 3"
    print("    final FEN             :", b.fen())
    print("    expected (text order) :", want)
    print("    ORDER CORRECT?        :", b.fen() == want)

    # 6d. plain English prose is turned into board moves by normalize_chess_speech
    print("\n--- 6d. ordinary English prose -> phantom moves")
    for txt in ["I could be 4 moves ahead of him",
                "he was told to be 3 for 1 in the pawn endgame",
                "and then the day 5 pawn push comes in"]:
        n = normalize_chess_speech(txt)
        tl, va = s._extract_chess_moves_and_lines([{"start": 1.0, "text": txt}], "", [], NOTITLE, 60)
        print("    %-48r" % txt)
        print("        normalized : %r" % n)
        print("        extracted  : %s" % [m["san"] for m in tl])

    # 7. description PGN extraction (also only reachable from the dead function)
    desc = "Main line: 1. e4 e5 2. Nf3\nSecond line: 1. d4 d5 2. c4"
    print("\n_extract_lines_from_description:")
    for v in s._extract_lines_from_description(desc):
        print("   ", v["name"], "|", v["start_formatted"], "|", v["pgn"], "| moves:", len(v["moves"]))


if __name__ == "__main__":
    main()
