"""SCOUT-14 repro: hostile strings through the scouted_content cache + the
ep-blind 3-token FEN index in check_line_overlap.

No network: yt_dlp is faked.  Production DB untouched (temp DB only).
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk4_mocks import banner, install_fake_ytdlp, uninstall_fake_ytdlp  # noqa: E402

import chess  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402
from backend.db import get_db  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402
from backend.scout import ContentScout  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
scout = ContentScout.get_instance()


def sec(t):
    print("\n" + "-" * 74)
    print(t)
    print("-" * 74)


def main():
    banner("SCOUT-14  hostile cache keys / SQLi / prototype pollution / ep-blind index")
    real = install_fake_ytdlp()
    try:
        # ---------- SQL injection through source_id ----------
        sec("1. SQL metacharacters in source_id (favorite / recent / analyze)")
        for sid in ["'; DROP TABLE scouted_content; --",
                    "' OR '1'='1",
                    "x' UNION SELECT data_json,1,2,3,4,5,6,7,8,9,10 FROM scouted_content--",
                    "../../../../etc/passwd",
                    "a" * 5000]:
            r = client.post("/api/scout/favorite", json={"source_id": sid})
            print("   favorite(%-52r) -> HTTP %s %s" % (sid[:52], r.status_code, r.json()))
        conn = get_db()
        n = conn.execute("SELECT COUNT(*) c FROM scouted_content").fetchone()["c"]
        conn.close()
        print("   scouted_content still has", n, "rows -> no injection, no table loss")

        # a PGN whose md5-derived source_id collides with an existing youtube id?
        r = client.post("/api/scout/analyze",
                        json={"source": '[Event "x"]\n\n1. e4 e5 *\n', "force_refresh": True})
        sid = r.json()["video_id"]
        print("\n   PGN analysed under source_id", repr(sid), "(type", r.json()["source_type"] + ")")
        r2 = client.post("/api/scout/analyze", json={"source": "eco_C50_Italian_Game"})
        print("   ECO guide source_id:", repr(r2.json()["video_id"]), r2.json()["source_type"])
        r3 = client.post("/api/scout/analyze",
                         json={"source": "https://lichess.org/study/AbCd1234", "force_refresh": True})
        print("   lichess study (network mocked out) ->", r3.status_code, str(r3.json())[:70])

        # ---------- prototype pollution attempts ----------
        sec("2. prototype-pollution / odd-type payloads")
        payloads = [
            {"repertoire": "white", "moves": [{"fen_before": chess.STARTING_FEN, "uci": "e2e4",
                                               "__proto__": {"polluted": True},
                                               "constructor": {"prototype": {"x": 1}}}]},
            {"repertoire": "white", "moves": [{"fen_before": chess.STARTING_FEN, "uci": "e2e4",
                                               "arrows": '{"__proto__":{"evil":1}}'}]},
            {"repertoire": "white", "moves": [{"fen_before": chess.STARTING_FEN, "uci": "e2e4",
                                               "is_important": "yes", "comment": 12345,
                                               "arrows": ["not", "a", "string"]}]},
            {"repertoire": "white", "moves": [{"fen_before": chess.STARTING_FEN, "uci": "e2e4",
                                               "comment": "x" * 200000}]},
        ]
        for p in payloads:
            r = client.post("/api/scout/import_repertoire", json=p)
            j = r.json() if r.status_code == 200 else r.json()
            print("   ->", r.status_code, json.dumps(j)[:150] if isinstance(j, dict) else str(j)[:150])
        print("   python dict after the __proto__ payload: polluted =", {}.get("polluted"))
        conn = get_db()
        row = conn.execute("SELECT is_important, comment, arrows FROM repertoire_moves "
                           "WHERE move_uci='e2e4' AND parent_fen=?", (chess.STARTING_FEN,)).fetchone()
        conn.close()
        print("   stored row:", dict(row) if row else None)

        # ---------- ep-blind 3-token index ----------
        sec("3. check_line_overlap's 3-token FEN index ignores the en-passant square")
        # P2 (ep=d6) and P1 (ep=-) are the same position except for the ep field
        b2 = chess.Board()
        for san in ["e4", "a6", "e5", "d5"]:
            b2.push_san(san)
        P2 = b2.fen()                      # ...d5 double push -> ep d6
        b1 = chess.Board()
        for san in ["e4", "a6", "e5", "d6", "Nf3", "Nf6", "Ng1", "d5"]:
            b1.push_san(san)
        P1 = b1.fen()                      # ...d6-d5 single push -> ep -
        k2 = " ".join(P2.split()[:3])
        k1 = " ".join(P1.split()[:3])
        print("   P2:", P2)
        print("   P1:", P1)
        print("   3-token keys equal? ", k1 == k2, " (ep is the 4th token, dropped by the index)")
        line = scout.analyze_source("1. e4 a6 2.e5 d6 3. Nf3 Nf6 4. Ng1 d5 5. d4 Nf6 *")["full_timeline"]
        client.post("/api/scout/import_repertoire", json={"repertoire": "white", "moves": line})
        conn = get_db()
        rows = conn.execute("SELECT move_uci, parent_fen, fen FROM repertoire_moves "
                            "WHERE repertoire='white'").fetchall()
        conn.close()
        print("   repertoire parent_fens include P1? ", any(r["parent_fen"] == " ".join(P1.split()[:4]) for r in rows))
        # now ask about a line whose parent is P2 with a move the repertoire only has after P1
        fake = [{"fen_before": P2, "uci": "g1f3", "san": "Nf3"}]
        ov = client.post("/api/scout/check_line", json={"repertoire": "white", "moves": fake}).json()
        print("   check_line with parent=P2 (ep=d6) uci=g1f3 ->")
        print("      ", json.dumps({k: ov[k] for k in
              ("status", "total_plies", "matched_plies", "new_plies", "message")}, ensure_ascii=False))
        print("   NOTE: the true position after P1 is a *different* position (ep '-') and in P2")
        print("         White has the extra en-passant capture exd6 available.")
    finally:
        uninstall_fake_ytdlp(real)


if __name__ == "__main__":
    main()
