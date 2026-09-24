"""
Verification Suite for Confirmed Bugs 1-8 (Defender Fix Verification)
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

def repro_bug_1():
    print("\n--- Bug 1: Explore Malformed FEN (HTTP 400 Bad Request) ---")
    r = requests.get(f"{BASE_URL}/api/repertoire/explore", params={"fen": "garbage"})
    print("Status:", r.status_code)
    print("Body:", r.text[:100])
    assert r.status_code == 400, f"Expected 400, got {r.status_code}"
    assert "Invalid FEN string" in r.text, f"Expected 'Invalid FEN string' in response, got {r.text}"
    print("✓ Bug 1 Verified: Malformed FEN returns HTTP 400")

def repro_bug_2():
    print("\n--- Bug 2: Add Move Malformed parent_fen (HTTP 400 Bad Request) ---")
    r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white",
        "parent_fen": "invalid_fen_string",
        "move_uci": "e2e4"
    })
    print("Status:", r.status_code)
    print("Body:", r.text[:100])
    assert r.status_code == 400, f"Expected 400, got {r.status_code}"
    assert "Invalid FEN string" in r.text, f"Expected 'Invalid FEN string' in response, got {r.text}"
    print("✓ Bug 2 Verified: Invalid parent_fen returns HTTP 400")

def repro_bug_3():
    print("\n--- Bug 3: Add Illegal Move Rejected with HTTP 400 ---")
    r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white",
        "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "move_uci": "e2e5" # Illegal move
    })
    print("Status:", r.status_code)
    print("Body:", r.text[:100])
    assert r.status_code == 400, f"Expected 400, got {r.status_code}"
    assert "Illegal move" in r.text, f"Expected 'Illegal move' in response, got {r.text}"
    print("✓ Bug 3 Verified: Illegal move rejected with HTTP 400")

def repro_bug_4():
    print("\n--- Bug 4: Invalid Repertoire Color Rejected with HTTP 422/400 ---")
    r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "purple",
        "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "move_uci": "e2e4"
    })
    print("Status:", r.status_code)
    print("Body:", r.text[:100])
    assert r.status_code in (400, 422), f"Expected 400 or 422, got {r.status_code}"
    print("✓ Bug 4 Verified: Invalid repertoire color rejected with HTTP 422/400")

def repro_bug_5():
    print("\n--- Bug 5: Trainer Crash on Opponent Move with NULL popularity_pct (Resolved HTTP 200) ---")
    # Insert user move first
    unique_fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black",
        "parent_fen": unique_fen,
        "move_uci": "b8c6",
        "is_user_move": True
    })
    # Insert opponent reply with null popularity_pct
    requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "black",
        "parent_fen": "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
        "move_uci": "f1c4",
        "is_user_move": False,
        "popularity_pct": None
    })
    # User plays their prepared move
    r = requests.post(f"{BASE_URL}/api/repertoire/train/attempt", json={
        "repertoire": "black",
        "current_fen": unique_fen,
        "attempted_uci": "b8c6"
    })
    print("Status:", r.status_code)
    print("Body:", r.text[:100])
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert r.json().get("correct") is True, f"Expected correct=True, got {r.json()}"
    print("✓ Bug 5 Verified: Trainer handles NULL popularity_pct cleanly with HTTP 200")

def repro_bug_6():
    print("\n--- Bug 6: Cache Poisoning on Aborted / depth=0 Eval Stream (Resolved) ---")
    test_fen = "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1"
    # Send depth=0, which should be rejected by parameter bounds (ge=1)
    r0 = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": test_fen, "depth": 0})
    assert r0.status_code == 422, f"Expected 422 for depth=0, got {r0.status_code}"
    
    # Now query depth=16 on that FEN
    r16 = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": test_fen, "depth": 16}, stream=True)
    first_event = None
    for line in r16.iter_lines():
        if line:
            first_event = line.decode('utf-8')
            break
    print("First event for depth=16 query:", first_event)
    data = json.loads(first_event.replace("data: ", ""))
    print("Event data:", data)
    # Ensure cache is not poisoned with shallow final=True
    assert not (data.get("final") is True and data.get("depth", 0) < 16), "Cache was poisoned!"
    print("✓ Bug 6 Verified: depth=0 rejected and cache poisoning prevented")

def repro_bug_7():
    print("\n--- Bug 7: Sync Crashes on Malformed Game JSON (Resolved) ---")
    malformed_game = [{
        'id': 'game_none_test',
        'speed': 'blitz',
        'rated': True,
        'players': {'white': None, 'black': {'user': {'name': 'testuser'}}},
        'moves': 'e4 e5'
    }]
    with patch('backend.api.LichessClient.fetch_user_games', return_value=malformed_game):
        r = client.post('/api/sync?max_games=1')
        print("Status:", r.status_code)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    print("✓ Bug 7 Verified: Sync safely processes malformed game JSON without crashing")

def repro_bug_8():
    print("\n--- Bug 8: gap_threshold silently dropped & no setting bounds (Resolved) ---")
    # Bounds check: negative settings rejected
    r_bad = requests.post(f"{BASE_URL}/api/settings", json={
        "max_opening_ply": -10,
        "blunder_threshold_cp": -500
    })
    assert r_bad.status_code == 422, f"Expected 422 for negative bounds, got {r_bad.status_code}"

    # Setting gap_threshold saved and returned
    r_post = requests.post(f"{BASE_URL}/api/settings", json={
        "gap_threshold": 9.9
    })
    assert r_post.status_code == 200, f"Expected 200, got {r_post.status_code}"
    r = requests.get(f"{BASE_URL}/api/settings").json()
    print("Settings returned:", r)
    assert r.get("gap_threshold") == 9.9, f"Expected gap_threshold=9.9, got {r.get('gap_threshold')}"
    print("✓ Bug 8 Verified: gap_threshold saved/retrieved and bounds strictly enforced")

if __name__ == "__main__":
    repro_bug_1()
    repro_bug_2()
    repro_bug_3()
    repro_bug_4()
    repro_bug_5()
    repro_bug_6()
    repro_bug_7()
    repro_bug_8()
    print("\nAll 8 bug verification checks passed successfully!")
