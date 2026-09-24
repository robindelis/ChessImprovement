"""SCOUT-07 repro: ECO / opening identification in backend/openings.py + scout.py.

Shows (a) the whole video-analysis ECO label being hijacked by a naive all-words
substring match, (b) find_opening_by_name substring/prefix confusion,
(c) search_openings word-vs-ECO-code cross matching, (d) robustness of
get_opening_for_fen against hostile FENs.  yt_dlp faked, no network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk4_mocks import banner, install_fake_ytdlp, uninstall_fake_ytdlp  # noqa: E402

import chess  # noqa: E402
from backend.openings import OpeningBook  # noqa: E402
from backend.scout import ContentScout  # noqa: E402
from backend.db import get_setting  # noqa: E402

FAKE = {
    "id": "dP4qtaUAhWM",
    "title": "Sicilian Defense Najdorf Guide",
    "uploader": "Chess Channel",
    "duration": 1830,
    "description": "",
    "chapters": [{"title": "Najdorf introduction", "start_time": 0, "end_time": 120},
                 {"title": "Poisoned pawn", "start_time": 120, "end_time": 600}],
}


def main():
    banner("SCOUT-07  ECO identification / openings.py matching")
    ob = OpeningBook.get_instance()
    print("encyclopedia entries:", len(ob.openings_list), " distinct indexed FENs:", len(ob.fen_to_opening))

    print("\n(a) a Sicilian Najdorf video is labelled A45 Trompowsky:")
    real = install_fake_ytdlp(video_payload=lambda u: dict(FAKE))
    try:
        s = ContentScout.get_instance()
        st = s.analyze_source("https://www.youtube.com/watch?v=dP4qtaUAhWM")
        print("    video title :", FAKE["title"])
        print("    chapter[1]  :", FAKE["chapters"][1]["title"])
        print("    -> eco      :", st["eco"])
        print("    -> name     :", st["opening_name"])
        print("    -> advice   :", st["strategic_ideas"]["core_idea"][:90], "...")
        print("    -> setting  :", get_setting("recent_learned_opening"))
    finally:
        uninstall_fake_ytdlp(real)

    print("\n(b) find_opening_by_name() substring / prefix behaviour:")
    for q in ["Sicilian Defense", "Sicilian", "Sicilian Defense, Dragon", "dragon",
              "sicilian defense", "  SICILIAN  ", "French", "e4", "zzz-nope"]:
        m = ob.find_opening_by_name(q)
        print("    %-30r -> %s" % (q, (m["eco"], m["name"]) if m else None))

    print("\n(c) search_openings(): every word may match the ECO code OR the name:")
    for q in ["c", "b9", "the", "guide", "poisoned pawn", "Sicilian Najdorf", ""]:
        res = ob.search_openings(q, limit=3)
        print("    %-18r -> %s" % (q, [(r["eco"], r["name"][:38]) for r in res]))

    print("\n(d) identify_game_opening() on hostile / truncated move lists:")
    for moves in [["e4", "e5", "Nf3", "Nc6"], ["e4", "e5", "e4"], ["zzzz"],
                  ["e2e4", "e7e5"], [], [None, 42, "e4"], ["e4", "e5"] * 300]:
        try:
            d = ob.identify_game_opening(moves)
            print("    %-28s -> %s ply=%s" % (str(moves)[:28], d["name"][:34], d["ply"]))
        except Exception as e:
            print("    %-28s -> EXCEPTION %s: %s" % (str(moves)[:28], type(e).__name__, e))

    print("\n(e) get_opening_for_fen() on hostile FENs:")
    for fen in ["", "garbage", "8/8/8/8/8/8/8/8 w - -", "x" * 5000,
                "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq", "None", None,
                "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1 extra junk"]:
        try:
            r = ob.get_opening_for_fen(fen)
            print("    %-40r -> %s" % ((fen if fen is None else fen[:40]), (r or {}).get("name")))
        except Exception as e:
            print("    %-40r -> EXCEPTION %s: %s" % ((fen if fen is None else fen[:40]), type(e).__name__, e))

    print("\n(f) get_strategic_ideas() substring theme matching:")
    for name in ["Sicilian Najdorf", "Anti-French", "Semi-Slav", "Queen's Gambit Declined",
                 "SICILIAN", "frenchy stuff", "King's Indian Defense"]:
        t = ob.get_strategic_ideas("X00", name)
        print("    %-26r -> theme %r" % (name, t["name"]))


if __name__ == "__main__":
    main()
