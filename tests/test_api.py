import unittest
from unittest.mock import patch
import chess
import tests.test_setup
from fastapi.testclient import TestClient
import backend.api as api_module
from backend.api import app
from backend.db import init_db, set_setting
from tests.fixtures import SYNTHETIC_USERNAME

class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_setup.reset_fixtures()
        init_db()
        set_setting("lichess_username", SYNTHETIC_USERNAME)
        cls.client = TestClient(app)
        cls._patchers = [
            patch.object(api_module.LichessClient, "_resolve_token", lambda self: None),
            patch.object(api_module.LichessClient, "get_account_profile",
                         lambda self: {"username": SYNTHETIC_USERNAME, "id": "synthetic", "perfs": {}}),
        ]
        for p in cls._patchers:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls._patchers:
            p.stop()
        tests.test_setup.reset_fixtures()

    def setUp(self):
        set_setting("lichess_username", SYNTHETIC_USERNAME)

    def test_account_endpoint(self):
        res = self.client.get("/api/account")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("connected", data)
        self.assertIn("username", data)
        # FIXER-W-01 (strengthened, not relaxed): the account really was resolved, from the
        # locally declared profile, and no token is present.
        self.assertTrue(data["connected"])
        self.assertEqual(data["username"], SYNTHETIC_USERNAME)
        self.assertFalse(data["token_present"])

    def test_dashboard_endpoint(self):
        res = self.client.get("/api/dashboard")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("total_games", data)
        self.assertIn("total_blunders", data)

    def test_blunders_endpoint(self):
        res = self.client.get("/api/blunders")
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.json(), list)

    def test_training_paths_endpoint(self):
        res = self.client.get("/api/train/paths")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("paths", data)
        self.assertIn("due_today", data)
        # BUG-B07 check: group_name should never be None or empty
        for p in data["paths"]:
            self.assertIsNotNone(p.get("group_name"))
            self.assertTrue(len(p.get("group_name", "")) > 0)

    def test_repertoire_tree(self):
        with patch("backend.repertoire.RepertoireManager.get_candidate_moves_2000",
                   return_value={"fen": chess.STARTING_FEN, "total_games": 100, "moves": []}):
            res = self.client.get("/api/repertoire/tree?color=white&threshold=5.0")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn("coverage_pct", data)
            self.assertIn("total_moves", data)

    def test_repertoire_explore(self):
        with patch("backend.repertoire.RepertoireManager.get_candidate_moves_2000",
                   return_value={"fen": chess.STARTING_FEN, "total_games": 100, "moves": []}):
            res = self.client.get("/api/repertoire/explore?fen=rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR%20w%20KQkq%20-%200%201")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn("moves", data)

    # --- BUG-01: Malformed FEN in /api/repertoire/explore ---
    def test_repertoire_explore_malformed_fen(self):
        for bad_fen in ["garbage", "8/8/8/8/8/8/8/8", "not_a_fen", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP"]:
            res = self.client.get("/api/repertoire/explore", params={"fen": bad_fen})
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.json().get("detail"), "Invalid FEN string")

    # --- BUG-02: Invalid parent_fen in /api/repertoire/move ---
    def test_repertoire_move_invalid_parent_fen(self):
        for bad_fen in ["invalid_fen", "12345", "8/8/8/8/8/8/8/8"]:
            res = self.client.post("/api/repertoire/move", json={
                "repertoire": "white",
                "parent_fen": bad_fen,
                "move_uci": "e2e4"
            })
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.json().get("detail"), "Invalid FEN string")

    # --- BUG-03: Illegal chess moves rejected ---
    def test_repertoire_move_illegal_moves(self):
        # 1. Non-existent move
        res1 = self.client.post("/api/repertoire/move", json={
            "repertoire": "white",
            "parent_fen": chess.STARTING_FEN,
            "move_uci": "e2e5"
        })
        self.assertEqual(res1.status_code, 400)
        self.assertIn("Illegal move", res1.json().get("detail", ""))

        # 2. Pawn promotion without promotion piece specified (e7e8 instead of e7e8q)
        res2 = self.client.post("/api/repertoire/move", json={
            "repertoire": "white",
            "parent_fen": "8/4P3/8/8/8/8/8/k6K w - - 0 1",
            "move_uci": "e7e8"
        })
        self.assertEqual(res2.status_code, 400)
        self.assertIn("Illegal move", res2.json().get("detail", ""))

        # 3. Valid pawn promotion succeeds
        res3 = self.client.post("/api/repertoire/move", json={
            "repertoire": "white",
            "parent_fen": "8/4P3/8/8/8/8/8/k6K w - - 0 1",
            "move_uci": "e7e8q"
        })
        self.assertEqual(res3.status_code, 200)

    # --- BUG-04: Arbitrary / Invalid repertoire color rejected ---
    def test_repertoire_invalid_colors(self):
        for bad_color in ["purple", "", "red", "WHITE"]:
            res = self.client.post("/api/repertoire/move", json={
                "repertoire": bad_color,
                "parent_fen": chess.STARTING_FEN,
                "move_uci": "e2e4"
            })
            self.assertEqual(res.status_code, 422)

            res_tree = self.client.get(f"/api/repertoire/tree?color={bad_color}")
            self.assertEqual(res_tree.status_code, 422)

    # --- BUG-05: Opponent move with NULL popularity_pct handled cleanly ---
    def test_repertoire_null_popularity_pct_trainer(self):
        unique_fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
        # User move
        self.client.post("/api/repertoire/move", json={
            "repertoire": "black",
            "parent_fen": unique_fen,
            "move_uci": "b8c6",
            "is_user_move": True
        })
        # Opponent reply with NULL popularity_pct
        self.client.post("/api/repertoire/move", json={
            "repertoire": "black",
            "parent_fen": "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
            "move_uci": "f1c4",
            "is_user_move": False,
            "popularity_pct": None
        })
        # Attempt move in trainer
        res = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "black",
            "current_fen": unique_fen,
            "attempted_uci": "b8c6"
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("correct"))

    # --- BUG-06 & BUG-C05: depth bounds on eval_stream ---
    def test_eval_stream_depth_validation(self):
        # depth <= 0 rejected
        res0 = self.client.get("/api/repertoire/eval_stream", params={"fen": chess.STARTING_FEN, "depth": 0})
        self.assertEqual(res0.status_code, 422)

        res_neg = self.client.get("/api/repertoire/eval_stream", params={"fen": chess.STARTING_FEN, "depth": -5})
        self.assertEqual(res_neg.status_code, 422)

        # depth > 30 rejected
        res_huge = self.client.get("/api/repertoire/eval_stream", params={"fen": chess.STARTING_FEN, "depth": 35})
        self.assertEqual(res_huge.status_code, 422)

    # --- BUG-C01 & BUG-C06: eval_stream FEN validation ---
    def test_eval_stream_fen_validation(self):
        # Illegal pawn on rank 8
        res_c01 = self.client.get("/api/repertoire/eval_stream", params={"fen": "P7/8/8/8/8/8/8/k6K w - - 0 1", "depth": 10})
        self.assertEqual(res_c01.status_code, 400)

        # Empty board
        res_c06 = self.client.get("/api/repertoire/eval_stream", params={"fen": "8/8/8/8/8/8/8/8 w - - 0 1", "depth": 10})
        self.assertEqual(res_c06.status_code, 400)

    # --- BUG-07: Malformed Game JSON sync safe handling ---
    def test_sync_malformed_games(self):
        malformed_games = [
            {
                "id": "game_none_white",
                "speed": "blitz",
                "rated": True,
                "players": {"white": None, "black": {"user": {"name": "testuser"}}},
                "moves": "e4 e5"
            },
            {
                "id": "game_ai_bot",
                "speed": "rapid",
                "rated": False,
                "players": {"white": {"aiLevel": 3}, "black": {"user": None}},
                "moves": "d4 d5"
            },
            {
                "id": "game_none_opening",
                "speed": "blitz",
                "rated": True,
                "players": {"white": {"user": {"name": "testuser"}}, "black": {"user": {"name": "opp"}}},
                "opening": None,
                "moves": "e4 e5"
            }
        ]
        with patch("backend.api.LichessClient.fetch_user_games", return_value=malformed_games), \
             patch("backend.analyzer.GameAnalyzer.analyze_game_openings", return_value=[]):
            res = self.client.post("/api/sync?max_games=3")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json().get("status"), "success")

    # --- BUG-08: gap_threshold setting and bounds ---
    def test_settings_gap_threshold_and_bounds(self):
        # 1. Bounds rejected (negative values)
        res_bad_ply = self.client.post("/api/settings", json={"max_opening_ply": -10})
        self.assertEqual(res_bad_ply.status_code, 422)

        res_bad_thresh = self.client.post("/api/settings", json={"blunder_threshold_cp": -100})
        self.assertEqual(res_bad_thresh.status_code, 422)

        res_bad_gap = self.client.post("/api/settings", json={"gap_threshold": 0.05})
        self.assertEqual(res_bad_gap.status_code, 422)

        # 2. Valid settings update and retrieval
        res_ok = self.client.post("/api/settings", json={
            "max_opening_ply": 26,
            "blunder_threshold_cp": 160,
            "gap_threshold": 7.5
        })
        self.assertEqual(res_ok.status_code, 200)

        res_get = self.client.get("/api/settings")
        self.assertEqual(res_get.status_code, 200)
        data = res_get.json()
        self.assertEqual(data.get("max_opening_ply"), 26)
        self.assertEqual(data.get("blunder_threshold_cp"), 160)
        self.assertEqual(data.get("gap_threshold"), 7.5)

    def test_sync_games_stream_no_games(self):
        """Verify SSE stream handles empty game results cleanly."""
        with patch("backend.repertoire.LichessClient.fetch_user_games", return_value=[]):
            res = self.client.get("/api/repertoire/sync_games_stream")
            self.assertEqual(res.status_code, 200)
            self.assertIn("text/event-stream", res.headers.get("content-type", ""))
            content = res.text
            self.assertIn('"stage": "fetching"', content)
            self.assertIn('"stage": "no_games"', content)

    def test_sync_games_stream_with_games(self):
        """Verify SSE stream emits progressive analyzing and completed events."""
        mock_games = [
            {
                "id": "stream_test_g1",
                "speed": "rapid",
                "rated": True,
                "createdAt": 1700000000,
                "players": {
                    "white": {"user": {"name": "RobiDeli"}, "rating": 1965},
                    "black": {"user": {"name": "Opponent"}, "rating": 1980}
                },
                "moves": "e4 e5 Nf3 Nc6 Bc4",
                "opening": {"name": "Italian Game", "eco": "C50"}
            }
        ]
        with patch("backend.repertoire.LichessClient.fetch_user_games", return_value=mock_games), \
             patch("backend.analyzer.GameAnalyzer.analyze_game_openings", return_value=[]):
            res = self.client.get("/api/repertoire/sync_games_stream?max_games=1")
            self.assertEqual(res.status_code, 200)
            self.assertIn("text/event-stream", res.headers.get("content-type", ""))
            content = res.text
            self.assertIn('"stage": "fetching"', content)
            self.assertIn('"stage": "analyzing_start"', content)
            self.assertIn('"stage": "analyzing"', content)
            self.assertIn('"stage": "completed"', content)
            self.assertIn('"percent": 100', content)

    def test_sync_games_stream_validation(self):
        """Verify max_games parameter validation."""
        res_bad = self.client.get("/api/repertoire/sync_games_stream?max_games=0")
        self.assertEqual(res_bad.status_code, 422)

    def test_clear_repertoire_endpoint(self):
        """Verify POST /api/repertoire/clear resets database and validates input."""
        # 1. Add a move to white repertoire
        self.client.post("/api/repertoire/move", json={
            "repertoire": "white",
            "parent_fen": chess.STARTING_FEN,
            "move_uci": "e2e4",
            "is_user_move": True
        })
        tree_res = self.client.get("/api/repertoire/tree?color=white")
        self.assertGreater(tree_res.json()["total_moves"], 0)

        # 2. Reset white repertoire specifically
        clear_white_res = self.client.post("/api/repertoire/clear?repertoire=white")
        self.assertEqual(clear_white_res.status_code, 200)
        self.assertEqual(clear_white_res.json()["status"], "ok")
        self.assertIn("White repertoire", clear_white_res.json()["message"])

        # Verify white is empty
        tree_res = self.client.get("/api/repertoire/tree?color=white")
        self.assertEqual(tree_res.json()["total_moves"], 0)

        # 3. Add move and reset all
        self.client.post("/api/repertoire/move", json={
            "repertoire": "white",
            "parent_fen": chess.STARTING_FEN,
            "move_uci": "e2e4",
            "is_user_move": True
        })
        clear_all_res = self.client.post("/api/repertoire/clear")
        self.assertEqual(clear_all_res.status_code, 200)
        self.assertEqual(clear_all_res.json()["status"], "ok")
        self.assertIn("Entire repertoire database", clear_all_res.json()["message"])

        # 4. Bad color parameter validation
        bad_res = self.client.post("/api/repertoire/clear?repertoire=invalid")
        self.assertEqual(bad_res.status_code, 400)

    def test_api_update_move_meta(self):
        # 1. Add move
        self.client.post("/api/repertoire/move", json={
            "repertoire": "white",
            "parent_fen": chess.STARTING_FEN,
            "move_uci": "e2e4",
            "is_user_move": True
        })

        # 2. Update metadata via API
        res = self.client.post("/api/repertoire/move_meta", json={
            "repertoire": "white",
            "parent_fen": chess.STARTING_FEN,
            "move_uci": "e2e4",
            "is_important": True,
            "comment": "Key opening move with e4",
            "arrows": '[{"from":"e2","to":"e4","color":"#22c55e"}]'
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["is_important"], 1)
        self.assertEqual(data["comment"], "Key opening move with e4")
        self.assertIn("e2", data["arrows"])

    def test_recent_games_endpoint(self):
        res = self.client.get("/api/games/recent?limit=5")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("games", data)
        self.assertIn("total", data)
        self.assertIsInstance(data["games"], list)
        if len(data["games"]) > 0:
            g = data["games"][0]
            self.assertIn("id", g)
            self.assertIn("speed", g)
            self.assertIn("user_color", g)
            self.assertIn("opponent_name", g)
            self.assertIn("result", g)
            self.assertIn("score_text", g)
            self.assertIn("opening_name", g)
            self.assertIn("eco", g)
            self.assertIn("moves_san", g)
            self.assertIn("opening_ply", g)
            self.assertIn("game_url", g)

    def test_recent_games_filters(self):
        res_w = self.client.get("/api/games/recent?color=white&limit=10")
        self.assertEqual(res_w.status_code, 200)
        data_w = res_w.json()
        for g in data_w["games"]:
            self.assertEqual(g["user_color"], "white")

        res_b = self.client.get("/api/games/recent?color=black&limit=10")
        self.assertEqual(res_b.status_code, 200)
        data_b = res_b.json()
        for g in data_b["games"]:
            self.assertEqual(g["user_color"], "black")

        res_bad = self.client.get("/api/games/recent?color=invalid")
        self.assertEqual(res_bad.status_code, 422)

        res_rapid = self.client.get("/api/games/recent?speed=rapid&limit=10")
        self.assertEqual(res_rapid.status_code, 200)
        data_rapid = res_rapid.json()
        for g in data_rapid["games"]:
            self.assertEqual(g["speed"], "rapid")

    def test_import_game_opening_endpoint(self):
        res = self.client.get("/api/games/recent?limit=1")
        self.assertEqual(res.status_code, 200)
        games = res.json().get("games", [])
        if games:
            game = games[0]
            imp_res = self.client.post("/api/repertoire/import_game_opening", json={
                "game_id": game["id"],
                "repertoire": game["user_color"],
                "max_ply": 6
            })
            self.assertEqual(imp_res.status_code, 200)
            imp_data = imp_res.json()
            self.assertEqual(imp_data["status"], "success")
            self.assertTrue(imp_data["imported_moves"] > 0)

        # Invalid game_id error handling
        bad_res = self.client.post("/api/repertoire/import_game_opening", json={
            "game_id": "non_existent_game_xyz",
            "repertoire": "white"
        })
        self.assertEqual(bad_res.status_code, 400)

        # Invalid repertoire color
        bad_col = self.client.post("/api/repertoire/import_game_opening", json={
            "game_id": "any_id",
            "repertoire": "purple"
        })
        self.assertEqual(bad_col.status_code, 422)

    def test_sync_recent_games_endpoint(self):
        mock_games = [
            {
                "id": "mock_game_123",
                "speed": "blitz",
                "rated": True,
                "createdAt": 1700000000000,
                "players": {
                    "white": {"user": {"name": "RobiDeli"}, "rating": 1950},
                    "black": {"user": {"name": "GrandMasterOpp"}, "rating": 2100}
                },
                "winner": "white",
                "opening": {"name": "Italian Game", "eco": "C50"},
                "moves": "e4 e5 Nf3 Nc6 Bc4 Bc5"
            }
        ]
        with patch("backend.repertoire.LichessClient.fetch_user_games", return_value=mock_games):
            res = self.client.post("/api/games/sync_recent?max_games=5")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "success")
            self.assertGreaterEqual(data["synced_games"], 1)

    def test_sync_recent_games_stream_endpoint(self):
        mock_games = [
            {
                "id": "mock_game_stream_456",
                "speed": "blitz",
                "rated": True,
                "createdAt": 1700000000000,
                "players": {
                    "white": {"user": {"name": "RobiDeli"}, "rating": 1950},
                    "black": {"user": {"name": "GrandMasterOpp"}, "rating": 2100}
                },
                "winner": "white",
                "opening": {"name": "Sicilian Defense", "eco": "B33"},
                "moves": "e4 c5 Nf3 Nc6 d4 cxd4 Nxd4"
            }
        ]
        with patch("backend.repertoire.LichessClient.fetch_user_games", return_value=mock_games):
            res = self.client.get("/api/games/sync_recent_stream?max_games=5")
            self.assertEqual(res.status_code, 200)
            self.assertIn("text/event-stream", res.headers.get("content-type", ""))
            content = res.content.decode("utf-8")
            self.assertIn('"stage": "connecting"', content)
            self.assertIn('"stage": "completed"', content)
            self.assertIn('"synced_games": 1', content)

if __name__ == "__main__":
    unittest.main()
