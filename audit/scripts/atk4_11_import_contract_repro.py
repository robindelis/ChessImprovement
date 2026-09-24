"""SCOUT-11 repro: whole-line import contract violations.

 1. overlap == already_present still performs DB writes ("prevents redundant
    writes" per AGENTS.md 4E) while the response says "No changes needed".
 2. the overlap *check* accepts SAN-only moves, the *import* silently skips them
    (badge says "+4 new", import writes 0).
 3. import returns status=success even when nothing at all was written.
 4. importAllLinesToRepertoire's DFS ordering destroys sibling branches.
 5. hostile / unreachable fen_before values.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk4_mocks import banner, rep_dump  # noqa: E402

import chess  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402
from backend.scout import ContentScout  # noqa: E402
from backend.db import get_db  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
scout = ContentScout.get_instance()


def rowcount():
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) c FROM repertoire_moves").fetchone()["c"]
    conn.close()
    return n


def section(t):
    print("\n" + "-" * 74)
    print(t)
    print("-" * 74)


def t1_already_present_still_writes():
    section("1. already_present -> 'No changes needed' but rows are rewritten")
    moves = scout.analyze_source("1. e4 c6 2. d4 d5 *")["full_timeline"]
    client.post("/api/scout/import_repertoire", json={"repertoire": "white", "moves": moves})
    conn = get_db()
    before = conn.execute("SELECT id, move_uci, created_at, study_name FROM repertoire_moves ORDER BY id").fetchall()
    conn.close()
    n0 = rowcount()

    r = client.post("/api/scout/import_repertoire",
                    json={"repertoire": "white", "moves": moves}).json()
    conn = get_db()
    after = conn.execute("SELECT id, move_uci, created_at, study_name FROM repertoire_moves ORDER BY id").fetchall()
    conn.close()
    print("   response:", json.dumps({k: r[k] for k in
          ("already_present", "overlap_status", "imported_count", "new_count", "message")}, ensure_ascii=False))
    print("   rows before/after:", n0, "/", rowcount())
    print("   created_at rewritten on every re-import:",
          [dict(a) for a in after] != [dict(b) for b in before],
          "->", [b["created_at"] for b in before], "->", [a["created_at"] for a in after])
    print("   new_count reported:", r["new_count"], " imported_count reported:", r["imported_count"])

    # the sharpest form: already_present + a *different* study_name still rewrites the rows
    moves2 = scout.analyze_source("1. d4 d5 *")["full_timeline"]
    client.post("/api/scout/import_repertoire",
                json={"repertoire": "white", "moves": moves2, "study_name": "Study A"})
    conn = get_db()
    a = [(x["move_san"], x["study_name"]) for x in
         conn.execute("SELECT move_san, study_name FROM repertoire_moves ORDER BY id")]
    conn.close()
    r2 = client.post("/api/scout/import_repertoire",
                     json={"repertoire": "white", "moves": moves2, "study_name": "Study B"}).json()
    conn = get_db()
    b = [(x["move_san"], x["study_name"]) for x in
         conn.execute("SELECT move_san, study_name FROM repertoire_moves ORDER BY id")]
    conn.close()
    print("   re-importing an already_present line with study B ->", r2["message"])
    print("   study_name before:", a)
    print("   study_name after :", b, " <-- rows rewritten while claiming 'No changes needed'")


def t2_san_only():
    section("2. check_line accepts SAN-only moves, import_repertoire silently drops them")
    san_only = [{"san": s} for s in ["e4", "c6", "d4", "d5", "Nf3", "g6"]]
    chk = client.post("/api/scout/check_line", json={"repertoire": "white", "moves": san_only}).json()
    print("   check_line  ->", json.dumps({k: chk[k] for k in
          ("status", "total_plies", "matched_plies", "new_plies", "message")}, ensure_ascii=False))
    n0 = rowcount()
    imp = client.post("/api/scout/import_repertoire",
                      json={"repertoire": "white", "moves": san_only}).json()
    print("   import      ->", json.dumps({k: imp[k] for k in
          ("status", "imported_count", "new_count", "message", "errors")}, ensure_ascii=False))
    print("   rows before/after:", n0, "/", rowcount())
    print("   frontend would show: badge '+%s new' then toast '%s'" % (chk["new_plies"], imp["message"]))


def t3_success_with_zero_writes():
    section("3. import reports status=success although nothing was written")
    bogus = [
        {"fen_before": "not-a-fen", "uci": "e2e4"},
        {"fen_before": "8/8/8/8/8/8/8/8 w - -", "uci": "a1a2"},
        {"fen_before": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -", "uci": "e2e9"},
        {"fen_before": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -", "uci": "zzzz"},
        {"uci": "e2e4"},
        {"fen_before": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"},
    ]
    n0 = rowcount()
    r = client.post("/api/scout/import_repertoire",
                    json={"repertoire": "white", "moves": bogus}).json()
    print("   request moves:", json.dumps(bogus)[:150], "...")
    print("   response:", json.dumps({k: r[k] for k in
          ("status", "imported_count", "already_present", "overlap_status", "errors", "message")}, ensure_ascii=False))
    print("   rows before/after:", n0, "/", rowcount())
    r422 = client.post("/api/scout/import_repertoire",
                       json={"repertoire": "white", "moves": ["not-even-a-dict"]})
    print("   a bare string in `moves` -> HTTP", r422.status_code, str(r422.json())[:120])


def t4_all_lines_dfs():
    section("4. importAllLinesToRepertoire DFS ordering destroys sibling branches")
    client.post("/api/repertoire/clear_all" if False else "/api/scout/import_repertoire",
                json={"repertoire": "white",
                      "moves": scout.analyze_source("1. e4 *")["full_timeline"],
                      "study_name": "base"})
    # session tree equivalent: getAllUniqueMoves() DFS order over two sibling branches
    a = scout.analyze_source("1. e4 c5 2. Nf3 *")["full_timeline"]
    b = scout.analyze_source("1. d4 d5 2. c4 *")["full_timeline"]
    dfs_order = a + b
    n0 = rowcount()
    r = client.post("/api/scout/import_repertoire",
                    json={"repertoire": "white", "moves": dfs_order, "study_name": "all"}).json()
    print("   DFS move order:", [m["uci"] for m in dfs_order])
    print("   response:", json.dumps({k: r[k] for k in
          ("imported_count", "overlap_status", "message")}, ensure_ascii=False))
    rows = rep_dump("white")
    print("   surviving moves:", [(x["uci"], x["san"], x["is_user_move"]) for x in rows])
    print("   -> branch '1.e4 c5 2.Nf3' survives only if e4 was not replaced; e4 replaced by d4:",
          any(x["uci"] == "e2e4" for x in rows))


def t5_hostile_fens():
    section("5. hostile / unreachable fen_before")
    three_queens = "qqqqkqqq/8/8/8/8/8/8/QQQQKQQQ w - - 0 1"
    r = client.post("/api/scout/import_repertoire",
                    json={"repertoire": "white", "moves": [{"fen_before": three_queens, "uci": "b1a1"}]}).json()
    print("   3 black+white queens FEN ->", r["imported_count"], r["errors"])
    # black-to-move parent declared for the WHITE repertoire
    r2 = client.post("/api/scout/import_repertoire",
                     json={"repertoire": "white",
                           "moves": [{"fen_before": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
                                      "uci": "e7e5"}]}).json()
    row = [x for x in rep_dump("white") if x["uci"] == "e7e5"]
    print("   opponent reply after 1.e4 ->", r2["imported_count"], "row:", row)
    # a mid-game FEN: creates a completely detached branch off the repertoire tree
    midgame = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 5 4"
    r3 = client.post("/api/scout/import_repertoire",
                     json={"repertoire": "white", "moves": [{"fen_before": midgame, "uci": "c6a5"}]}).json()
    print("   detached mid-game branch ->", r3["imported_count"], r3["errors"])
    detached = [x for x in rep_dump("white") if x["uci"] == "c6a5"]
    print("   stored row:", detached)
    print("   (is this FEN reachable inside the repertoire tree? -> no, it was accepted anyway)")
    orphans = get_db()
    rows = orphans.execute("SELECT move_san, parent_fen FROM repertoire_moves WHERE parent_fen=?",
                           ("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -",)).fetchall()
    print("   children of the start position:", [dict(x) for x in rows])
    orphans.close()


if __name__ == "__main__":
    banner("SCOUT-11  whole-line import contract violations")
    t1_already_present_still_writes()
    t2_san_only()
    t3_success_with_zero_writes()
    t4_all_lines_dfs()
    t5_hostile_fens()
