"""
FIXER-A consolidated post-fix verification probe for CODE-1 / CODE-2 / CODE-3 / CODE-4 / CODE-5 /
CODE-6 / CODE-17 in `backend/repertoire.py`.

Run (post-fix):
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXA-01_verify_fixes.py
Run (pre-fix, for the delta):
    FIXA_PREFIX_COPY=/path/to/pre-fix/repertoire.py \
        /home/robin/miniconda3/bin/python3 audit/scripts/FIXA-01_verify_fixes.py

Imports tests.test_setup first: everything runs on a throwaway temp copy of the database and the
script makes no request to 127.0.0.1:38281. It is idempotent (it DELETEs everything it creates).
"""
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # FIRST backend-adjacent import

if os.environ.get("FIXA_PREFIX_COPY"):
    import backend
    _spec = importlib.util.spec_from_file_location(
        "backend.repertoire", os.environ["FIXA_PREFIX_COPY"])
    _m = importlib.util.module_from_spec(_spec)
    sys.modules["backend.repertoire"] = _m
    _spec.loader.exec_module(_m)
    backend.repertoire = _m
    print("### running against PRE-FIX module:", os.environ["FIXA_PREFIX_COPY"], "###")
else:
    print("### running against the CURRENT backend/repertoire.py ###")

import chess  # noqa: E402
import backend.repertoire as rep_mod  # noqa: E402
from backend.db import get_db, init_db  # noqa: E402
from backend.repertoire import RepertoireManager, normalize_fen  # noqa: E402

init_db()
rm = RepertoireManager.get_instance()
START = normalize_fen(chess.STARTING_FEN)
FEN_EP = "rnbqkbnr/ppp2ppp/4p3/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 3"
FEN_NOEP = "rnbqkbnr/ppp2ppp/4p3/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq - 0 3"
FAILURES = []


def rows(sql, params=()):
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ("   " + str(detail) if detail else ""))
    if not ok:
        FAILURES.append(label)


def wipe():
    conn = get_db()
    try:
        for t in ("study_moves", "repertoire_moves", "repertoire_studies"):
            conn.execute("DELETE FROM %s" % t)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- CODE-1
print("\n[CODE-1] re-adding an existing move is an idempotent UPSERT")
wipe()
rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
rm.update_move_metadata("white", chess.STARTING_FEN, "e2e4", is_important=True,
                        comment="precious", arrows=json.dumps([{"from": "e2", "to": "e4"}]),
                        study_name="Anti French")
conn = get_db()
conn.execute("UPDATE repertoire_moves SET times_practiced=2, times_correct=2, last_practiced=11,"
             " next_review=22, interval_days=3.0 WHERE parent_fen=?", (START,))
conn.commit(); conn.close()
before = rows("SELECT * FROM repertoire_moves WHERE parent_fen=?", (START,))[0]
rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
after = rows("SELECT * FROM repertoire_moves WHERE parent_fen=?", (START,))
check("exactly one row", len(after) == 1, len(after))
a = after[0]
check("row id preserved", a["id"] == before["id"], "%s -> %s" % (before["id"], a["id"]))
check("comment preserved", a["comment"] == "precious", repr(a["comment"]))
check("arrows preserved", "e2" in (a["arrows"] or ""), repr(a["arrows"]))
check("is_important preserved", a["is_important"] == 1, a["is_important"])
check("study preserved", a["study_name"] == "Anti French", a["study_name"])
check("SRS preserved",
      (a["times_practiced"], a["times_correct"], a["last_practiced"], a["next_review"],
       a["interval_days"]) == (2, 2, 11, 22, 3.0),
      (a["times_practiced"], a["times_correct"], a["last_practiced"], a["next_review"],
       a["interval_days"]))
prog = rm.get_trainer_progress("white")
check("trainer still sees it as mastered", prog["mastered_count"] == 1, prog)
rm.add_repertoire_move("white", chess.STARTING_FEN, "d2d4", is_user_move=True)
u = rows("SELECT move_uci FROM repertoire_moves WHERE parent_fen=? AND is_user_move=1", (START,))
check("one user move per position still enforced", [x["move_uci"] for x in u] == ["d2d4"], u)

# ---------------------------------------------------------------- CODE-2
print("\n[CODE-2] force-seeding never prunes a user-prepared line and returns a result object")
wipe()
b = chess.Board()
for uci, user in [("e2e4", True), ("e7e6", False), ("d2d4", True), ("d7d5", False),
                  ("e4e5", True), ("c7c5", False), ("g1f3", True)]:
    rm.add_repertoire_move("white", b.fen(), uci, is_user_move=user)
    b.push_uci(uci)
rm.update_move_metadata("white", chess.STARTING_FEN, "e2e4", comment="anti-french prep")
before = rows("SELECT * FROM repertoire_moves WHERE repertoire='white' ORDER BY id")
res = rm.seed_default_repertoires(force=True)
check("returns a truthy dict", isinstance(res, dict) and res.get("status") == "success", res)
after = {r["id"]: r for r in rows("SELECT * FROM repertoire_moves")}
gone = [r["move_uci"] for r in before if r["id"] not in after]
check("no user row deleted", gone == [], gone)
check("comment intact", after.get(before[0]["id"], {}).get("comment") == "anti-french prep",
      after.get(before[0]["id"], {}).get("comment"))
skipped = rm.seed_default_repertoires(force=False)
check("guard path returns a dict", isinstance(skipped, dict) and skipped["status"] == "skipped",
      skipped)

# ---------------------------------------------------------------- CODE-3
print("\n[CODE-3] the prepared-move lookup is deterministic (exact parent wins)")
wipe()
rm.add_repertoire_move("white", FEN_NOEP, "b1c3", is_user_move=True)
rm.add_repertoire_move("white", FEN_EP, "g1f3", is_user_move=True)
other = rows("SELECT * FROM repertoire_moves WHERE parent_fen=?", (normalize_fen(FEN_NOEP),))[0]
right = rows("SELECT * FROM repertoire_moves WHERE parent_fen=?", (normalize_fen(FEN_EP),))[0]
ok = rm.evaluate_trainer_move("white", FEN_EP, "g1f3")
check("correct move accepted", ok.get("correct") is True, ok.get("message"))
check("validated against the exact parent row", ok.get("prepared_id") == right["id"],
      "%s vs %s" % (ok.get("prepared_id"), right["id"]))
bad = rm.evaluate_trainer_move("white", FEN_EP, "b1c3")
r_right = rows("SELECT * FROM repertoire_moves WHERE id=?", (right["id"],))[0]
r_other = rows("SELECT * FROM repertoire_moves WHERE id=?", (other["id"],))[0]
check("the mistake incremented only this position's row",
      r_right["times_practiced"] == 2 and r_other["times_practiced"] == 0,
      (r_right["times_practiced"], r_other["times_practiced"]))

# ---------------------------------------------------------------- CODE-4 / CODE-5
print("\n[CODE-4 + CODE-5] one explorer call per position, coloured with the repertoire")
wipe()
board = chess.Board()
for ply, uci in enumerate(["e2e4", "e7e5", "g1f3", "b8c6"]):
    # for a BLACK repertoire the user moves the black side, i.e. the odd plies
    rm.add_repertoire_move("black", board.fen(), uci, is_user_move=(ply % 2 == 1))
    board.push_uci(uci)
calls = []
orig_cand = rm.get_candidate_moves_2000
orig_gaps = rm.get_game_gaps
orig_count = rm.get_explorer_game_count
rm.get_game_gaps = lambda *a, **kw: {"gaps": [], "patched_gaps": []}
rm.get_explorer_game_count = lambda fen, ratings="2000,2200,2500": 0
rm.get_candidate_moves_2000 = lambda fen, ratings="2000,2200,2500", color="white": (
    calls.append((fen, color)) or {"fen": fen, "total_games": 100, "moves": [
        {"san": "d5", "uci": "d7d5", "games": 50, "popularity_pct": 30.0, "win_rate": 50.0}]})
try:
    rm.get_repertoire_tree("black")
finally:
    rm.get_candidate_moves_2000 = orig_cand
    rm.get_game_gaps = orig_gaps
    rm.get_explorer_game_count = orig_count
fens = [f for f, _ in calls]
expected = {normalize_fen(chess.STARTING_FEN)}  # a black repertoire starts with the opponent
expected |= {r["fen"] for r in rows("SELECT fen FROM repertoire_moves WHERE repertoire='black'")
             if chess.Board(r["fen"]).turn == chess.WHITE}
check("one call per distinct opponent position",
      len(fens) == len(set(fens)) == len(expected) > 0 and set(fens) == expected,
      calls)
check("every explorer call carried color='black'",
      {c for _, c in calls} == {"black"}, {c for _, c in calls})

# ---------------------------------------------------------------- CODE-6
print("\n[CODE-6] get_game_gaps is read-only and never persists a failed probe")
wipe()
conn = get_db()
try:
    conn.execute("INSERT OR REPLACE INTO games (id, speed, rated, played_at, user_color, result,"
                 " moves_san) VALUES ('fixa-verify','rapid',1,1,'white','black','e4 e5')")
    for i, fen in enumerate((FEN_EP, FEN_NOEP), start=1):
        conn.execute(
            "INSERT OR REPLACE INTO blunders (id, game_id, move_number, ply, color, fen_before,"
            " fen_after, played_move, played_san, best_move, best_san, eval_before, eval_after,"
            " cpl_loss, opening_name, eco, opening_games, created_at)"
            " VALUES (?, 'fixa-verify', 1, 1, 'white', ?, ?, 'g8f6', 'Nf6', 'g1f3', 'Nf3', 20, -180,"
            " 200, 'V', 'A00', NULL, 0)", (990000 + i, fen, fen))
    conn.commit()
finally:
    conn.close()

writes = []
real_get_db = rep_mod.get_db


def traced():
    c = real_get_db()
    c.set_trace_callback(
        lambda s: writes.append(" ".join(s.split())[:70])
        if s.strip() and s.lstrip().split(None, 1)[0].upper() in
        ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER") else None)
    return c


rm.get_explorer_game_count = lambda fen, ratings="2000,2200,2500": 0
rep_mod.get_db = traced
try:
    rm.get_game_gaps("white", min_games=30)
finally:
    rep_mod.get_db = real_get_db
    rm.get_explorer_game_count = orig_count
    stored = rows("SELECT opening_games FROM blunders WHERE id IN (990001, 990002) ORDER BY id")
    conn = get_db()
    try:
        conn.execute("DELETE FROM blunders WHERE id IN (990001, 990002)")
        conn.execute("DELETE FROM games WHERE id = 'fixa-verify'")
        conn.commit()
    finally:
        conn.close()
check("no write statement was executed", writes == [], writes)
check("the failed probe was not persisted",
      [s["opening_games"] for s in stored] == [None, None],
      [s["opening_games"] for s in stored])

# ---------------------------------------------------------------- CODE-17
print("\n[CODE-17] delete_study(delete_moves=True) only removes exclusive moves and prunes children")
wipe()
rm.create_study("Shared A", "white")
rm.create_study("Shared B", "white")
rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True, study_name="Shared A")
b = chess.Board(); b.push_uci("e2e4")
rm.add_repertoire_move("white", b.fen(), "e7e5", is_user_move=False, study_name="Shared A")
rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True, study_name="Shared B")
study_b = rm.get_study_by_name("Shared B", "white")
rm.delete_study(study_b["id"], delete_moves=True)
survivors = sorted(r["move_uci"] for r in rows("SELECT * FROM repertoire_moves"))
check("the shared move and its child survived", survivors == ["e2e4", "e7e5"], survivors)
e4 = [r for r in rows("SELECT * FROM repertoire_moves") if r["move_uci"] == "e2e4"]
check("study_name re-pointed at the surviving study",
      bool(e4) and e4[0]["study_name"] == "Shared A", e4[0]["study_name"] if e4 else None)
check("the deleted study is gone", rm.get_study_by_name("Shared B", "white") is None)

wipe()
rm.create_study("Solo", "white")
board = chess.Board()
rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True, study_name="Solo")
board.push_uci("e2e4")
rm.add_repertoire_move("white", board.fen(), "e7e5", is_user_move=False, study_name="Solo")
board.push_uci("e7e5")
rm.add_repertoire_move("white", board.fen(), "g1f3", is_user_move=True, study_name="Solo")
res = rm.delete_study(rm.get_study_by_name("Solo", "white")["id"], delete_moves=True)
check("exclusive moves deleted", res.get("deleted_moves") == 3, res)
check("no dangling children left", rows("SELECT * FROM repertoire_moves") == [],
      rows("SELECT * FROM repertoire_moves"))
check("no dangling study_moves", rows("SELECT * FROM study_moves") == [])

wipe()
print("\n%s  (%d failing check(s))" % ("ALL CHECKS PASSED" if not FAILURES else "FAILURES", len(FAILURES)))
sys.exit(1 if FAILURES else 0)
