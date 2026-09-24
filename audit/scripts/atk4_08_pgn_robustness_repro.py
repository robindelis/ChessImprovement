"""SCOUT-08 repro: malformed / hostile PGN blobs + unrecognised input through
POST /api/scout/analyze.

yt_dlp is FAKED for the whole run (a fake search entry is tagged
'ZZMARKERZZ' so the silent YouTube-search fallback is visible instead of a real
crawl).  Every case runs under a hard 10s alarm so an infinite loop shows up as
'TIMEOUT/INFINITE LOOP' rather than hanging.
"""
import os
import sys
import signal
import time
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk4_mocks import banner, install_fake_ytdlp, uninstall_fake_ytdlp  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402
from backend.scout import ContentScout  # noqa: E402


class Timeout(Exception):
    pass


signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(Timeout()))

FAKE_SEARCH = {"entries": [{"id": "ZZMARKERZZ", "title": "ZZMARKERZZ fallback hit",
                            "uploader": "zz", "duration": 42}]}
FAKE_VIDEO = {"id": "ZZMARKERZZ", "title": "ZZMARKERZZ fallback video",
              "uploader": "zz", "duration": 42, "description": "", "chapters": []}

CASES = {
    "empty string": "",
    "headers only": '[Event "x"]\n[Site "?"]\n\n',
    "no headers, moves": "1. e4 e5 2. Nf3 *\n",
    "unterminated brace comment": '[Event "x"]\n\n{ this comment never closes\n1. e4 e5 *\n',
    "unterminated quote header": '[Event "abc]\n\n1. e4 e5 *\n',
    "stray closing brace": '[Event "x"]\n\n1. e4 e5 }}}\n',
    "null bytes": '[Event "a\x00b"]\n\n1. e4\x00 e5 *\n',
    "illegal move sequence": "1. e4 e5 2. Nf3 Nc6 3. Bb5 Qh4 4. Nxh4?? *\n",
    "garbage tokens": "@@@ %%% ^^^ 1. e4 e5 2. ??? Nf3 *\n",
    "not pgn at all": "hello world, this is prose, not a pgn file at all\n",
    "json blob": json.dumps({"moves": ["e4", "e5"]}),
    "html": "<html><body><script>alert(1)</script></body></html>\n1. e4 e5 *\n",
    "5000 headerless games": "1. e4 e5 *\n" * 5000,
    "3 headerless games": "1. e4 e5 *\n1. d4 d5 *\n1. c4 e5 *\n",
    "3 games with headers": '[Event "G1"]\n\n1. e4 e5 *\n\n[Event "G2"]\n\n1. d4 d5 *\n\n[Event "G3"]\n\n1. c4 e5 *\n',
    "moves without numbers": " ".join(["e4", "e5", "Nf3", "Nc6"] * 20) + " *\n",
    "1000 plies numbered": " ".join(f"{i//2+1}." + (" e4" if i % 2 == 0 else " e5") for i in range(1000)) + " *\n",
    "setup fen midgame": '[SetUp "1"]\n[FEN "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 5 4"]\n\n4. O-O *\n',
    "8 queens": '[FEN "qqqqkqqq/qqqqqqqq/8/8/8/8/QQQQQQQQ/QQQQKQQQ w - - 0 1"]\n\n1. Qa4 *\n',
    "huge single game": "1. e4 " + "e5 " * 20000 + "*\n",
    "repeated headers only": '[Event "a"]\n' * 20000,
    "nul-heavy": "\x00" * 100000,
    "very long single token": "1. " + "z" * 200000 + " *\n",
    "xss in Event header": '[Event "<img src=x onerror=alert(1)>"]\n\n1. e4 e5 *\n',
}


def main():
    banner("SCOUT-08  malformed / hostile PGN + silent YouTube-search fallback")
    real = install_fake_ytdlp(video_payload=lambda u: dict(FAKE_VIDEO),
                              search_payload=lambda u: FAKE_SEARCH)
    client = TestClient(app, raise_server_exceptions=False)
    s = ContentScout.get_instance()
    try:
        for label, pgn in CASES.items():
            size = len(pgn)
            t0 = time.time()
            signal.alarm(10)
            try:
                r = client.post("/api/scout/analyze", json={"source": pgn[:400000]})
                dt = time.time() - t0
                signal.alarm(0)
                if r.status_code == 200:
                    d = r.json()
                    marker = " <<< FELL THROUGH TO YOUTUBE SEARCH" if d.get("source_type") == "youtube" else ""
                    print("  %-24s %7dB %6.2fs HTTP200 vars=%d tl=%d title=%r%s"
                          % (label, size, dt, len(d.get("variations", [])), len(d.get("full_timeline", [])),
                             (d.get("title") or "")[:34], marker))
                else:
                    print("  %-24s %7dB %6.2fs HTTP%s %s"
                          % (label, size, dt, r.status_code, str(r.json().get("detail"))[:60]))
            except Timeout:
                signal.alarm(0)
                print("  %-24s %7dB   >10s   *** INFINITE LOOP / HANG ***" % (label, size))
            except Exception as e:
                signal.alarm(0)
                print("  %-24s %7dB %6.2fs *** %s: %s" % (label, size, time.time() - t0, type(e).__name__, e))

        print("\n--- variation naming (scout.py:661 operator precedence)")
        for label, pgn in [("no headers", "1. e4 e5 *"),
                           ("White only", '[White "Alice"]\n\n1. e4 e5 *'),
                           ("Event empty", '[Event ""]\n\n1. e4 e5 *')]:
            d = s.analyze_source(pgn)
            print("    %-12s -> title=%r  variation names=%r" % (label, d["title"], [v["name"] for v in d["variations"]]))

        print("\n--- multi-game blob: variations vs full_timeline")
        blob = '[Event "G1"]\n\n1. e4 e5 *\n\n[Event "G2"]\n\n1. d4 d5 *\n\n[Event "G3"]\n\n1. c4 e5 *\n'
        d = s.analyze_source(blob)
        print("    variations:", [(v["line_id"], v["name"], v["pgn"]) for v in d["variations"]])
        print("    full_timeline (only game 1):", [m["san"] for m in d["full_timeline"]])
        print("    chapters:", [(c["title"], c["start_time"], c["end_time"]) for c in d["chapters"]])

        d2 = s.analyze_source("1. e4 e5 *\n1. d4 d5 *\n1. c4 e5 *\n")
        print("    headerless 3-game blob -> variations:", [(v["name"], v["pgn"]) for v in d2["variations"]])

        print("\n--- xss payload persisted?")
        d3 = s.analyze_source('[Event "<img src=x onerror=alert(1)>"]\n\n1. e4 e5 *\n')
        from backend.db import get_db
        conn = get_db()
        row = conn.execute("SELECT source_id, title FROM scouted_content ORDER BY scouted_at DESC LIMIT 1").fetchone()
        conn.close()
        print("    returned title :", repr(d3["title"]))
        print("    scouted_content:", dict(row))
    finally:
        uninstall_fake_ytdlp(real)


if __name__ == "__main__":
    main()
