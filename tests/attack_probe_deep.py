import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# CODE-05-10: `import tests.test_setup` must be the FIRST backend-adjacent
# import.
import tests.test_setup
from tests.probe_guard import require_isolated_db
from backend.analyzer import GameAnalyzer
from backend.db import get_db, init_db, set_setting
from backend.api import app
from fastapi.testclient import TestClient

# Fail closed: this script writes games, settings and repertoire moves.
require_isolated_db()

client = TestClient(app, raise_server_exceptions=False)
BASE_URL = "http://testserver"

class RequestAdapter:
    def get(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        # Remove stream param if present for TestClient
        kwargs.pop("stream", None)
        return client.get(path, **kwargs)
    def post(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        return client.post(path, **kwargs)
    def delete(self, url, **kwargs):
        path = url.replace(BASE_URL, "").replace("http://127.0.0.1:38281", "")
        return client.delete(path, **kwargs)

requests = RequestAdapter()

def test_cache_poisoning_after_depth0():
    print("\n--- TEST: Cache Poisoning via depth=0 ---")
    # Query normal depth=16 on starting position which was hit by depth=0 probe
    r = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "depth": 16
    }, stream=True, timeout=5)
    lines = []
    for line in r.iter_lines():
        if line:
            lines.append(line.decode('utf-8'))
    print("Received lines for depth=16 on starting position:")
    for l in lines:
        print(" ", l)

def test_aborted_stream_cache_poisoning():
    print("\n--- TEST: Aborted Stream Cache Poisoning ---")
    fen_test = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
    
    # Send request 1 for fen_test with depth 16, but abort quickly
    try:
        r1 = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": fen_test, "depth": 16}, stream=True, timeout=0.1)
        for line in r1.iter_lines():
            pass
    except Exception:
        pass # aborted

    time.sleep(0.5)
    # Now check what is cached for fen_test
    r2 = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": fen_test, "depth": 16}, stream=True, timeout=5)
    lines = []
    for line in r2.iter_lines():
        if line:
            lines.append(line.decode('utf-8'))
    print("Received lines for second request to fen_test:")
    for l in lines:
        print(" ", l)

def test_stream_concurrency_cancellation():
    print("\n--- TEST: Concurrent Stream Cancellation ---")
    fen1 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    fen2 = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    
    events1 = []
    events2 = []

    def req1():
        r = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": fen1, "depth": 16}, stream=True, timeout=10)
        for line in r.iter_lines():
            if line:
                events1.append(line.decode('utf-8'))

    def req2():
        time.sleep(0.05) # start slightly after req1
        r = requests.get(f"{BASE_URL}/api/repertoire/eval_stream", params={"fen": fen2, "depth": 16}, stream=True, timeout=10)
        for line in r.iter_lines():
            if line:
                events2.append(line.decode('utf-8'))

    t1 = threading.Thread(target=req1)
    t2 = threading.Thread(target=req2)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    print(f"Req 1 (fen1) got {len(events1)} events:")
    for e in events1:
        print("  Req1:", e)
    print(f"Req 2 (fen2) got {len(events2)} events:")
    for e in events2:
        print("  Req2:", e)

def test_analyzer_malformed_games():
    print("\n--- TEST: GameAnalyzer Malformed Games ---")
    analyzer = GameAnalyzer()

    # Case A: Missing 'user' key (e.g. AI or guest player)
    game_ai = {
        "id": "game_ai_test",
        "speed": "blitz",
        "rated": False,
        "players": {
            "white": {"aiLevel": 3},
            "black": {"user": {"name": "TestUser", "id": "testuser"}}
        },
        "moves": "e4 e5 Nf3 Nc6",
        "user_color": "black"
    }

    # Test api sync logic with game_ai
    print("Testing game_ai players extraction:")
    try:
        white_user = game_ai.get("players", {}).get("white", {}).get("user", {}).get("name", "")
        print("game_ai white_user:", white_user)
    except Exception as e:
        print("game_ai white_user FAILED:", type(e), e)

    # Case B: 'white' is None
    game_none = {
        "id": "game_none_test",
        "speed": "blitz",
        "rated": False,
        "players": {
            "white": None,
            "black": {"user": {"name": "TestUser", "id": "testuser"}}
        },
        "moves": "e4 e5",
        "user_color": "black"
    }
    print("Testing game_none players extraction:")
    try:
        white_user = game_none.get("players", {}).get("white", {}).get("user", {}).get("name", "")
        print("game_none white_user:", white_user)
    except Exception as e:
        print("game_none white_user CRASHED:", type(e), e)

    # Case C: Mate in 1 blunder
    # 1. f3 e5 2. g4 Qh4# (Fool's mate)
    # White blunders with 2. g4 allowing Qh4#
    game_fool = {
        "id": "fools_mate_game",
        "user_color": "white",
        "moves": "f3 e5 g4 Qh4#",
        "opening": {"name": "Fool's Mate", "eco": "A00"}
    }
    print("Testing Fool's mate analysis:")
    try:
        blunders = analyzer.analyze_game_openings(game_fool)
        print("Fool's mate blunders found:", len(blunders))
        for b in blunders:
            print(f"  Blunder move: {b['played_san']} cpl_loss: {b['cpl_loss']} refutation: {b['refutation_pv']}")
    except Exception as e:
        import traceback
        print("Fool's mate CRASHED:", type(e), e)
        traceback.print_exc()

    # Case D: 0-ply game
    game_0ply = {"id": "0ply", "user_color": "white", "moves": ""}
    print("Testing 0-ply game:")
    try:
        blunders = analyzer.analyze_game_openings(game_0ply)
        print("0-ply blunders:", len(blunders))
    except Exception as e:
        print("0-ply CRASHED:", type(e), e)

    # Case E: 1-ply game
    game_1ply = {"id": "1ply", "user_color": "white", "moves": "e4"}
    print("Testing 1-ply game:")
    try:
        blunders = analyzer.analyze_game_openings(game_1ply)
        print("1-ply blunders:", len(blunders))
    except Exception as e:
        print("1-ply CRASHED:", type(e), e)

    analyzer.close()

def test_analyzer_with_extreme_settings():
    print("\n--- TEST: GameAnalyzer with Extreme Settings ---")
    analyzer = GameAnalyzer()

    # Set negative max_opening_ply = -5
    set_setting("max_opening_ply", "-5")
    game = {
        "id": "extreme_settings_test",
        "user_color": "white",
        "moves": "e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6 d3 d6 O-O O-O",
        "opening": {"name": "Italian Game", "eco": "C50"}
    }
    print("Testing max_opening_ply = -5:")
    try:
        blunders = analyzer.analyze_game_openings(game)
        print(f"Blunders detected with ply=-5: {len(blunders)}")
    except Exception as e:
        print("ply=-5 CRASHED:", type(e), e)

    # Reset
    set_setting("max_opening_ply", "24")

    # Set blunder_threshold_cp = -999999
    set_setting("blunder_threshold_cp", "-999999")
    print("Testing blunder_threshold_cp = -999999:")
    try:
        blunders = analyzer.analyze_game_openings(game)
        print(f"Blunders detected with thresh=-999999: {len(blunders)} (Every move flagged as blunder!)")
        for b in blunders[:3]:
            print(f"  Move: {b['played_san']}, cpl_loss: {b['cpl_loss']}, category: {b['category']}")
    except Exception as e:
        print("thresh=-999999 CRASHED:", type(e), e)

    # Reset
    set_setting("blunder_threshold_cp", "150")
    analyzer.close()

def test_concurrency_db():
    print("\n--- TEST: Concurrency DB Stress ---")
    successes = 0
    failures = 0
    errors = []

    def write_move(i):
        payload = {
            "repertoire": "white",
            "parent_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "move_uci": "e2e4",
            "eval_score": i
        }
        r = requests.post(f"{BASE_URL}/api/repertoire/move", json=payload, timeout=5)
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(write_move, i) for i in range(50)]
        for f in concurrent.futures.as_completed(futures):
            try:
                code = f.result()
                if code == 200:
                    successes += 1
                else:
                    failures += 1
                    errors.append(f"HTTP {code}")
            except Exception as e:
                failures += 1
                errors.append(str(e))

    print(f"Concurrent repertoire writes: {successes} succeeded, {failures} failed. Errors: {errors[:5]}")

if __name__ == "__main__":
    test_cache_poisoning_after_depth0()
    test_aborted_stream_cache_poisoning()
    test_stream_concurrency_cancellation()
    test_analyzer_malformed_games()
    test_analyzer_with_extreme_settings()
    test_concurrency_db()
