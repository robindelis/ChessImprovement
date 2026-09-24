"""
SCOUT-02 repro: importing a scouted line that diverges at a prepared USER move
silently deletes (and recursively prunes) the user's move + subtree.

AGENTS.md 4E: "If a user move diverges at a turn where a move is already prepared,
warns that the new move replaces the prepared move."  The overlap *check* does warn.
The *import* does not: it writes anyway, and the frontend never shows the warning.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk4_mocks import banner, rep_dump  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
import chess  # noqa: E402
from backend.api import app  # noqa: E402
from backend.scout import ContentScout  # noqa: E402


def show(rows, label):
    print(f"  --- repertoire after {label}: {len(rows)} move(s)")
    for r in rows:
        print(f"      ply_parent_turn={r['parent_fen'].split()[1]}  {r['uci']:6s} {r['san']:6s} "
              f"is_user_move={r['is_user_move']}  study={r['study_name']!r}")


def main():
    banner("SCOUT-02  divergence import destroys the user's prepared move (no warning path)")
    client = TestClient(app, raise_server_exceptions=False)
    scout = ContentScout.get_instance()

    # 1. The user's real preparation (AGENTS.md 9.3): Anti-French
    base = scout.analyze_source("1. e4 e6 2. d4 d5 3. e5 c5 4. Nc3 Nc6 5. Nf3 *")["full_timeline"]
    r = client.post("/api/scout/import_repertoire",
                    json={"repertoire": "white", "moves": base, "study_name": "Anti French"})
    print("seed import ->", json.dumps({k: r.json()[k] for k in
          ("imported_count", "already_present", "overlap_status", "message")}, ensure_ascii=False))
    show(rep_dump("white"), "seed")

    # 2. Now import a video line that keeps the first 4 plies but plays 3. Nc3
    #    (a user-move ply where 3. e5 is already prepared).
    div = scout.analyze_source("1. e4 e6 2. d4 d5 3. Nc3 Nf6 *")["full_timeline"]
    check = client.post("/api/scout/check_line", json={"repertoire": "white", "moves": div}).json()
    print("\n/api/scout/check_line says:")
    print("   status        =", check["status"])
    print("   divergence    =", check["divergence_info"])
    print("   message       =", check["message"])

    imp = client.post("/api/scout/import_repertoire",
                      json={"repertoire": "white", "moves": div, "study_name": "From Video"}).json()
    print("\n/api/scout/import_repertoire response:")
    for k in ("status", "already_present", "overlap_status", "imported_count", "message", "errors"):
        print(f"   {k:16s}= {imp[k]!r}")

    after = rep_dump("white")
    show(after, "divergent import")

    sans = sorted(r["san"] for r in after)
    print("\n  3. e5 still present? ", "e5" in sans)
    print("  3. e5 child  (4. c5) present? ", "c5" in sans)
    print("  4. Nc3 (its child) present?  ", "Nc3" in sans)
    print("  3. Nc3 (new) present?        ", "Nc3" in sans)
    ok = ("e5" not in sans) and ("c5" not in sans) and ("Nc3" in sans)
    print("\nVERDICT:", "USER MOVE + SUBTREE DESTROYED" if ok else "no destruction observed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
