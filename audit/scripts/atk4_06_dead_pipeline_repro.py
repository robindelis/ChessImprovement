"""SCOUT-06 repro: the YouTube analysis path can never produce a timeline, and the
empty result is cached forever.  yt_dlp is faked; no network is touched.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk4_mocks import banner, install_fake_ytdlp, uninstall_fake_ytdlp  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402
import backend.scout as scout_mod  # noqa: E402

FAKE = {
    "id": "dP4qtaUAhWM",
    "title": "Sicilian Defense Najdorf Guide",
    "uploader": "Chess Channel",
    "duration": 1830,
    "description": "1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6",
    "chapters": [{"title": "Najdorf introduction", "start_time": 0, "end_time": 120},
                 {"title": "Poisoned pawn", "start_time": 120, "end_time": 600}],
}

TEXTBOOK_PGN = "1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6 *"


def main():
    banner("SCOUT-06  YouTube analysis never extracts moves; empty result cached permanently")
    real = install_fake_ytdlp(video_payload=lambda u: dict(FAKE))
    client = TestClient(app, raise_server_exceptions=False)
    try:
        r = client.post("/api/scout/analyze",
                        json={"source": "https://www.youtube.com/watch?v=dP4qtaUAhWM"})
        st = r.json()
        print("analyze #1 ->", r.status_code)
        print("   variations       :", st["variations"])
        print("   full_timeline    :", st["full_timeline"])
        print("   transcript_snips :", st["transcript_snippets"])
        print("   chapters         :", len(st["chapters"]), "->", [c["title"] for c in st["chapters"]])
        print("   eco/opening      :", st["eco"], "/", st["opening_name"])
        print("   NOTE: the video description contains a full PGN line, it is parsed into `description`")
        print("         at scout.py:212 and then never used anywhere.")

        r2 = client.post("/api/scout/analyze",
                         json={"source": "https://www.youtube.com/watch?v=dP4qtaUAhWM"})
        st2 = r2.json()
        print("\nanalyze #2 (same id, force_refresh NOT set) ->", r2.status_code)
        print("   variations       :", st2["variations"], " (served from cache)")
        r3 = client.post("/api/scout/analyze",
                         json={"source": "https://www.youtube.com/watch?v=dP4qtaUAhWM",
                               "force_refresh": True})
        print("analyze #3 (force_refresh=True) -> variations:", r3.json()["variations"])

        # The frontend NEVER sends force_refresh:
        import re
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "..", "frontend", "scout.js")).read()
        body = re.search(r"body: JSON.stringify\(\{ source: source \}\)", src)
        print("\nfrontend analyzeSource() sends force_refresh? ",
              bool(body), "->", body.group(0) if body else None)
    finally:
        uninstall_fake_ytdlp(real)

    print("\ncall sites of the extractor inside the whole repo:")
    import subprocess
    out = subprocess.run(["grep", "-rn", "_extract_chess_moves_and_lines", "--include=*.py",
                          "--include=*.js", "/home/robin/Projects/ChessImprovement"],
                         capture_output=True, text=True).stdout
    print(out)
    print("call sites of the transcript API:")
    out2 = subprocess.run(["grep", "-rn", "YouTubeTranscriptApi", "--include=*.py",
                           "/home/robin/Projects/ChessImprovement"],
                          capture_output=True, text=True).stdout
    print(out2)
    print("direct extraction call (for contrast) returns:",
          len(scout_mod.ContentScout.get_instance().analyze_source(TEXTBOOK_PGN)["full_timeline"]), "moves")


if __name__ == "__main__":
    main()
