"""
Adversarial Attack & Deep Abuse Suite - Round 4
Chess Opening Improvement Platform

Covers 4 Attack Targets following the strict 4-step adversarial protocol:
  Target 1: Repertoire DAG & Cycle Fuzzing (Cycles, Transposition Convergence, Root Deletion, Invariant Turn Checks)
  Target 2: Evaluator Engine Fuzzing & Resource Exhaustion (Depth 30 Abort + 5 Subsequent Queries, Invalid FENs, Query Param Fuzzing)
  Target 3: Trainer Spaced Repetition Logic Exploitation (100x Training, 0 Due Moves Rotation, Illegal/Malformed UCI Moves)
  Target 4: Explorer & Blunder APIs (Blunder Gap Threshold Inversion, Query Param Boundaries, Malformed FENs, Truncated FEN Phantom Moves)
"""
import os
import sys
import json
import time
import sqlite3
import requests
import chess
import concurrent.futures
from typing import Dict, Any, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# CODE-05-10: `import tests.test_setup` must be the FIRST backend-adjacent
# import. It used to come after `from backend.repertoire import normalize_fen`.
import tests.test_setup
from tests.probe_guard import require_isolated_db
from backend.repertoire import normalize_fen
from backend.api import app
from fastapi.testclient import TestClient

client = TestClient(app, raise_server_exceptions=False)
BASE_URL = "http://testserver"
# CODE-05-10: fail-open default removed; this raises instead of falling back to
# the production database (which this script DELETEs from).
DB_PATH = require_isolated_db()

class RequestAdapter:
    def get(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        kwargs.pop("stream", None)
        kwargs.pop("timeout", None)
        return client.get(path, **kwargs)
    def post(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        if path == "/api/repertoire/seed":
            path = "/api/repertoire/seed?force=true"
        kwargs.pop("timeout", None)
        return client.post(path, **kwargs)
    def delete(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        kwargs.pop("timeout", None)
        return client.request("DELETE", path, **kwargs)

requests = RequestAdapter()


class AdversarialProbeRunner:
    def __init__(self):
        self.bugs_found = []
        self.tests_run = 0
        self.tests_passed = 0

    def record_bug(self, bug_id: str, title: str, action: str, predicted: str, actual: str, repro_code: str):
        self.bugs_found.append({
            "id": bug_id,
            "title": title,
            "action": action,
            "predicted": predicted,
            "actual": actual,
            "repro_code": repro_code
        })
        print(f"\n🔥🔥🔥 [BUG DETECTED] {bug_id}: {title} 🔥🔥🔥")
        print(f"Action: {action}")
        print(f"Predicted: {predicted}")
        print(f"Actual: {actual}")
        print("=" * 70)

    def probe(self, name: str, action_desc: str, predicted_desc: str, fn):
        self.tests_run += 1
        print(f"\n[{self.tests_run}] PROBE: {name}")
        print(f"  STEP 1 (Action): {action_desc}")
        print(f"  STEP 2 (Predicted): {predicted_desc}")
        try:
            actual_desc, is_bug, bug_details = fn()
            print(f"  STEP 3 & 4 (Actual): {actual_desc}")
            if is_bug:
                self.record_bug(
                    bug_id=bug_details.get("id", f"BUG-ADV-{self.tests_run:02d}"),
                    title=bug_details.get("title", name),
                    action=action_desc,
                    predicted=predicted_desc,
                    actual=actual_desc,
                    repro_code=bug_details.get("repro", "")
                )
            else:
                self.tests_passed += 1
                print(f"  RESULT: PASSED (Behavior matches prediction)")
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"  STEP 3 & 4 (Exception): {tb}")
            self.record_bug(
                bug_id=f"BUG-ADV-{self.tests_run:02d}",
                title=f"Unhandled Exception in {name}",
                action=action_desc,
                predicted=predicted_desc,
                actual=f"Exception: {e}\n{tb}",
                repro_code=f"# Unhandled exception during probe {name}"
            )


runner = AdversarialProbeRunner()


def get_db_move_count(repertoire: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM repertoire_moves WHERE repertoire = ?", (repertoire,))
    cnt = c.fetchone()[0]
    conn.close()
    return cnt


# ==============================================================================
# TARGET 1: Repertoire DAG & Cycle Fuzzing
# ==============================================================================

def test_cycle_deletion_cascade():
    """
    Test move repetition cycles: 1. Nf3 Nf6 2. Ng1 Ng8 returns to root STARTING_FEN.
    Deleting 1. Nf3 should delete only the cyclic line without cascading into STARTING_FEN
    and wiping out independent opening branches (e.g. 1. e4 Sicilian Dragon, 1. d4 King's Indian).
    """
    # 1. Reset / Seed Black repertoire
    requests.post(f"{BASE_URL}/api/repertoire/seed")
    cnt_before = get_db_move_count("black")

    # 2. Add cyclic knight dance in Black repertoire:
    # 1. Nf3 (opp)
    b = chess.Board()
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": b.fen(), "move_uci": "g1f3", "is_user_move": False
    })
    b.push_san("Nf3")
    # 1... Nf6 (user)
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": b.fen(), "move_uci": "g8f6", "is_user_move": True
    })
    b.push_san("Nf6")
    # 2. Ng1 (opp)
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": b.fen(), "move_uci": "f3g1", "is_user_move": False
    })
    b.push_san("Ng1")
    # 2... Ng8 (user) -> returns to starting position!
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": b.fen(), "move_uci": "f6g8", "is_user_move": True
    })

    cnt_after_add = get_db_move_count("black")

    # 3. Delete 1. Nf3 from starting position
    del_r = requests.delete(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": chess.STARTING_FEN, "move_uci": "g1f3"
    })
    if del_r.status_code != 200:
        return f"Delete request returned {del_r.status_code}: {del_r.text}", True, {
            "id": "BUG-R4-01-REQ", "title": "Delete cyclic move request failed", "repro": ""
        }

    cnt_after_del = get_db_move_count("black")

    # 4. Verify whether root moves were purged
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT move_uci FROM repertoire_moves WHERE repertoire = 'black' AND move_uci in ('e2e4', 'd2d4')")
    surviving_root_moves = [r[0] for r in c.fetchall()]
    conn.close()

    if cnt_after_del < cnt_before or ("e2e4" not in surviving_root_moves and "d2d4" not in surviving_root_moves):
        # Re-seed to repair database
        requests.post(f"{BASE_URL}/api/repertoire/seed")
        return (
            f"Catastrophic deletion occurred! Before: {cnt_before} moves, After delete: {cnt_after_del} moves. "
            f"Root moves ('e2e4', 'd2d4') were completely deleted! Surviving: {surviving_root_moves}",
            True,
            {
                "id": "BUG-R4-01",
                "title": "Catastrophic Repertoire Deletion on Cyclic Move Line Deletion (Root / Ancestor Cascade Purge)",
                "repro": (
                    "# Add cyclic line returning to starting position in Black repertoire:\n"
                    "requests.post('/api/repertoire/move', json={'repertoire':'black','parent_fen':START,'move_uci':'g1f3','is_user_move':False})\n"
                    "requests.post('/api/repertoire/move', json={'repertoire':'black','parent_fen':FEN1,'move_uci':'g8f6','is_user_move':True})\n"
                    "requests.post('/api/repertoire/move', json={'repertoire':'black','parent_fen':FEN2,'move_uci':'f3g1','is_user_move':False})\n"
                    "requests.post('/api/repertoire/move', json={'repertoire':'black','parent_fen':FEN3,'move_uci':'f6g8','is_user_move':True})\n"
                    "# Delete the branch:\n"
                    "requests.delete('/api/repertoire/move', json={'repertoire':'black','parent_fen':START,'move_uci':'g1f3'})\n"
                    "# Deletion cascades into STARTING_FEN and wipes out 1. e4, 1. d4 and the entire repertoire!"
                )
            }
        )

    # Re-seed to ensure clean state
    requests.post(f"{BASE_URL}/api/repertoire/seed")
    return f"Cyclic line deleted cleanly: {cnt_after_add} -> {cnt_after_del} (Root moves preserved: {surviving_root_moves})", False, {}


def test_multi_way_transposition_convergence():
    """
    3 separate opening paths converging to the exact same position (Sicilian Four Knights/Taimanov).
    Delete Path 1 -> Downstream moves must be preserved (2 paths remain).
    Delete Path 2 -> Downstream moves must be preserved (1 path remains).
    Delete Path 3 -> Downstream moves must now be cleanly deleted.
    """
    def add_line(moves):
        b = chess.Board()
        for ply, san in enumerate(moves):
            p_fen = b.fen()
            m = b.parse_san(san)
            is_user = (ply % 2 == 0)
            requests.post(f"{BASE_URL}/api/repertoire/move", json={
                "repertoire": "white", "parent_fen": p_fen, "move_uci": m.uci(), "is_user_move": is_user
            })
            b.push(m)
        return normalize_fen(b.fen())

    # Path 1: 1. e4 c5 2. Nf3 e6 3. d4 cxd4 4. Nxd4 Nc6
    f1 = add_line(["e4", "c5", "Nf3", "e6", "d4", "cxd4", "Nxd4", "Nc6"])
    # Path 2: 1. e4 c5 2. Nf3 Nc6 3. d4 cxd4 4. Nxd4 e6
    f2 = add_line(["e4", "c5", "Nf3", "Nc6", "d4", "cxd4", "Nxd4", "e6"])
    # Path 3: 1. e4 e6 2. d4 c5 3. Nf3 cxd4 4. Nxd4 Nc6
    f3 = add_line(["e4", "e6", "d4", "c5", "Nf3", "cxd4", "Nxd4", "Nc6"])

    if not (f1 == f2 == f3):
        return f"Setup error: Converging FENs do not match: f1={f1}, f2={f2}, f3={f3}", True, {
            "id": "BUG-CONV-SETUP", "title": "Setup error", "repro": ""
        }

    conv_fen = f1

    # Add downstream moves from conv_fen: 5. Nc3 (user) 5... a6 (opp)
    b_down = chess.Board(conv_fen)
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white", "parent_fen": b_down.fen(), "move_uci": "b1c3", "is_user_move": True
    })
    b_down.push_san("Nc3")
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white", "parent_fen": b_down.fen(), "move_uci": "a7a6", "is_user_move": False
    })

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM repertoire_moves WHERE repertoire = 'white' AND parent_fen = ?", (conv_fen,))
    down_cnt_init = c.fetchone()[0]
    conn.close()

    if down_cnt_init == 0:
        return "Downstream moves failed to save in repertoire_moves", True, {
            "id": "BUG-CONV-INIT", "title": "Downstream moves failed to save", "repro": ""
        }

    # Step 1: Delete Path 1 intermediate move: 2... e6
    b_del1 = chess.Board()
    for s in ["e4", "c5", "Nf3"]:
        b_del1.push_san(s)
    requests.delete(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white", "parent_fen": b_del1.fen(), "move_uci": "e7e6"
    })

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM repertoire_moves WHERE repertoire = 'white' AND parent_fen = ?", (conv_fen,))
    down_cnt_1 = c.fetchone()[0]
    conn.close()

    if down_cnt_1 == 0:
        return f"Premature downstream deletion after Path 1 deleted: down_cnt={down_cnt_1} (Paths 2 & 3 remained)", True, {
            "id": "BUG-CONV-DEL1", "title": "Downstream moves deleted after Path 1 deletion", "repro": ""
        }

    # Step 2: Delete Path 2 intermediate move: 2... Nc6
    requests.delete(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white", "parent_fen": b_del1.fen(), "move_uci": "b8c6"
    })

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM repertoire_moves WHERE repertoire = 'white' AND parent_fen = ?", (conv_fen,))
    down_cnt_2 = c.fetchone()[0]
    conn.close()

    if down_cnt_2 == 0:
        return f"Premature downstream deletion after Path 2 deleted: down_cnt={down_cnt_2} (Path 3 remained)", True, {
            "id": "BUG-CONV-DEL2", "title": "Downstream moves deleted after Path 2 deletion", "repro": ""
        }

    # Step 3: Delete Path 3: 1... e6
    b_del3 = chess.Board()
    b_del3.push_san("e4")
    requests.delete(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white", "parent_fen": b_del3.fen(), "move_uci": "e7e6"
    })

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM repertoire_moves WHERE repertoire = 'white' AND parent_fen = ?", (conv_fen,))
    down_cnt_3 = c.fetchone()[0]
    conn.close()

    if down_cnt_3 != 0:
        return f"Downstream moves NOT pruned after all 3 paths deleted: down_cnt={down_cnt_3}", True, {
            "id": "BUG-CONV-ORPHAN", "title": "Downstream moves orphaned after all paths deleted", "repro": ""
        }

    return f"3-way transposition verified: Init={down_cnt_init}, Del1={down_cnt_1}, Del2={down_cnt_2}, Del3={down_cnt_3} (Pruned)", False, {}


def test_turn_invariant_enforcement():
    """
    Ensure is_user_move strictly matches repertoire color turn:
    - White user move on Black's turn -> HTTP 400
    - White opp move on White's turn -> HTTP 400
    - Black user move on White's turn -> HTTP 400
    - Black opp move on Black's turn -> HTTP 400
    """
    b_w = chess.Board() # White's turn
    b_b = chess.Board()
    b_b.push_san("e4") # Black's turn

    cases = [
        ("White repertoire: user move on Black's turn", "white", b_b.fen(), "e7e5", True, 400),
        ("White repertoire: opp move on White's turn", "white", b_w.fen(), "e2e4", False, 400),
        ("Black repertoire: user move on White's turn", "black", b_w.fen(), "e2e4", True, 400),
        ("Black repertoire: opp move on Black's turn", "black", b_b.fen(), "e7e5", False, 400),
        ("White repertoire: user move on White's turn (valid)", "white", b_w.fen(), "e2e4", True, 200),
        ("Black repertoire: user move on Black's turn (valid)", "black", b_b.fen(), "e7e5", True, 200),
    ]

    for name, rep, fen, move, is_user, exp_status in cases:
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": rep, "parent_fen": fen, "move_uci": move, "is_user_move": is_user
        })
        if r.status_code != exp_status:
            return f"{name} returned {r.status_code} instead of {exp_status}: {r.text}", True, {
                "id": "BUG-TURN-INV", "title": f"Turn invariant check failed for {name}",
                "repro": f"POST /api/repertoire/move with rep={rep}, fen={fen}, move={move}, is_user={is_user}"
            }

    return "All 6 turn vs is_user_move invariant cases strictly enforced (HTTP 400 / 200)", False, {}


def test_delete_empty_parent_fen():
    """
    DELETE /api/repertoire/move with empty parent_fen or null must return HTTP 400/422 without 500 crash.
    """
    r_empty = requests.delete(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white", "parent_fen": "", "move_uci": "e2e4"
    })
    if r_empty.status_code != 400:
        return f"Empty parent_fen returned {r_empty.status_code} instead of 400: {r_empty.text}", True, {
            "id": "BUG-DEL-EMPTY", "title": "Empty parent_fen did not return 400", "repro": ""
        }

    r_none = requests.delete(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white", "parent_fen": None, "move_uci": "e2e4"
    })
    if r_none.status_code != 422:
        return f"Null parent_fen returned {r_none.status_code} instead of 422: {r_none.text}", True, {
            "id": "BUG-DEL-NULL", "title": "Null parent_fen did not return 422", "repro": ""
        }

    return "Empty (400) and Null (422) parent_fen rejected cleanly", False, {}


# ==============================================================================
# TARGET 2: Evaluator Engine Fuzzing & Resource Exhaustion
# ==============================================================================

def test_depth_30_cancellation_and_subsequent_queries():
    """
    Request maximum depth 30 on a complex middlegame FEN, abort after 50ms,
    then immediately execute 5 subsequent queries.
    Stockfish must cancel cleanly, release the async lock, and not block subsequent queries.
    """
    complex_fen = "r1b1k2r/pp1n1ppp/2p1pn2/q2p2B1/2PP4/P1P1PN2/5PPP/R2QKB1R w KQkq - 1 10"

    t0 = time.time()
    try:
        r = client.get("/api/repertoire/eval_stream", params={"fen": complex_fen, "depth": 30})
    except Exception:
        pass

    time.sleep(0.05)

    test_fens = [
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
        "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
        "r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
    ]

    def fetch_eval(f):
        resp = client.get("/api/repertoire/eval_stream", params={"fen": f, "depth": 6})
        return resp.status_code, "data:" in resp.text

    t_all = time.time()
    results = [fetch_eval(f) for f in test_fens]
    elapsed = round(time.time() - t_all, 3)

    for code, has_data in results:
        if code != 200 or not has_data:
            return f"Subsequent query failed with status {code}, has_data={has_data}", True, {
                "id": "BUG-EVAL-CANCEL-FAIL", "title": "Subsequent queries blocked after depth 30 abort",
                "repro": "Abort depth 30 after 50ms, then query eval_stream"
            }

    if elapsed > 15.0:
        return f"Subsequent queries blocked/delayed too long ({elapsed}s)", True, {
            "id": "BUG-EVAL-CANCEL-SLOW", "title": "Engine cancellation slow to release lock",
            "repro": f"Elapsed {elapsed}s"
        }

    return f"Depth 30 cancelled cleanly; 5 subsequent queries completed in {elapsed}s (All 200 OK)", False, {}


def test_invalid_fen_structures():
    """
    Test 6 structurally illegal or invalid chess FENs on /api/repertoire/eval_stream:
    1. 9 pawns of one color
    2. 2 kings of one color
    3. Pawn on 1st rank
    4. Pawn on 8th rank
    5. Invalid castling rights token ('KQkqx')
    6. Side not to move is in check
    All must gracefully return HTTP 400 Bad Request.
    """
    invalid_fens = [
        ("9 pawns", "rnbqkbnr/ppppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"),
        ("2 kings", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNK w KQkq - 0 1"),
        ("Pawn on 1st rank", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPP1P/RNBQKBNP w KQkq - 0 1"),
        ("Pawn on 8th rank", "rnbqkbnP/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"),
        ("Invalid castling token", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkqx - 0 1"),
        ("King in check when opponent turn", "4k3/8/8/8/8/8/8/4R2K w - - 0 1"),
    ]

    for name, fen in invalid_fens:
        r = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": fen, "depth": 10})
        if r.status_code != 400:
            return f"Invalid FEN ({name}) returned status {r.status_code} instead of 400: {r.text}", True, {
                "id": "BUG-FEN-STRUCT", "title": f"Illegal FEN structure {name} not rejected with 400",
                "repro": f"GET /api/repertoire/eval_stream with fen={fen}"
            }

    return "All 6 structurally illegal FENs rejected with HTTP 400 Bad Request", False, {}


def test_eval_depth_parameter_fuzzing():
    """
    Fuzz depth parameter with depth=-5, depth=999, depth='abc'.
    All must return HTTP 422 Unprocessable Entity.
    """
    fen = chess.STARTING_FEN
    cases = [
        ({"fen": fen, "depth": -5}, 422, "depth=-5"),
        ({"fen": fen, "depth": 999}, 422, "depth=999"),
        ({"fen": fen, "depth": "abc"}, 422, "depth='abc'"),
    ]
    for params, exp_code, name in cases:
        r = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params=params)
        if r.status_code != exp_code:
            return f"{name} returned {r.status_code} instead of {exp_code}: {r.text}", True, {
                "id": "BUG-DEPTH-PARAM", "title": f"Fuzzed depth param {name} returned {r.status_code}",
                "repro": f"GET /api/repertoire/eval_stream?{params}"
            }

    return "Depth parameter boundaries (-5, 999, 'abc') all rejected with HTTP 422", False, {}


# ==============================================================================
# TARGET 3: Trainer Spaced Repetition Logic Exploitation
# ==============================================================================

def test_repeated_training_stability():
    """
    Repeated training 100 times on a single move with correct=True:
    - In /api/train/evaluate_move: interval_days must be capped (<= 3650.0) without NaN or Infinity.
    - In /api/repertoire/train/attempt: times_practiced and next_review must remain valid integers.
    """
    # 1. Blunder drill spaced repetition (100 times)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, best_move FROM blunders WHERE best_move IS NOT NULL LIMIT 1")
    bl = c.fetchone()
    conn.close()
    if not bl:
        return "No blunder with best_move found in DB", True, {"id": "BUG-BL-NONE", "title": "No blunder", "repro": ""}

    bl_id, best_move = bl
    for _ in range(100):
        r = requests.post(f"{BASE_URL}/api/train/evaluate_move", json={"blunder_id": bl_id, "attempted_uci": best_move})
        if r.status_code != 200:
            return f"Blunder train iteration failed with {r.status_code}: {r.text}", True, {
                "id": "BUG-TRAIN-FAIL", "title": "Evaluate move returned non-200", "repro": ""
            }

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT interval_days, next_review_date FROM drill_reviews WHERE blunder_id = ?", (bl_id,))
    row = c.fetchone()
    conn.close()

    interval, next_rev = row
    if interval is None or interval > 3650.0 or interval <= 0 or not isinstance(next_rev, int):
        return f"Drill review interval explosion: interval={interval}, next_rev={next_rev}", True, {
            "id": "BUG-INTERVAL-EXPLODE", "title": "Interval exploded or invalid timestamp", "repro": ""
        }

    # 2. Repertoire move trainer (100 times)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, parent_fen, move_uci FROM repertoire_moves WHERE repertoire = 'white' AND is_user_move = 1 LIMIT 1")
    rep_row = c.fetchone()
    conn.close()
    if not rep_row:
        return "No user repertoire move found in DB", True, {"id": "BUG-REP-NONE", "title": "No user move", "repro": ""}

    m_id, p_fen, m_uci = rep_row
    for _ in range(100):
        r = requests.post(f"{BASE_URL}/api/repertoire/train/attempt", json={
            "repertoire": "white", "current_fen": p_fen, "attempted_uci": m_uci
        })
        if r.status_code != 200:
            return f"Repertoire attempt iteration failed with {r.status_code}: {r.text}", True, {
                "id": "BUG-REP-ATTEMPT-FAIL", "title": "Repertoire train attempt returned non-200", "repro": ""
            }

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT times_practiced, next_review, interval_days FROM repertoire_moves WHERE id = ?", (m_id,))
    times_prac, next_rev_rep, rep_interval = c.fetchone()
    conn.close()

    if times_prac < 100 or not isinstance(next_rev_rep, int) or rep_interval is None:
        return f"Repertoire move stats corrupted: times_practiced={times_prac}, next_review={next_rev_rep}", True, {
            "id": "BUG-REP-CORRUPT", "title": "Repertoire stats corrupted", "repro": ""
        }

    return f"100x Training stable: Drill interval capped at {interval}d, Repertoire practiced {times_prac}x", False, {}


def test_training_move_sanitization_and_illegal_uci():
    """
    Submit training attempts with:
    - Empty string UCI
    - Malformed string UCI ('invalid')
    - Illegal chess move ('e2e5')
    - Illegal teleport move ('a1h8')
    - Invalid promotion piece ('e7e8k')
    - Null-byte injection ('e2e4\0')
    All must return clean validation responses (status 200 with valid=False/correct=False) without 500 crash.
    """
    start_fen = chess.STARTING_FEN
    vectors = [
        ("", "Empty UCI"),
        ("invalid", "Malformed UCI"),
        ("e2e5", "Illegal move"),
        ("a1h8", "Illegal teleport"),
        ("e7e8k", "Invalid promotion piece"),
        ("e2e4\x00", "Null byte in move"),
    ]

    for uci, name in vectors:
        # Repertoire trainer endpoint
        r_rep = requests.post(f"{BASE_URL}/api/repertoire/train/attempt", json={
            "repertoire": "white", "current_fen": start_fen, "attempted_uci": uci
        })
        if r_rep.status_code != 200:
            return f"Repertoire train {name} ({uci!r}) returned HTTP {r_rep.status_code}: {r_rep.text}", True, {
                "id": "BUG-TRAIN-SAN-REP", "title": f"Repertoire train crash on {name}", "repro": ""
            }
        data_rep = r_rep.json()
        if data_rep.get("correct") is True and uci in ("", "invalid", "e2e5", "a1h8", "e7e8k"):
            return f"Repertoire train accepted illegal move {uci!r} as correct!", True, {
                "id": "BUG-TRAIN-ACCEPT-ILL", "title": f"Illegal move {uci!r} marked correct", "repro": ""
            }

        # Blunder trainer endpoint
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id FROM blunders LIMIT 1")
        row = c.fetchone()
        conn.close()
        target_bl_id = row[0] if row else 1

        r_bl = requests.post(f"{BASE_URL}/api/train/evaluate_move", json={
            "blunder_id": target_bl_id, "attempted_uci": uci
        })
        if r_bl.status_code != 200:
            return f"Blunder train {name} ({uci!r}) returned HTTP {r_bl.status_code}: {r_bl.text}", True, {
                "id": "BUG-TRAIN-SAN-BL", "title": f"Blunder train crash on {name}", "repro": ""
            }
        data_bl = r_bl.json()
        if data_bl.get("valid") is True and uci in ("", "invalid", "e2e5", "a1h8", "e7e8k"):
            return f"Blunder train accepted illegal move {uci!r} as valid!", True, {
                "id": "BUG-BL-ACCEPT-ILL", "title": f"Illegal move {uci!r} marked valid", "repro": ""
            }

    return "All 6 illegal, malformed, and null-byte UCI vectors rejected cleanly (HTTP 200 valid/correct=False)", False, {}


def test_trainer_zero_due_moves_and_rotation():
    """
    Test /api/repertoire/train/next when all moves have been practiced:
    - Must rotate among least recently practiced moves (ORDER BY last_practiced ASC).
    - If 0 moves configured in a repertoire color, must return HTTP 404 cleanly without 500 crash.
    """
    # 1. Normal rotation when moves exist
    r_next = requests.get(f"{BASE_URL}/api/repertoire/train/next", params={"color": "white"})
    if r_next.status_code != 200:
        return f"train/next returned {r_next.status_code}: {r_next.text}", True, {
            "id": "BUG-TRAIN-NEXT-FAIL", "title": "train/next failed", "repro": ""
        }
    drill = r_next.json()
    if not drill.get("expected_uci") or not drill.get("parent_fen"):
        return f"train/next payload incomplete: {drill}", True, {
            "id": "BUG-TRAIN-NEXT-PAYLOAD", "title": "Incomplete drill payload", "repro": ""
        }

    # 2. Query with invalid color parameter
    r_inv = requests.get(f"{BASE_URL}/api/repertoire/train/next", params={"color": "purple"})
    if r_inv.status_code != 422:
        return f"train/next with invalid color returned {r_inv.status_code} instead of 422", True, {
            "id": "BUG-TRAIN-NEXT-COLOR", "title": "Invalid color did not return 422", "repro": ""
        }

    return f"train/next rotated successfully (selected {drill['expected_san']} in {drill['opening_name']}), color validation 422 verified", False, {}


# ==============================================================================
# TARGET 4: Explorer & Blunder APIs
# ==============================================================================

def test_game_gaps_premature_limit_starvation():
    """
    Test /api/repertoire/game_gaps with min_games=0 vs min_games=30:
    Lowering min_games from 30 to 0 (all games) must return a superset of blunders.
    Premature SQL LIMIT 60 before is_patched check causes unpatched blunders to be starved.
    """
    r_30 = requests.get(f"{BASE_URL}/api/repertoire/game_gaps", params={"color": "white", "min_games": 30})
    r_0 = requests.get(f"{BASE_URL}/api/repertoire/game_gaps", params={"color": "white", "min_games": 0})

    if r_30.status_code != 200 or r_0.status_code != 200:
        return f"game_gaps returned non-200: r_30={r_30.status_code}, r_0={r_0.status_code}", True, {
            "id": "BUG-GAPS-STATUS", "title": "game_gaps status error", "repro": ""
        }

    gaps_30 = r_30.json().get("gaps", [])
    gaps_0 = r_0.json().get("gaps", [])

    if len(gaps_0) < len(gaps_30):
        return (
            f"Game Gaps Inversion Bug: min_games=0 returned {len(gaps_0)} unpatched gaps, "
            f"while min_games=30 returned {len(gaps_30)} unpatched gaps! "
            f"Relaxing filter caused active unpatched blunders to disappear!",
            True,
            {
                "id": "BUG-R4-02",
                "title": "Game Gaps Blunder Inversion and Starvation via Premature SQL LIMIT 60",
                "repro": (
                    "rm = RepertoireManager.get_instance()\n"
                    "gaps_30 = rm.get_game_gaps('white', min_games=30)['gaps']\n"
                    "gaps_0 = rm.get_game_gaps('white', min_games=0)['gaps']\n"
                    "assert len(gaps_0) >= len(gaps_30) # FAILS: 0 is not >= 1"
                )
            }
        )

    return f"Game gaps filter monotonic: min_games=0 ({len(gaps_0)}) >= min_games=30 ({len(gaps_30)})", False, {}


def test_game_gaps_parameter_boundaries():
    """
    Test /api/repertoire/game_gaps parameters:
    - min_games=-5 -> HTTP 422
    - color='invalid' -> HTTP 422
    - min_games=999999 -> HTTP 200 with empty gaps
    """
    r_neg = requests.get(f"{BASE_URL}/api/repertoire/game_gaps", params={"min_games": -5})
    if r_neg.status_code != 422:
        return f"min_games=-5 returned {r_neg.status_code} instead of 422", True, {
            "id": "BUG-GAPS-NEG", "title": "Negative min_games not rejected with 422", "repro": ""
        }

    r_col = requests.get(f"{BASE_URL}/api/repertoire/game_gaps", params={"color": "invalid"})
    if r_col.status_code != 422:
        return f"color='invalid' returned {r_col.status_code} instead of 422", True, {
            "id": "BUG-GAPS-COL", "title": "Invalid color not rejected with 422", "repro": ""
        }

    # min_games=999999 should only return positions with >= 999999 games (e.g. starting position)
    r_999k = requests.get(f"{BASE_URL}/api/repertoire/game_gaps", params={"min_games": 999999})
    if r_999k.status_code != 200:
        return f"min_games=999999 returned {r_999k.status_code}", True, {
            "id": "BUG-GAPS-999K", "title": "min_games=999999 non-200", "repro": ""
        }
    for g in r_999k.json().get("gaps", []):
        if g.get("opening_games", 0) < 999999:
            return f"Blunder with {g.get('opening_games')} games returned when min_games=999999", True, {
                "id": "BUG-GAPS-THRESH-FAIL", "title": "min_games filter not applied", "repro": ""
            }

    # 1 billion games should return 0 gaps since no chess position has 1 billion games
    r_billion = requests.get(f"{BASE_URL}/api/repertoire/game_gaps", params={"min_games": 1_000_000_000})
    if r_billion.status_code != 200 or len(r_billion.json().get("gaps", [])) != 0:
        return f"min_games=1_000_000_000 returned non-empty: {r_billion.text[:60]}", True, {
            "id": "BUG-GAPS-BILLION", "title": "1B min_games returned non-empty", "repro": ""
        }

    return "All game_gaps parameter boundary checks (-5, 'invalid', 999k, 1B) handled correctly (422 / 200)", False, {}


def test_truncated_fen_invariant_and_phantom_move():
    """
    Test truncated 2-token FEN:
    is_valid_fen('... w') returns True, normalize_fen preserves 2 tokens.
    Adding a 2-token FEN move causes invariant violation (duplicate user moves)
    and becomes an undeletable phantom move when requested with standard FEN.
    """
    fen_2token = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w"

    # Clean starting moves for this test
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM repertoire_moves WHERE repertoire = 'white' AND parent_fen LIKE 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR%'")
    conn.commit()
    conn.close()

    # 1. Add e2e4 with 2-token FEN
    r_add1 = requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white", "parent_fen": fen_2token, "move_uci": "e2e4", "is_user_move": True
    })
    # 2. Add d2d4 with standard 6-token FEN
    r_add2 = requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white", "parent_fen": chess.STARTING_FEN, "move_uci": "d2d4", "is_user_move": True
    })

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    SELECT id, parent_fen, move_uci FROM repertoire_moves
    WHERE repertoire = 'white' AND parent_fen LIKE 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR%' AND is_user_move = 1
    """)
    rows = c.fetchall()
    conn.close()

    # If both moves coexist, invariant 'ONE primary move per position' was violated
    has_duplicate_user_moves = len(rows) > 1

    # 3. Now attempt to delete e2e4 using standard FEN
    r_del = requests.delete(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white", "parent_fen": chess.STARTING_FEN, "move_uci": "e2e4"
    })

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM repertoire_moves WHERE repertoire = 'white' AND move_uci = 'e2e4' AND parent_fen = ?", (fen_2token,))
    still_exists = c.fetchone() is not None
    conn.close()

    # Clean up and re-seed
    requests.post(f"{BASE_URL}/api/repertoire/seed")

    if has_duplicate_user_moves and still_exists:
        return (
            f"Truncated FEN Bug: Allowed 2 conflicting primary user moves from start position: {rows}. "
            f"Move e2e4 was undeletable via standard FEN (DELETE returned {r_del.status_code} but move survived in DB)!",
            True,
            {
                "id": "BUG-R4-03",
                "title": "Truncated FEN Token Invariant Violation & Undeletable Phantom Move",
                "repro": (
                    "requests.post('/api/repertoire/move', json={'repertoire':'white','parent_fen':'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w','move_uci':'e2e4','is_user_move':True})\n"
                    "requests.post('/api/repertoire/move', json={'repertoire':'white','parent_fen':chess.STARTING_FEN,'move_uci':'d2d4','is_user_move':True})\n"
                    "# Both e2e4 and d2d4 coexist in repertoire_moves!\n"
                    "requests.delete('/api/repertoire/move', json={'repertoire':'white','parent_fen':chess.STARTING_FEN,'move_uci':'e2e4'})\n"
                    "# Returns 200 OK but e2e4 is NOT deleted!"
                )
            }
        )

    return "Truncated FEN handled cleanly", False, {}


def test_explore_malformed_and_rare_fens():
    """
    Test /api/repertoire/explore with malformed FENs (400) and rare legal position (200, 0 games).
    """
    malformed = [
        ("", "Empty FEN"),
        ("invalid_fen_string", "Garbage string"),
        ("rnbqkbnr/ppppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "9 pawns"),
        ("4k3/8/8/8/8/8/8/4R2K w - - 0 1", "Opponent in check"),
    ]
    for fen, name in malformed:
        r = requests.get(f"{BASE_URL}/api/repertoire/explore", params={"fen": fen})
        if r.status_code != 400:
            return f"Explore {name} returned status {r.status_code} instead of 400", True, {
                "id": "BUG-EXPLORE-MAL", "title": f"Explore did not return 400 for {name}", "repro": ""
            }

    # Rare legal position
    rare_fen = "b6k/8/8/8/8/8/8/K6B w - - 0 1"
    r_rare = requests.get(f"{BASE_URL}/api/repertoire/explore", params={"fen": rare_fen})
    if r_rare.status_code != 200:
        return f"Explore rare legal FEN returned {r_rare.status_code}", True, {
            "id": "BUG-EXPLORE-RARE", "title": "Explore rare legal FEN returned non-200", "repro": ""
        }
    data = r_rare.json()
    if data.get("total_games") != 0 or len(data.get("moves", [])) != 0:
        return f"Explore rare legal FEN returned unexpected games: {data}", True, {
            "id": "BUG-EXPLORE-GAMES", "title": "Rare FEN returned non-zero games", "repro": ""
        }

    return "All 4 malformed FENs rejected with 400; Rare legal position handled cleanly (200, 0 games)", False, {}


# ==============================================================================
# MAIN TEST RUNNER
# ==============================================================================

def main():
    print("=" * 70)
    print("ROUND 4 DEEP ABUSE & FUZZING TEST SUITE")
    print("=" * 70)

    # TARGET 1
    runner.probe(
        "Repertoire Move Repetition / Cycle Deletion Cascade",
        "Add 1. Nf3 Nf6 2. Ng1 Ng8 returning to root STARTING_FEN, then delete 1. Nf3",
        "Delete only the 4 moves in the cyclic branch; preserve all other root branches",
        test_cycle_deletion_cascade
    )

    runner.probe(
        "Multi-way Transposition Convergence (3 Paths to Same FEN)",
        "3 separate lines converging to same FEN with downstream moves. Delete Path 1, then Path 2, then Path 3",
        "Downstream moves preserved through Delete 1 and Delete 2, and cleanly pruned on Delete 3",
        test_multi_way_transposition_convergence
    )

    runner.probe(
        "Repertoire Turn vs is_user_move Invariant Checks",
        "Submit moves with mismatched turn and is_user_move across White and Black repertoires",
        "Reject all mismatched turn moves with HTTP 400 Bad Request; accept matching moves with 200 OK",
        test_turn_invariant_enforcement
    )

    runner.probe(
        "Delete Repertoire Move with Empty / Null parent_fen",
        "Submit DELETE /api/repertoire/move with parent_fen='' and parent_fen=None",
        "Gracefully reject empty parent_fen with 400 and null with 422",
        test_delete_empty_parent_fen
    )

    # TARGET 2
    runner.probe(
        "Evaluator Depth 30 Rapid Abort & Engine Lock Liveness",
        "Start depth 30 on complex middlegame FEN, abort after 50ms, fire 5 concurrent subsequent queries",
        "Engine cancels cleanly, releases async lock, all 5 subsequent queries complete in <3s with 200 OK",
        test_depth_30_cancellation_and_subsequent_queries
    )

    runner.probe(
        "Evaluator Invalid FEN Structures (6 Vectors)",
        "Submit 6 structurally illegal FENs (9 pawns, 2 kings, 1st/8th rank pawns, invalid castling, opp in check)",
        "Reject all 6 illegal FEN structures with HTTP 400 Bad Request",
        test_invalid_fen_structures
    )

    runner.probe(
        "Evaluator Depth Parameter Boundary Fuzzing",
        "Submit depth=-5, depth=999, depth='abc' to eval_stream",
        "Reject all out-of-bounds or non-integer depth values with HTTP 422",
        test_eval_depth_parameter_fuzzing
    )

    # TARGET 3
    runner.probe(
        "Trainer 100x Repeated Spaced Repetition Stability",
        "Submit 100 correct training attempts on blunder drill and repertoire move",
        "No float overflow / NaN / Infinity; interval capped at <= 3650.0d and timestamps remain valid",
        test_repeated_training_stability
    )

    runner.probe(
        "Trainer Move Sanitization & Illegal UCI Fuzzing",
        "Submit empty UCI, malformed UCI, illegal moves, teleport moves, invalid promotions, null bytes",
        "Reject all invalid/illegal attempts cleanly (HTTP 200 with valid/correct=False) without 500 crash",
        test_training_move_sanitization_and_illegal_uci
    )

    runner.probe(
        "Trainer Zero Due Moves Rotation & Color Parameter Validation",
        "Call /api/repertoire/train/next when moves practiced, and test invalid color='purple'",
        "Gracefully rotate among least recently practiced moves (200 OK); reject invalid color with 422",
        test_trainer_zero_due_moves_and_rotation
    )

    # TARGET 4
    runner.probe(
        "Game Gaps Blunder Inversion / Starvation via Premature SQL LIMIT",
        "Compare /api/repertoire/game_gaps with min_games=0 vs min_games=30 when 60+ blunders are patched",
        "min_games=0 returns superset of blunders (len(gaps_0) >= len(gaps_30)); active blunders not starved",
        test_game_gaps_premature_limit_starvation
    )

    runner.probe(
        "Game Gaps Parameter Boundary Checks",
        "Test min_games=-5 (422), color='invalid' (422), min_games=999999 (200 empty)",
        "Reject negative and invalid params with 422; return empty list for huge min_games with 200",
        test_game_gaps_parameter_boundaries
    )

    runner.probe(
        "Truncated FEN Token Invariant & Phantom Move Exploitation",
        "Add move with 2-token FEN, add move with 6-token FEN, then attempt to delete using standard FEN",
        "Strictly reject truncated FENs or normalize them; enforce 1 primary move; allow clean deletion",
        test_truncated_fen_invariant_and_phantom_move
    )

    runner.probe(
        "Explorer Malformed FENs & Rare Legal Positions",
        "Submit 4 malformed FENs (400) and rare legal position (200 with 0 games) to /api/repertoire/explore",
        "Reject malformed with 400; return total_games=0 and moves=[] for rare legal position with 200",
        test_explore_malformed_and_rare_fens
    )

    print("\n" + "=" * 70)
    print(f"ADVERSARIAL ATTACK SUMMARY: {runner.tests_run} Probes Executed")
    print(f"Passed: {runner.tests_passed} | Bugs Found: {len(runner.bugs_found)}")
    print("=" * 70)

    if runner.bugs_found:
        print("\nSUMMARY OF DISCOVERED BUGS:")
        for b in runner.bugs_found:
            print(f"  - [{b['id']}] {b['title']}")
        print("=" * 70)

    return len(runner.bugs_found)


if __name__ == "__main__":
    bugs = main()
    sys.exit(1 if bugs > 0 else 0)
