import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# CODE-05-10: `import tests.test_setup` must be the FIRST backend-adjacent
# import, so the process is provably off the production database before any
# backend module can open a connection.
import tests.test_setup
from tests.probe_guard import require_isolated_db
import json
import time
from backend.api import app
from fastapi.testclient import TestClient

# Fail closed: this script issues DELETEs through the API.
require_isolated_db()

client = TestClient(app, raise_server_exceptions=False)
BASE_URL = "http://testserver"

class RequestAdapter:
    def get(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        return client.get(path, **kwargs)
    def post(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        return client.post(path, **kwargs)
    def delete(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        return client.delete(path, **kwargs)

requests = RequestAdapter()

def run_probes():
    results = {}
    print("Starting comprehensive adversarial attack probes on", BASE_URL)

    # --- CATEGORY 1: Repertoire API ---
    # 1.1 Explore with garbage FEN
    try:
        r = requests.get(f"{BASE_URL}/api/repertoire/explore", params={"fen": "garbage"})
        results["1.1_explore_garbage_fen"] = (r.status_code, r.text)
    except Exception as e:
        results["1.1_explore_garbage_fen"] = ("CRASH", str(e))

    # 1.2 Explore with incomplete board FEN
    try:
        r = requests.get(f"{BASE_URL}/api/repertoire/explore", params={"fen": "8/8/8/8/8/8/8/8"})
        results["1.2_explore_incomplete_fen"] = (r.status_code, r.text)
    except Exception as e:
        results["1.2_explore_incomplete_fen"] = ("CRASH", str(e))

    # 1.3 Explore with 2-token FEN
    try:
        r = requests.get(f"{BASE_URL}/api/repertoire/explore", params={"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w"})
        results["1.3_explore_2token_fen"] = (r.status_code, r.text)
    except Exception as e:
        results["1.3_explore_2token_fen"] = ("CRASH", str(e))

    # 1.4 Add repertoire move with garbage parent_fen
    try:
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": "white",
            "parent_fen": "garbage",
            "move_uci": "e2e4"
        })
        results["1.4_add_move_garbage_parent_fen"] = (r.status_code, r.text)
    except Exception as e:
        results["1.4_add_move_garbage_parent_fen"] = ("CRASH", str(e))

    # 1.5 Add repertoire move with 2-token parent_fen
    try:
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": "white",
            "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w",
            "move_uci": "e2e4"
        })
        results["1.5_add_move_2token_parent_fen"] = (r.status_code, r.text)
    except Exception as e:
        results["1.5_add_move_2token_parent_fen"] = ("CRASH", str(e))

    # 1.6 Add repertoire move with illegal move_uci
    try:
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": "white",
            "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "move_uci": "e2e5"
        })
        results["1.6_add_move_illegal_uci"] = (r.status_code, r.text)
    except Exception as e:
        results["1.6_add_move_illegal_uci"] = ("CRASH", str(e))

    # 1.7 Add repertoire move with pawn promotion missing piece
    try:
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": "white",
            "parent_fen": "8/4P3/8/8/8/8/8/k6K w - - 0 1",
            "move_uci": "e7e8"
        })
        results["1.7_add_move_missing_promo"] = (r.status_code, r.text)
    except Exception as e:
        results["1.7_add_move_missing_promo"] = ("CRASH", str(e))

    # 1.8 Add repertoire move with invalid repertoire color
    try:
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
            "repertoire": "purple",
            "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "move_uci": "e2e4"
        })
        results["1.8_add_move_invalid_repertoire"] = (r.status_code, r.text)
    except Exception as e:
        results["1.8_add_move_invalid_repertoire"] = ("CRASH", str(e))

    # 1.9 Tree endpoint with invalid color
    try:
        r = requests.get(f"{BASE_URL}/api/repertoire/tree", params={"color": "purple"})
        results["1.9_tree_invalid_color"] = (r.status_code, r.text)
    except Exception as e:
        results["1.9_tree_invalid_color"] = ("CRASH", str(e))

    # --- CATEGORY 2: Training Engine ---
    # 2.1 Non-existent blunder ID
    try:
        r = requests.post(f"{BASE_URL}/api/train/evaluate_move", json={
            "blunder_id": 999999,
            "attempted_uci": "e2e4"
        })
        results["2.1_train_nonexistent_blunder"] = (r.status_code, r.text)
    except Exception as e:
        results["2.1_train_nonexistent_blunder"] = ("CRASH", str(e))

    # 2.2 Evaluate move with empty attempted_uci
    try:
        r = requests.post(f"{BASE_URL}/api/train/evaluate_move", json={
            "blunder_id": 1,
            "attempted_uci": ""
        })
        results["2.2_train_empty_uci"] = (r.status_code, r.text)
    except Exception as e:
        results["2.2_train_empty_uci"] = ("CRASH", str(e))

    # 2.3 Trainer attempt with garbage FEN
    try:
        r = requests.post(f"{BASE_URL}/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": "garbage",
            "attempted_uci": "e2e4"
        })
        results["2.3_trainer_attempt_garbage_fen"] = (r.status_code, r.text)
    except Exception as e:
        results["2.3_trainer_attempt_garbage_fen"] = ("CRASH", str(e))

    # 2.4 Trainer attempt with 5-character string and garbage FEN
    try:
        r = requests.post(f"{BASE_URL}/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": "garbage",
            "attempted_uci": "e7e8q"
        })
        results["2.4_trainer_attempt_5char_garbage_fen"] = (r.status_code, r.text)
    except Exception as e:
        results["2.4_trainer_attempt_5char_garbage_fen"] = ("CRASH", str(e))

    # --- CATEGORY 3: Settings API ---
    # 3.1 Extreme negative max_opening_ply
    try:
        r = requests.post(f"{BASE_URL}/api/settings", json={"max_opening_ply": -5})
        results["3.1_settings_negative_ply"] = (r.status_code, r.text)
    except Exception as e:
        results["3.1_settings_negative_ply"] = ("CRASH", str(e))

    # 3.2 Huge blunder_threshold_cp negative
    try:
        r = requests.post(f"{BASE_URL}/api/settings", json={"blunder_threshold_cp": -999999})
        results["3.2_settings_negative_blunder_thresh"] = (r.status_code, r.text)
    except Exception as e:
        results["3.2_settings_negative_blunder_thresh"] = ("CRASH", str(e))

    # 3.3 Verify gap_threshold in settings
    try:
        r = requests.post(f"{BASE_URL}/api/settings", json={"gap_threshold": 12.5})
        r_get = requests.get(f"{BASE_URL}/api/settings")
        results["3.3_settings_gap_threshold"] = (r.status_code, r_get.text)
    except Exception as e:
        results["3.3_settings_gap_threshold"] = ("CRASH", str(e))

    # --- CATEGORY 4: Engine Evaluator ---
    # 4.1 Eval stream depth 0
    try:
        r = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "depth": 0
        }, stream=True, timeout=5)
        lines = []
        for line in r.iter_lines():
            if line:
                lines.append(line.decode('utf-8'))
            if len(lines) >= 3:
                break
        results["4.1_eval_depth_0"] = (r.status_code, "\n".join(lines))
    except Exception as e:
        results["4.1_eval_depth_0"] = ("CRASH", str(e))

    # 4.2 Eval stream depth -5
    try:
        r = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "depth": -5
        }, stream=True, timeout=5)
        lines = []
        for line in r.iter_lines():
            if line:
                lines.append(line.decode('utf-8'))
            if len(lines) >= 3:
                break
        results["4.2_eval_depth_neg5"] = (r.status_code, "\n".join(lines))
    except Exception as e:
        results["4.2_eval_depth_neg5"] = ("CRASH", str(e))

    # 4.3 Eval stream with garbage FEN
    try:
        r = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={
            "fen": "garbage",
            "depth": 10
        }, stream=True, timeout=5)
        lines = []
        for line in r.iter_lines():
            if line:
                lines.append(line.decode('utf-8'))
            if len(lines) >= 3:
                break
        results["4.3_eval_garbage_fen"] = (r.status_code, "\n".join(lines))
    except Exception as e:
        results["4.3_eval_garbage_fen"] = ("CRASH", str(e))

    for k, v in sorted(results.items()):
        print(f"=== {k} ===")
        print(f"Status: {v[0]}")
        print(f"Output: {v[1][:250]}")
        print()

if __name__ == "__main__":
    run_probes()
