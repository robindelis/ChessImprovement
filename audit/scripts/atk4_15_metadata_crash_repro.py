"""SCOUT-15 repro: hostile yt-dlp metadata (chapters / duration / title types)
crashes or corrupts the Scout studio.  yt_dlp is faked; no network.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk4_mocks import banner, install_fake_ytdlp, uninstall_fake_ytdlp  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402

BASE = {
    "id": "dP4qtaUAhWM",
    "title": "Sicilian Defense Najdorf Guide",
    "uploader": "Chess Channel",
    "duration": 1830,
    "description": "",
    "chapters": [],
}

CASES = {
    "chapter start_time = None": {"chapters": [{"title": "a", "start_time": None, "end_time": 10}]},
    "chapter end_time = None": {"chapters": [{"title": "a", "start_time": 0, "end_time": None}]},
    "chapter missing start_time key": {"chapters": [{"title": "a", "end_time": 10}]},
    "chapter start_time = string": {"chapters": [{"title": "a", "start_time": "3:45", "end_time": 10}]},
    "chapter start_time = bool": {"chapters": [{"title": "a", "start_time": True, "end_time": 10}]},
    "chapter start_time = inf": {"chapters": [{"title": "a", "start_time": float("inf"), "end_time": 10}]},
    "chapter start_time = nan": {"chapters": [{"title": "a", "start_time": float("nan"), "end_time": 10}]},
    "chapters is a string": {"chapters": "not-a-list"},
    "chapters is a dict": {"chapters": {"a": 1}},
    "chapter is a string": {"chapters": ["oops"]},
    "5000 chapters": {"chapters": [{"title": "c%d" % i, "start_time": i, "end_time": i + 1}
                                    for i in range(5000)]},
    "duration is a float string": {"duration": "600.5"},
    "duration is a list": {"duration": [1, 2]},
    "duration is 10**12": {"duration": 10 ** 12},
    "title is None": {"title": None},
    "title is a list": {"title": ["a", "b"]},
    "uploader is a dict": {"uploader": {"name": "x"}},
}


def main():
    banner("SCOUT-15  hostile yt-dlp metadata -> /api/scout/analyze")
    client = TestClient(app, raise_server_exceptions=False)
    for label, patch in CASES.items():
        payload = dict(BASE)
        payload.update(patch)
        real = install_fake_ytdlp(video_payload=lambda u, p=payload: dict(p))
        try:
            vid = ("zZprobe%04d" % (abs(hash(label)) % 9000))[:11]
            assert len(vid) == 11, vid
            r = client.post("/api/scout/analyze",
                            json={"source": "https://www.youtube.com/watch?v=" + vid,
                                  "force_refresh": True})
            if r.status_code == 200:
                d = r.json()
                ch = d.get("chapters") or []
                print("  %-32s HTTP200  chapters=%d dur=%r fmt=%r first_start=%r"
                      % (label, len(ch), d.get("duration"), d.get("duration_formatted"),
                         ch[0].get("start_formatted") if ch else None))
                if ch and len(ch) < 4:
                    print("       chapters:", json.dumps(ch)[:220])
            else:
                print("  %-32s HTTP%s  %s" % (label, r.status_code,
                                             str(r.json().get("detail"))[:90]))
        except Exception as e:
            print("  %-32s *** %s: %s" % (label, type(e).__name__, e))
        finally:
            uninstall_fake_ytdlp(real)


if __name__ == "__main__":
    main()
