"""
Re-Attack Probe Script: Demonstrating New Bypasses and Vulnerabilities
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# CODE-05-10: `import tests.test_setup` must be the FIRST backend-adjacent
# import. It used to come after `from backend.db import get_db`.
import tests.test_setup
import requests
import json
import chess
from backend.db import get_db
from backend.repertoire import RepertoireManager, normalize_fen
from backend.api import app
from fastapi.testclient import TestClient

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

def test_bypass_nan_infinity_crash():
    print("\n--- BUG 9: JSON NaN/Infinity Crash Prevention (HTTP 422) ---")
    headers = {'Content-Type': 'application/json'}
    
    # 1. NaN in settings
    r1 = requests.post(f"{BASE_URL}/api/settings", data='{"gap_threshold": NaN}', headers=headers)
    print("1. Settings NaN Status:", r1.status_code)
    assert r1.status_code == 422, f"Expected 422, got {r1.status_code}"

    # 2. Infinity in settings
    r2 = requests.post(f"{BASE_URL}/api/settings", data='{"gap_threshold": Infinity}', headers=headers)
    print("2. Settings Infinity Status:", r2.status_code)
    assert r2.status_code == 422, f"Expected 422, got {r2.status_code}"

    # 3. NaN in repertoire move
    r3 = requests.post(f"{BASE_URL}/api/repertoire/move", data='''{
        "repertoire": "white",
        "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "move_uci": "e2e4",
        "popularity_pct": NaN
    }''', headers=headers)
    print("3. Repertoire Move NaN Status:", r3.status_code)
    assert r3.status_code == 422, f"Expected 422, got {r3.status_code}"
    print("✓ BUG-09 Verified: NaN/Infinity requests cleanly rejected with HTTP 422 without 500 crash.")

def test_bypass_opponent_as_user_move():
    print("\n--- BUG 10: Turn Mismatch Validation (HTTP 400) ---")
    # In White repertoire, submit a Black move with is_user_move=True
    r = requests.post(f"{BASE_URL}/api/repertoire/move", json={
        "repertoire": "white",
        "parent_fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        "move_uci": "c7c5",
        "is_user_move": True
    })
    print("Move Ingestion Status:", r.status_code, "Body:", r.json())
    assert r.status_code == 400, f"Expected 400 Bad Request, got {r.status_code}"
    assert "User moves for white repertoire must be on White's turn" in r.text, f"Unexpected body: {r.text}"

    # Clean up test move
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM repertoire_moves WHERE repertoire = 'white' AND parent_fen LIKE 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b%' AND move_uci = 'c7c5'")
    conn.commit()
    conn.close()
    print("✓ BUG-10 Verified: Turn mismatch rejected with HTTP 400 Bad Request.")

def test_bypass_annotated_san_data_loss():
    print("\n--- BUG 11: Annotated SAN Indexing without Data Loss ---")
    conn = get_db()
    c = conn.cursor()
    c.execute("""
    INSERT OR REPLACE INTO games (
        id, speed, rated, played_at, white_player, black_player,
        white_rating, black_rating, user_color, result, opening_name, eco, moves_san, analyzed_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ('probe_game_annotated', 'blitz', 1, 1000, 'TestUser', 'Opponent', 1500, 1500, 'white', 'white', 'Grob', 'A00', 'g4! e5', 1000))
    conn.commit()
    conn.close()

    rm = RepertoireManager()
    rm._user_stats_cache = {}
    stats = rm.get_user_position_stats('white')
    start_fen = normalize_fen(chess.STARTING_FEN)
    indexed = stats.get((start_fen, 'g2g4'))
    print("Indexed stats for initial move 'g4!':", indexed)
    
    # Clean up
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM games WHERE id = 'probe_game_annotated'")
    conn.commit()
    conn.close()

    assert indexed is not None, "Expected 'g4!' to be successfully indexed!"
    assert indexed.get("games") == 1, f"Expected games == 1, got {indexed.get('games')}"
    print("✓ BUG-11 Verified: Annotated SAN 'g4!' properly indexed without data loss.")

if __name__ == "__main__":
    test_bypass_nan_infinity_crash()
    test_bypass_opponent_as_user_move()
    test_bypass_annotated_san_data_loss()
    print("\nAll BUG-09, BUG-10, BUG-11 defense probes passed successfully!")
