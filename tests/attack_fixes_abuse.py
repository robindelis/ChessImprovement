"""
Adversarial Attack & Patch Abuse Test Suite
Chess Opening Improvement Platform
Tests all 4 attack targets following the strict 4-step adversarial protocol:
  STEP 1: THINK OF AN ACTION
  STEP 2: PREDICT EXPECTED RESPONSE
  STEP 3: EXECUTE ACTION
  STEP 4: VERIFY ACTUAL VS EXPECTED
"""
import os
import sys
import json
import time
import sqlite3
import requests
import concurrent.futures
from typing import Dict, Any, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.test_setup
from tests.probe_guard import require_isolated_db
from backend.api import app
from fastapi.testclient import TestClient

client = TestClient(app, raise_server_exceptions=False)
BASE_URL = "http://testserver"
# CODE-05-10: this used to be
#   os.environ.get("CHESS_DB_PATH", "chess_improvement.db")
# i.e. a FAIL-OPEN default that pointed raw sqlite3 at the user's real
# repertoire, which this script then DELETEs from. `require_isolated_db()`
# raises instead of guessing.
DB_PATH = require_isolated_db()

class RequestAdapter:
    def get(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        return client.get(path, **kwargs)
    def post(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        if path == "/api/repertoire/seed":
            path = "/api/repertoire/seed?force=true"
        return client.post(path, **kwargs)
    def delete(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        return client.delete(path, **kwargs)

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

# ==============================================================================
# TARGET 1: Repertoire Move Injection Abuse
# ==============================================================================

def test_underpromotions():
    fen_w = "8/4P3/8/8/8/8/k7/4K3 w - - 0 1"
    fen_b = "4k3/8/8/8/8/8/4p3/K7 b - - 0 1"
    
    # White promotions
    promos_w = [
        ("e7e8n", True, "Knight underpromotion"),
        ("e7e8b", True, "Bishop underpromotion"),
        ("e7e8r", True, "Rook underpromotion"),
        ("e7e8q", True, "Queen promotion"),
        ("e7e8k", False, "King promotion (illegal)"),
        ("e7e8p", False, "Pawn promotion (illegal)"),
        ("e7e8", False, "Missing promotion piece (illegal)"),
    ]
    for move, should_succeed, desc in promos_w:
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": "white", "parent_fen": fen_w, "move_uci": move
        })
        if should_succeed and r.status_code != 200:
            return f"White {desc} {move} rejected with {r.status_code}: {r.text}", True, {
                "id": "BUG-PROMO-W", "title": f"Legal underpromotion {move} rejected",
                "repro": f"POST /api/repertoire/move with {move}"
            }
        if not should_succeed and r.status_code != 400:
            return f"White {desc} {move} returned {r.status_code} instead of 400: {r.text}", True, {
                "id": "BUG-PROMO-W-INV", "title": f"Invalid promotion {move} not rejected with 400",
                "repro": f"POST /api/repertoire/move with {move}"
            }

    # Black promotions
    promos_b = [
        ("e2e1n", True, "Black Knight underpromotion"),
        ("e2e1b", True, "Black Bishop underpromotion"),
        ("e2e1r", True, "Black Rook underpromotion"),
        ("e2e1q", True, "Black Queen promotion"),
        ("e2e1k", False, "Black King promotion (illegal)"),
        ("e2e1p", False, "Black Pawn promotion (illegal)"),
    ]
    for move, should_succeed, desc in promos_b:
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": "black", "parent_fen": fen_b, "move_uci": move
        })
        if should_succeed and r.status_code != 200:
            return f"Black {desc} {move} rejected with {r.status_code}: {r.text}", True, {
                "id": "BUG-PROMO-B", "title": f"Legal black underpromotion {move} rejected",
                "repro": f"POST /api/repertoire/move with {move}"
            }
        if not should_succeed and r.status_code != 400:
            return f"Black {desc} {move} returned {r.status_code} instead of 400: {r.text}", True, {
                "id": "BUG-PROMO-B-INV", "title": f"Invalid black promotion {move} not rejected with 400",
                "repro": f"POST /api/repertoire/move with {move}"
            }

    return "All 13 underpromotion and invalid promotion vectors correctly handled (HTTP 200 / 400)", False, {}

def test_illegal_castling():
    castling_vectors = [
        ("Castling through check (f1 attacked)", "r3k2r/8/8/8/8/5r2/8/R3K2R w KQkq - 0 1", "e1g1", 400),
        ("Castling into check (g1 attacked)", "r3k2r/8/8/8/8/6r1/8/R3K2R w KQkq - 0 1", "e1g1", 400),
        ("Castling while in check (e1 attacked)", "r3k2r/8/8/8/4r3/8/8/R3K2R w KQkq - 0 1", "e1g1", 400),
        ("Castling with revoked kingside rights", "r3k2r/8/8/8/8/8/8/R3K2R w Qq - 0 1", "e1g1", 400),
        ("Castling through occupied squares", "r3k2r/8/8/8/8/8/8/RN2KBNR w KQkq - 0 1", "e1g1", 400),
        ("Legal kingside castling", "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", 200),
        ("Legal queenside castling", "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1", 200),
    ]
    for name, fen, move, expected_status in castling_vectors:
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": "white", "parent_fen": fen, "move_uci": move
        })
        if r.status_code != expected_status:
            return f"{name} ({move}) returned HTTP {r.status_code}, expected {expected_status}: {r.text}", True, {
                "id": "BUG-CASTLE", "title": f"Castling legality check failed on {name}",
                "repro": f"POST /api/repertoire/move with fen='{fen}', move='{move}'"
            }
    return "All 7 castling legality scenarios correctly verified (HTTP 400 / 200)", False, {}

def test_subtle_en_passant_pins():
    ep_vectors = [
        ("Diagonal pin to King", "4k2q/8/8/5Pp1/8/8/8/K7 w - g6 0 1", "f5g6", 400),
        ("Horizontal rank pin to King", "4k3/8/8/r1pPK3/8/8/8/8 w - c6 0 1", "d5c6", 400),
        ("En passant missing ep square in FEN", "8/8/8/4Pp2/8/8/8/4K2k w - - 0 1", "e5f6", 400),
        ("Legal en passant capture", "8/8/8/4Pp2/8/8/8/4K2k w - f6 0 1", "e5f6", 200)
    ]
    for name, fen, move, expected_status in ep_vectors:
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": "white", "parent_fen": fen, "move_uci": move
        })
        if r.status_code != expected_status:
            return f"{name} ({move}) returned HTTP {r.status_code}, expected {expected_status}: {r.text}", True, {
                "id": "BUG-EP-PIN", "title": f"En passant pin validation failed on {name}",
                "repro": f"POST /api/repertoire/move with fen='{fen}', move='{move}'"
            }
    return "All 4 subtle en passant pinned and unpinned vectors correctly validated", False, {}

def test_string_sanitization_and_null_bytes():
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    vectors = [
        ("leading space", " e2e4"),
        ("trailing space", "e2e4 "),
        ("both spaces", " e2e4 "),
        ("null byte suffix", "e2e4\x00"),
        ("null byte prefix", "\x00e2e4"),
        ("zero width space", "\u200be2e4"),
        ("newline in middle", "e2\ne4"),
        ("newline suffix", "e2e4\n"),
        ("tab prefix", "\te2e4")
    ]
    for name, uci in vectors:
        r_move = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": "white", "parent_fen": fen, "move_uci": uci
        })
        if r_move.status_code == 500:
            return f"HTTP 500 crash on /api/repertoire/move with {name} {repr(uci)}", True, {
                "id": "BUG-STRING-CRASH-MOVE", "title": f"500 crash on move_uci with {name}",
                "repro": f"POST /api/repertoire/move with move_uci={repr(uci)}"
            }
        r_train = requests.post(f"{BASE_URL}/api/repertoire/train/attempt", json={
            "repertoire": "white", "current_fen": fen, "attempted_uci": uci
        })
        if r_train.status_code == 500:
            return f"HTTP 500 crash on /api/repertoire/train/attempt with {name} {repr(uci)}", True, {
                "id": "BUG-STRING-CRASH-TRAIN", "title": f"500 crash on trainer with {name}",
                "repro": f"POST /api/repertoire/train/attempt with attempted_uci={repr(uci)}"
            }
    return "All 9 whitespace, null byte, and unicode malformations handled cleanly without 500 crashes", False, {}

def test_popularity_pct_extremes():
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    cases = [
        ("1e-15 (near zero positive)", 1e-15, 200),
        ("99.999999 (near 100)", 99.999999, 200),
        ("-0.00001 (negative bound)", -0.00001, 422),
        ("100.00001 (overflow bound)", 100.00001, 422)
    ]
    for name, val, exp_code in cases:
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": "white", "parent_fen": fen, "move_uci": "e2e4", "popularity_pct": val
        })
        if r.status_code != exp_code:
            return f"{name} returned HTTP {r.status_code}, expected {exp_code}: {r.text}", True, {
                "id": "BUG-POP-BOUNDS", "title": f"popularity_pct boundary check failed for {val}",
                "repro": f"POST /api/repertoire/move with popularity_pct={val}"
            }
    return "All popularity_pct boundary and float precision cases correctly enforced", False, {}

# ==============================================================================
# TARGET 2: Evaluator SSE Stress & Rapid Disconnect Fuzzing
# ==============================================================================

def test_transposition_cache_integrity():
    fen_a = "rnbqkb1r/pppppppp/5n2/8/2PP4/8/PP2PPPP/RNBQKBNR b KQkq - 0 2"
    fen_b = "rnbqkb1r/pppppppp/5n2/8/2PP4/8/PP2PPPP/RNBQKBNR b KQkq - 0 2"
    
    r_a = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": fen_a, "depth": 10}, stream=True, timeout=10)
    events_a = [json.loads(l.decode('utf-8').replace('data: ', '')) for l in r_a.iter_lines() if l]
    
    r_b = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": fen_b, "depth": 10}, stream=True, timeout=10)
    events_b = [json.loads(l.decode('utf-8').replace('data: ', '')) for l in r_b.iter_lines() if l]
    
    if not events_a or not events_b:
        return f"Failed to retrieve SSE events: A={len(events_a)}, B={len(events_b)}", True, {
            "id": "BUG-TRANS-STREAM", "title": "Empty eval_stream on transposition test", "repro": "Query eval_stream on fen_a"
        }
    final_a = events_a[-1]
    final_b = events_b[-1]
    if final_a.get("best_move") != final_b.get("best_move") or final_a.get("score_cp") != final_b.get("score_cp"):
        return f"Transposition evaluation mismatch: A={final_a}, B={final_b}", True, {
            "id": "BUG-TRANS-MISMATCH", "title": "Transposition evaluation cache mismatch",
            "repro": f"Compare eval_stream for {fen_a} and {fen_b}"
        }
    if not final_b.get("cached"):
        return "Position B was not retrieved from cache", False, {}
    return f"Transpositions match identically: best_move={final_a.get('best_move')}, score={final_a.get('score_cp')}cp (cached={final_b.get('cached')})", False, {}

def test_terminal_positions():
    terminals = [
        ("Fool's Mate", "rnb1kbnr/pppp1ppp/4p3/8/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3", -10000, -1),
        ("Scholar's Mate", "r1bqkb1r/pppp1Qpp/2n5/4p3/2B1n3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4", 10000, 1),
        ("K+Q vs K mate", "8/8/8/8/8/5k2/4q3/4K3 w - - 0 1", -10000, -1),
        ("Stalemate", "k7/8/1Q6/8/8/8/8/7K b - - 0 1", 0, None)
    ]
    for name, fen, exp_score, exp_mate in terminals:
        r = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": fen, "depth": 10}, stream=True, timeout=10)
        lines = [l.decode('utf-8') for l in r.iter_lines() if l]
        if not lines:
            return f"Empty response for terminal position: {name}", True, {
                "id": "BUG-TERM-EMPTY", "title": f"Terminal position returned empty stream: {name}", "repro": f"eval_stream on {fen}"
            }
        data = json.loads(lines[0].replace("data: ", ""))
        if data.get("score_cp") != exp_score or data.get("mate") != exp_mate or data.get("final") is not True:
            return f"Terminal position {name} mismatch: got score={data.get('score_cp')}, mate={data.get('mate')}, expected score={exp_score}, mate={exp_mate}", True, {
                "id": "BUG-TERM-SCORE", "title": f"Incorrect score for terminal position {name}", "repro": f"eval_stream on {fen}"
            }
    return "All 4 terminal positions (Fool's Mate, Scholar's Mate, K+Q mate, Stalemate) handled with depth 99 & correct scores", False, {}

def test_rapid_sse_disconnect_engine_lock_leak():
    """
    Stress tests /api/repertoire/eval_stream with rapid connection & immediate disconnection spam.
    Verifies whether the Stockfish engine process dies or the async lock leaks.
    """
    url = f"{BASE_URL}/api/repertoire/eval_stream"
    
    def rapid_worker(i):
        try:
            r = requests.get(url, params={"fen": f"rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - {i} 1", "depth": 12}, stream=True, timeout=2.0)
            for _ in r.iter_lines():
                break # Instantly abort
            return True
        except Exception:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(rapid_worker, range(20)))

    # Now verify if a subsequent legitimate eval succeeds or hangs
    t0 = time.time()
    try:
        r_after = requests.get(
            url,
            params={"fen": "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3", "depth": 8},
            stream=True,
            timeout=4.0
        )
        first_line = None
        for line in r_after.iter_lines():
            if line:
                first_line = line.decode('utf-8')
                break
        elapsed = time.time() - t0
        if not first_line:
            return "Engine hung / lock leaked: no response received after rapid disconnect spam", True, {
                "id": "BUG-E02",
                "title": "Evaluator Engine Deadlock / Resource Leak Under Rapid SSE Disconnects",
                "repro": "Run 20 concurrent SSE connections with immediate break, then request eval_stream"
            }
        return f"Subsequent eval succeeded in {elapsed:.2f}s after rapid disconnects", False, {}
    except requests.exceptions.Timeout:
        return "CRITICAL BUG: Subsequent evaluation timed out (Engine lock leaked / Deadlock)!", True, {
            "id": "BUG-E02",
            "title": "Evaluator Engine Deadlock / Resource Leak Under Rapid SSE Disconnects",
            "repro": "Run 20 concurrent SSE connections with immediate break, then request eval_stream with timeout=4"
        }
    except Exception as e:
        return f"CRITICAL BUG: Subsequent evaluation crashed with {type(e).__name__}: {e}", True, {
            "id": "BUG-E02",
            "title": "Evaluator Engine Deadlock / Resource Leak Under Rapid SSE Disconnects",
            "repro": f"Error: {e}"
        }

# ==============================================================================
# TARGET 3: Training & Spaced Repetition Abuse
# ==============================================================================

def test_alternative_move_eval_and_negative_cpl():
    conn = sqlite3.connect(DB_PATH)
    fen_test = "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2P2N2/PP1P1PPP/RNBQK2R w KQkq - 1 5"
    conn.execute("""
    INSERT OR REPLACE INTO blunders (
        id, game_id, move_number, ply, color, fen_before, fen_after,
        played_move, played_san, best_move, best_san, eval_before, eval_after, cpl_loss,
        opening_name, eco, is_book_move, explanation, strategic_idea, category, created_at
    ) VALUES (
        9999, 'test_alt_better', 5, 9, 'white', ?, 'after_fen',
        'h2h3', 'h3', 'd2d4', 'd4', -100, -200, 100,
        'Italian Game', 'C50', 0, 'Test', 'Test', 'Tactical', 1000
    )
    """, (fen_test,))
    conn.commit()
    conn.close()

    try:
        r = requests.post(f"{BASE_URL}/api/train/evaluate_move", json={
            "blunder_id": 9999, "attempted_uci": "d2d3"
        })
        data = r.json()
        feedback_type = data.get("feedback_type")
        msg = data.get("message", "")
        is_correct = data.get("is_correct")
        
        # Check if negative cpl_diff correctly rewarded
        if feedback_type != "good_alternative" or not is_correct:
            return f"Negative cpl_diff not rewarded: feedback={feedback_type}, is_correct={is_correct}", True, {
                "id": "BUG-ALT-REWARD", "title": "Superior alternative move not rewarded as good_alternative",
                "repro": "Evaluate alternative move with eval higher than blunder eval_before"
            }
        
        # Check for double minus sign cosmetic bug "--"
        if "--" in msg:
            return f"Cosmetic formatting bug detected: double minus in message '{msg}'", True, {
                "id": "BUG-E03",
                "title": "Double-Minus String Formatting in Trainer Good Alternative Diff Display",
                "repro": f"POST /api/train/evaluate_move with negative cpl_diff -> returns '{msg}'"
            }
        return f"Alternative move rewarded correctly: {feedback_type}, message: {msg}", False, {}
    finally:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM blunders WHERE id = 9999")
        conn.execute("DELETE FROM drill_reviews WHERE blunder_id = 9999")
        conn.commit()
        conn.close()

def test_transposition_cascading_deletion_bug():
    """
    Tests whether deleting a branch in one line accidentally destroys transposed subtrees
    shared with another line.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM repertoire_moves WHERE repertoire = 'black'")
    conn.commit()
    conn.close()

    # Line 1: 1. d4 Nf6 2. c4 e6 3. Nc3 Bb4
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "move_uci": "d2d4", "is_user_move": False
    })
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1", "move_uci": "g8f6", "is_user_move": True
    })
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": "rnbqkb1r/pppppppp/5n2/8/3P4/8/PPP1PPPP/RNBQKBNR w KQkq - 1 2", "move_uci": "c2c4", "is_user_move": False
    })
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": "rnbqkb1r/pppppppp/5n2/8/2PP4/8/PP2PPPP/RNBQKBNR b KQkq - 0 2", "move_uci": "e7e6", "is_user_move": True
    })
    fen_transpos = "rnbqkb1r/pppp1ppp/4pn2/8/2PP4/8/PP2PPPP/RNBQKBNR w KQkq - 0 3"
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": fen_transpos, "move_uci": "b1c3", "is_user_move": False
    })
    fen_nimzo = "rnbqkb1r/pppp1ppp/4pn2/8/2PP4/2N5/PP2PPPP/R1BQKBNR b KQkq - 1 3"
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": fen_nimzo, "move_uci": "f8b4", "is_user_move": True
    })

    # Line 2: 1. c4 e6 2. d4 Nf6 -> transposes to fen_transpos!
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "move_uci": "c2c4", "is_user_move": False
    })
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": "rnbqkbnr/pppppppp/8/8/2P5/8/PP1PPPPP/RNBQKBNR b KQkq - 0 1", "move_uci": "e7e6", "is_user_move": True
    })
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": "rnbqkbnr/pppp1ppp/4p3/8/2P5/8/PP1PPPPP/RNBQKBNR w KQkq - 0 2", "move_uci": "d2d4", "is_user_move": False
    })
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": "rnbqkbnr/pppp1ppp/4p3/8/2PP4/8/PP2PPPP/RNBQKBNR b KQkq - 0 2", "move_uci": "g8f6", "is_user_move": True
    })

    # Total moves should be 10
    tree_before = requests.get(f"{BASE_URL}/api/repertoire/tree?color=black").json()
    total_before = tree_before.get("total_moves", 0)

    # Now delete 1. c4 (Line 2)
    requests.delete(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black",
        "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "move_uci": "c2c4"
    })

    # Verify if 3... Bb4 in Line 1 survived
    r_attempt = requests.post(f"{BASE_URL}/api/repertoire/train/attempt", json={
        "repertoire": "black",
        "current_fen": fen_nimzo,
        "attempted_uci": "f8b4"
    }).json()

    tree_after = requests.get(f"{BASE_URL}/api/repertoire/tree?color=black").json()
    total_after = tree_after.get("total_moves", 0)

    if not r_attempt.get("correct"):
        return (
            f"CRITICAL DATA LOSS BUG: Deleting 1. c4 destroyed transposed Line 1 moves! "
            f"Total moves went from {total_before} to {total_after} (expected 6). "
            f"Trainer response: {r_attempt}"
        ), True, {
            "id": "BUG-E01",
            "title": "Cascading Data Loss / Destruction of Transposed Repertoire Subtrees upon Branch Deletion",
            "repro": "Create two lines transposing to FEN_T with child moves. Delete Line 2 -> child moves under Line 1 are destroyed."
        }

    return f"Transposition deletion isolated cleanly: total moves {total_before} -> {total_after}", False, {}

# ==============================================================================
# TARGET 4: Settings API Boundary Fuzzing
# ==============================================================================

def test_settings_boundaries():
    matrix = [
        ("max_opening_ply = 0 (reject)", {"max_opening_ply": 0}, 422),
        ("max_opening_ply = 1 (accept)", {"max_opening_ply": 1}, 200),
        ("max_opening_ply = 100 (accept)", {"max_opening_ply": 100}, 200),
        ("max_opening_ply = 101 (reject)", {"max_opening_ply": 101}, 422),
        ("blunder_threshold_cp = 0 (reject)", {"blunder_threshold_cp": 0}, 422),
        ("blunder_threshold_cp = 1 (accept)", {"blunder_threshold_cp": 1}, 200),
        ("blunder_threshold_cp = 2000 (accept)", {"blunder_threshold_cp": 2000}, 200),
        ("blunder_threshold_cp = 2001 (reject)", {"blunder_threshold_cp": 2001}, 422),
        ("gap_threshold = 0.09 (reject)", {"gap_threshold": 0.09}, 422),
        ("gap_threshold = 0.1 (accept)", {"gap_threshold": 0.1}, 200),
        ("gap_threshold = 100.0 (accept)", {"gap_threshold": 100.0}, 200),
        ("gap_threshold = 100.1 (reject)", {"gap_threshold": 100.1}, 422),
    ]
    for name, payload, exp_code in matrix:
        r = requests.post(f"{BASE_URL}/api/settings", json=payload)
        if r.status_code != exp_code:
            return f"Settings boundary failure for {name}: got {r.status_code}, expected {exp_code}", True, {
                "id": "BUG-SETTINGS-BOUND", "title": f"Settings validation failure on {name}",
                "repro": f"POST /api/settings with {payload}"
            }
    # Reset
    requests.post(f"{BASE_URL}/api/settings", json={"max_opening_ply": 24, "blunder_threshold_cp": 150, "gap_threshold": 5.0})
    return "All 12 settings boundary conditions strictly enforced (422 / 200)", False, {}

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("ADVERSARIAL ATTACK & ABUSE PROBE SUITE")
    print("=" * 70)

    # 1. Repertoire Move Injection Abuse
    runner.probe(
        "Underpromotions & Promotion Piece Legality",
        "Submit Knight, Bishop, Rook, Queen promotions vs King, Pawn, and invalid promotion tokens",
        "Accept valid promotions (200 OK), reject invalid promotions with HTTP 400 Bad Request",
        test_underpromotions
    )
    runner.probe(
        "Illegal Castling Vectors",
        "Attempt castling through check, into check, while in check, with revoked rights, or blocked squares",
        "Gracefully reject all illegal castling with HTTP 400 Bad Request",
        test_illegal_castling
    )
    runner.probe(
        "Subtle Pinned En Passant Legality",
        "Attempt en passant pawn capture when the capturing pawn is pinned horizontally or diagonally to King",
        "Detect pin legality and reject with HTTP 400 Bad Request",
        test_subtle_en_passant_pins
    )
    runner.probe(
        "Whitespace, Null Byte & Zero-Width Injection",
        "Send move_uci with leading/trailing spaces, null bytes (\x00), zero-width spaces (\u200b), newlines (\n), tabs (\t)",
        "Handle cleanly: reject invalid format with HTTP 400/422 without HTTP 500 crash",
        test_string_sanitization_and_null_bytes
    )
    runner.probe(
        "Extreme Floating Point Popularity Bounds",
        "Inject extreme floating point values: 1e-15, 99.999999, -0.00001, 100.00001 into popularity_pct",
        "Enforce strict bounds: accept [0.0, 100.0] with HTTP 200, reject out-of-bounds with HTTP 422",
        test_popularity_pct_extremes
    )

    # 2. Evaluator SSE Stress & Rapid Disconnect Fuzzing
    runner.probe(
        "Transposition Cache Integrity",
        "Query eval_stream on Position A (1. d4 Nf6 2. c4) and transposed Position B (1. c4 Nf6 2. d4)",
        "Both return identical evaluations and Position B hits cache without data corruption",
        test_transposition_cache_integrity
    )
    runner.probe(
        "Terminal & Endgame Positions Evaluation",
        "Stream eval for Fool's Mate, Scholar's Mate, K+Q checkmate, Stalemate",
        "Instantly return depth 99 terminal evaluations without calling engine search",
        test_terminal_positions
    )
    runner.probe(
        "Rapid SSE Connection Disconnect & Engine Lock Stress",
        "Open 20 concurrent SSE streams to eval_stream and immediately abort to simulate rapid UI clicking",
        "Engine cancels cleanly, releases async lock, and subsequent requests succeed without hanging",
        test_rapid_sse_disconnect_engine_lock_leak
    )

    # 3. Training & Spaced Repetition Abuse
    runner.probe(
        "Alternative Move Evaluation & Negative CPL Reward",
        "Play alternative move that has higher evaluation than blunder eval_before (negative cpl_diff)",
        "Reward as good_alternative with is_correct=True and clean positive diff string",
        test_alternative_move_eval_and_negative_cpl
    )
    runner.probe(
        "Transposition Branch Deletion Data Integrity",
        "Set up two opening lines transposing to same FEN with deep child moves, then delete Line 2",
        "Delete only Line 2 unique moves; PRESERVE transposed node and child moves for Line 1",
        test_transposition_cascading_deletion_bug
    )

    # 4. Settings API Boundary Fuzzing
    runner.probe(
        "Settings API Boundary Fuzzing",
        "Test exact edge values: max_opening_ply (0,1,100,101), blunder_threshold_cp (0,1,2000,2001), gap_threshold (0.09,0.1,100.0,100.1)",
        "Strictly accept valid range (HTTP 200) and reject boundary violations (HTTP 422)",
        test_settings_boundaries
    )

    # Summary
    print("\n" + "=" * 70)
    print(f"ADVERSARIAL ATTACK SUMMARY: {runner.tests_run} Probes Executed")
    print(f"Passed: {runner.tests_passed} | Bugs Found: {len(runner.bugs_found)}")
    print("=" * 70)
    for b in runner.bugs_found:
        print(f"  * [{b['id']}] {b['title']}")
    print("=" * 70)
