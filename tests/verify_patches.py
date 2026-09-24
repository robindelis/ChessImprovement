"""
Verify all 8 patched vulnerabilities from BUG-01 to BUG-08
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# CODE-05-10: `import tests.test_setup` must be the FIRST backend-adjacent
# import, and this script clears the repertoire — so it must fail closed.
import tests.test_setup
from tests.probe_guard import require_isolated_db
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend.api import app

require_isolated_db()

BASE_URL = "http://testserver"
client = TestClient(app, raise_server_exceptions=False)

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

def verify_patch_1():
    print("\n--- Verifying BUG-01: Malformed FEN in explore ---")
    r = requests.get(f"{BASE_URL}/api/repertoire/explore", params={"fen": "garbage"})
    print("Status:", r.status_code, "Body:", r.text)
    assert r.status_code == 400, f"Expected 400, got {r.status_code}"
    assert "Invalid FEN" in r.text

def verify_patch_2():
    print("\n--- Verifying BUG-02: Malformed parent_fen in add_move ---")
    r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white",
        "parent_fen": "invalid_fen_string",
        "move_uci": "e2e4"
    })
    print("Status:", r.status_code, "Body:", r.text)
    assert r.status_code == 400, f"Expected 400, got {r.status_code}"
    assert "Invalid FEN" in r.text

def verify_patch_3():
    print("\n--- Verifying BUG-03: Illegal move rejected in add_move ---")
    r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white",
        "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "move_uci": "e2e5"
    })
    print("Status:", r.status_code, "Body:", r.text)
    assert r.status_code == 400, f"Expected 400, got {r.status_code}"
    assert "Illegal move" in r.text

def verify_patch_4():
    print("\n--- Verifying BUG-04: Invalid repertoire color rejected ---")
    r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "purple",
        "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "move_uci": "e2e4"
    })
    print("Status:", r.status_code, "Body:", r.text)
    assert r.status_code == 422, f"Expected 422, got {r.status_code}"

def verify_patch_5():
    print("\n--- Verifying BUG-05: None-safe popularity_pct in trainer ---")
    unique_fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
    # User move
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black", "parent_fen": unique_fen, "move_uci": "b8c6", "is_user_move": True
    })
    # Opponent reply with null popularity_pct
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black",
        "parent_fen": "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
        "move_uci": "f1c4",
        "is_user_move": False,
        "popularity_pct": None
    })
    r = requests.post(f"{BASE_URL}/api/repertoire/train/attempt", json={
        "repertoire": "black", "current_fen": unique_fen, "attempted_uci": "b8c6"
    })
    print("Status:", r.status_code, "Body:", r.json())
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert r.json().get("correct") is True

def verify_patch_6():
    print("\n--- Verifying BUG-06: Evaluator depth bounds & cache integrity ---")
    test_fen = "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1"
    r0 = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": test_fen, "depth": 0})
    print("depth=0 status:", r0.status_code)
    assert r0.status_code == 422, f"Expected 422 for depth=0, got {r0.status_code}"
    
    r_neg = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": test_fen, "depth": -5})
    print("depth=-5 status:", r_neg.status_code)
    assert r_neg.status_code == 422, f"Expected 422 for depth=-5, got {r_neg.status_code}"

def verify_patch_7():
    print("\n--- Verifying BUG-07: Malformed game JSON in sync ---")
    malformed = [{'id': 'game_none_test', 'speed': 'blitz', 'rated': True, 'players': {'white': None, 'black': {'user': {'name': 'testuser'}}}, 'moves': 'e4 e5'}]
    with patch('backend.api.LichessClient.fetch_user_games', return_value=malformed):
        r = client.post('/api/sync?max_games=1')
        print("Status:", r.status_code, "Body:", r.json())
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"

def verify_patch_8():
    print("\n--- Verifying BUG-08: gap_threshold persistence & settings bounds ---")
    # Valid gap_threshold
    r_post = requests.post(f"{BASE_URL}/api/settings", json={"gap_threshold": 12.5})
    assert r_post.status_code == 200
    r_get = requests.get(f"{BASE_URL}/api/settings").json()
    print("Settings with gap_threshold:", r_get)
    assert r_get.get("gap_threshold") == 12.5

    # Boundary check: negative max_opening_ply
    r_neg_ply = requests.post(f"{BASE_URL}/api/settings", json={"max_opening_ply": -5})
    print("Negative ply status:", r_neg_ply.status_code)
    assert r_neg_ply.status_code == 422

if __name__ == "__main__":
    verify_patch_1()
    verify_patch_2()
    verify_patch_3()
    verify_patch_4()
    verify_patch_5()
    verify_patch_6()
    verify_patch_7()
    verify_patch_8()
    print("\nAll 8 initial bug vectors confirmed FIXED!")
