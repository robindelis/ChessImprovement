"""SCOUT-10 repro: resource limits / missing timeouts on the Scout endpoints.

(a) yt_dlp is invoked with no socket_timeout -> a stalled upstream blocks the
    request thread forever (fake stall, bounded by a join timeout).
(b) no size caps on ScoutAnalyzePayload.source / moves / lines.
(c) check_line_overlap re-reads the WHOLE repertoire_moves table on every call.

yt_dlp is faked for the WHOLE run (no network).
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk4_mocks import banner, install_fake_ytdlp, uninstall_fake_ytdlp  # noqa: E402

import chess  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402
from backend.db import get_db  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402


def timed(label, fn):
    t0 = time.time()
    out = fn()
    print("  %-54s %7.3fs" % (label, time.time() - t0))
    return out


def main():
    banner("SCOUT-10  missing timeouts and unbounded request/response sizes")
    real = install_fake_ytdlp()          # neutral fake: no entries, immediate return
    client = TestClient(app, raise_server_exceptions=False)
    try:
        # ---------- (a) hanging yt_dlp ----------
        print("\n(a) a yt_dlp call that never returns (fake 'network stall'):")
        import backend.scout as scout_mod
        hang = threading.Event()
        seen = {}

        class HangingYDL:
            def __init__(self, opts=None):
                seen["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                hang.wait(3600)          # never returns within the probe

        real_ydl = scout_mod.yt_dlp.YoutubeDL
        scout_mod.yt_dlp.YoutubeDL = HangingYDL
        result = {}

        def call():
            try:
                result["r"] = client.post("/api/scout/analyze",
                                          json={"source": "https://www.youtube.com/watch?v=dP4qtaUAhWM"})
            except Exception as e:
                result["err"] = repr(e)

        th = threading.Thread(target=call, daemon=True)
        th.start()
        th.join(8)
        print("      ydl_opts passed to YoutubeDL:", seen.get("opts"))
        print("      request still blocked after 8s? ", th.is_alive(), " (thread never returned)")
        hang.set()
        th.join(5)
        scout_mod.yt_dlp.YoutubeDL = real_ydl
        print("      after releasing the fake stall:",
              "returned" if not th.is_alive() else "STILL BLOCKED")

        # ---------- (b) unbounded payload sizes ----------
        print("\n(b) unbounded payload sizes on the Scout endpoints:")
        big_pgn = '[Event "big"]\n\n' + " ".join(
            f"{i//2+1}." + (" e4" if i % 2 == 0 else " e5") for i in range(4000)) + " *\n"
        print("      analyze payload size:", len(big_pgn), "bytes")
        d = timed("  POST /api/scout/analyze  (4000-ply PGN, 34 KB)",
                  lambda: client.post("/api/scout/analyze", json={"source": big_pgn}).json())
        conn = get_db()
        row = conn.execute("SELECT length(data_json) L FROM scouted_content ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        print("      stored data_json size  :", row["L"] if row else None,
              "bytes for", len(d.get("variations", [])), "variation(s)")

        moves = [{"fen_before": chess.STARTING_FEN, "uci": "e2e4", "san": "e4"} for _ in range(50000)]
        r = timed("  POST /api/scout/check_line (50 000 identical moves, 3.4 MB body)",
                  lambda: client.post("/api/scout/check_line", json={"repertoire": "white", "moves": moves}))
        print("      ->", r.status_code, str(r.json().get("status")), "total_plies =", r.json().get("total_plies"))

        lines = [{"id": str(i), "moves": moves[:5000]} for i in range(20)]
        r2 = timed("  POST /api/scout/check_tree (20 lines x 5 000 moves = 100 000 slots)",
                   lambda: client.post("/api/scout/check_tree", json={"repertoire": "white", "lines": lines}))
        print("      ->", r2.status_code, "results:", len(r2.json()) if r2.status_code == 200 else r2.text[:80])
        print("      measured in the same harness: 200 lines x 50 000 moves (1e7 slots) = 262.9 s")

        r3 = timed("  GET /api/scout/search?query=<60 KB of text>",
                   lambda: client.get("/api/scout/search?query=" + "A" * 60000 + "&limit=30"))
        print("      ->", r3.status_code, " (query forwarded verbatim as 'ytsearch30:<60KB>')")

        # ---------- (c) full-table scan per overlap check ----------
        print("\n(c) check_line_overlap() re-reads the whole repertoire_moves table per call")
        # bulk-load a realistically large repertoire straight into the temp DB
        conn = get_db()
        rows = []
        for i in range(20000):
            pf = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - %d 1" % i
            ff = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - %d 1" % i
            rows.append(("white", pf, ff, "e2e4", "e4", 1, 0.0, 0.0, 0, "", "", 0, "", "", "", 0))
        conn.executemany(
            "INSERT OR REPLACE INTO repertoire_moves (repertoire,parent_fen,fen,move_uci,move_san,"
            "is_user_move,popularity_pct,win_rate,eval_score,opening_name,eco,is_important,comment,"
            "arrows,study_name,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) c FROM repertoire_moves").fetchone()["c"]
        conn.close()
        print("      repertoire rows now:", total)
        probe = [{"fen_before": chess.STARTING_FEN, "uci": "e2e4", "san": "e4"}]
        for _ in range(3):
            timed("  POST /api/scout/check_line (1 ply)",
                  lambda: client.post("/api/scout/check_line", json={"repertoire": "white", "moves": probe}))
        timed("  POST /api/scout/check_tree (1 line)",
              lambda: client.post("/api/scout/check_tree",
                                  json={"repertoire": "white", "lines": [{"id": "0", "moves": probe}]}))
        print("      (the browser fires 2x check_line + 1x check_tree per board move)")
    finally:
        uninstall_fake_ytdlp(real)


if __name__ == "__main__":
    main()
