"""
SCOUT-03 repro: an overlapping Scout import relabels the user's curated study and
leaves dangling study_moves links behind.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk4_mocks import banner  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402
from backend.scout import ContentScout  # noqa: E402
from backend.db import get_db  # noqa: E402


def main():
    banner("SCOUT-03  overlapping import hijacks study_name + leaves dangling study_moves")
    client = TestClient(app, raise_server_exceptions=False)
    scout = ContentScout.get_instance()

    base = scout.analyze_source("1. e4 e6 2. d4 d5 3. e5 *")["full_timeline"]
    client.post("/api/scout/import_repertoire",
                json={"repertoire": "white", "moves": base, "study_name": "Anti French"})
    div = scout.analyze_source("1. e4 e6 2. d4 d5 3. Nc3 *")["full_timeline"]
    client.post("/api/scout/import_repertoire",
                json={"repertoire": "white", "moves": div, "study_name": "Video Import"})

    conn = get_db()
    print("repertoire_moves.study_name:")
    for r in conn.execute("SELECT move_uci, move_san, study_name FROM repertoire_moves ORDER BY id"):
        print("   %-6s %-5s -> %r" % (r["move_uci"], r["move_san"], r["study_name"]))

    print("\nstudy_moves links (study name -> uci) and whether the move still exists:")
    for r in conn.execute("""SELECT rs.name, sm.parent_fen, sm.move_uci FROM study_moves sm
                             JOIN repertoire_studies rs ON rs.id = sm.study_id"""):
        exists = conn.execute(
            "SELECT COUNT(*) c FROM repertoire_moves WHERE repertoire=? AND parent_fen=? AND move_uci=?",
            ("white", r["parent_fen"], r["move_uci"])).fetchone()["c"]
        print("   %-14r %-6s exists=%s %s" % (r["name"], r["move_uci"], bool(exists),
                                              "  <== DANGLING" if not exists else ""))
    conn.close()

    print("\nGET /api/repertoire/studies:")
    for st in client.get("/api/repertoire/studies").json():
        print("   %-14r total_moves=%s user_moves=%s" % (st["name"], st["total_moves"], st["user_moves"]))


if __name__ == "__main__":
    main()
