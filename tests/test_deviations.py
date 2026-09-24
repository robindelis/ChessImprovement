"""
tests/test_deviations.py — Unit and integration tests for the Repertoire Deviations Engine.

Verifies:
1. Pure move-tree replay against repertoire_moves without engine overhead.
2. Accurate deviation detection: user departure vs opponent out-of-book.
3. Move numbering, SAN/UCI translation, FEN tracking, and user color orientation.
4. Compliance percentage calculation and pagination.
5. Endpoints:
   - GET /api/repertoire/deviations
   - GET /api/repertoire/train/deviation_drill
   - GET /api/repertoire/train/next?mode=deviations
"""
import unittest
import chess
import tests.test_setup
from fastapi.testclient import TestClient
from backend.api import app
from backend.db import get_db, init_db
from backend.deviations import (
    find_game_deviations,
    get_deviation_drill,
    mark_deviation_resolved,
    reset_resolved_deviations,
    get_resolved_deviations,
)
from backend.repertoire import normalize_fen


class TestDeviationsEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_setup.reset_fixtures()
        init_db()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        tests.test_setup.reset_fixtures()

    def setUp(self):
        reset_resolved_deviations()
        conn = get_db()
        cursor = conn.cursor()
        # Clean slate for repertoire_moves and games in these unit tests
        cursor.execute("DELETE FROM repertoire_moves")
        cursor.execute("DELETE FROM games")
        conn.commit()
        conn.close()

    def _insert_rep_move(self, repertoire, parent_fen, uci, san, is_user, opening="Test Line", eco="A00", comment=""):
        b = chess.Board(parent_fen)
        try:
            m = chess.Move.from_uci(uci)
            b.push(m)
            after_fen = b.fen()
        except Exception:
            after_fen = parent_fen

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO repertoire_moves
            (repertoire, parent_fen, fen, move_uci, move_san, is_user_move, opening_name, eco, comment, popularity_pct, win_rate, eval_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 100.0, 50.0, 0)
        """, (repertoire, normalize_fen(parent_fen), after_fen, uci, san, 1 if is_user else 0, opening, eco, comment))
        conn.commit()
        conn.close()

    def _insert_game(self, game_id, user_color, moves_san, white="User", black="Opponent", result="1-0"):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO games (id, user_color, moves_san, white_player, black_player, result, played_at, speed)
            VALUES (?, ?, ?, ?, ?, ?, 1600000000, 'rapid')
        """, (game_id, user_color, moves_san, white, black, result))
        conn.commit()
        conn.close()

    def test_empty_system_returns_zero_deviations(self):
        res = find_game_deviations()
        self.assertEqual(res["total_games_analyzed"], 0)
        self.assertEqual(res["total_deviations"], 0)
        self.assertEqual(res["compliance_pct"], 100.0)
        self.assertEqual(res["deviations"], [])

    def test_perfect_repertoire_adherence(self):
        # Setup: 1. e4 e5 2. Nf3 Nc6 in repertoire
        start_fen = chess.STARTING_FEN
        board = chess.Board()
        self._insert_rep_move("white", board.fen(), "e2e4", "e4", is_user=True)
        board.push_san("e4")
        self._insert_rep_move("white", board.fen(), "e7e5", "e5", is_user=False)
        board.push_san("e5")
        self._insert_rep_move("white", board.fen(), "g1f3", "Nf3", is_user=True)
        board.push_san("Nf3")
        self._insert_rep_move("white", board.fen(), "b8c6", "Nc6", is_user=False)

        # Game where user played exactly this
        self._insert_game("g1", "white", "1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5")

        res = find_game_deviations(color="white")
        self.assertEqual(res["total_games_analyzed"], 1)
        self.assertEqual(res["games_with_prep"], 1)
        self.assertEqual(res["games_followed_repertoire"], 1)
        self.assertEqual(res["total_deviations"], 0)
        self.assertEqual(res["compliance_pct"], 100.0)

    def test_user_deviates_on_move_1(self):
        # White prepared 1. e4, but played 1. d4 in game
        self._insert_rep_move("white", chess.STARTING_FEN, "e2e4", "e4", is_user=True, comment="King's pawn")
        self._insert_game("g1", "white", "1. d4 d5 2. c4 e6", black="GrandmasterX")

        res = find_game_deviations(color="white")
        self.assertEqual(res["total_games_analyzed"], 1)
        self.assertEqual(res["total_deviations"], 1)
        self.assertEqual(res["compliance_pct"], 0.0)

        dev = res["deviations"][0]
        self.assertEqual(dev["game_id"], "g1")
        self.assertEqual(dev["move_number"], 1)
        self.assertEqual(dev["ply"], 1)
        self.assertEqual(dev["user_color"], "white")
        self.assertEqual(dev["played_san"], "d4")
        self.assertEqual(dev["played_uci"], "d2d4")
        self.assertEqual(dev["prepared_san"], "e4")
        self.assertEqual(dev["prepared_uci"], "e2e4")
        self.assertEqual(dev["opponent_name"], "GrandmasterX")
        self.assertEqual(dev["prepared_comment"], "King's pawn")

    def test_user_deviates_on_move_3(self):
        board = chess.Board()
        # 1. e4
        self._insert_rep_move("white", board.fen(), "e2e4", "e4", is_user=True)
        board.push_san("e4")
        # 1... e5
        self._insert_rep_move("white", board.fen(), "e7e5", "e5", is_user=False)
        board.push_san("e5")
        # 2. Nf3
        self._insert_rep_move("white", board.fen(), "g1f3", "Nf3", is_user=True)
        board.push_san("Nf3")
        # 2... Nc6
        self._insert_rep_move("white", board.fen(), "b8c6", "Nc6", is_user=False)
        board.push_san("Nc6")
        # 3. Bc4 (Italian)
        self._insert_rep_move("white", board.fen(), "f1c4", "Bc4", is_user=True, opening="Italian Game", eco="C50")

        # In game, user played 3. Bb5 (Ruy Lopez) instead of prepared 3. Bc4
        self._insert_game("g2", "white", "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6")

        res = find_game_deviations(color="white")
        self.assertEqual(res["total_deviations"], 1)
        dev = res["deviations"][0]
        self.assertEqual(dev["move_number"], 3)
        self.assertEqual(dev["ply"], 5)
        self.assertEqual(dev["played_san"], "Bb5")
        self.assertEqual(dev["played_uci"], "f1b5")
        self.assertEqual(dev["prepared_san"], "Bc4")
        self.assertEqual(dev["prepared_uci"], "f1c4")
        self.assertEqual(dev["opening_name"], "Italian Game")

    def test_opponent_leaves_book_first_is_not_user_deviation(self):
        # White prepared 1. e4 answering 1... e5 with 2. Nf3
        board = chess.Board()
        self._insert_rep_move("white", board.fen(), "e2e4", "e4", is_user=True)
        board.push_san("e4")
        self._insert_rep_move("white", board.fen(), "e7e5", "e5", is_user=False)

        # Opponent played 1... c5 (Sicilian Defense), not in repertoire branches
        self._insert_game("g3", "white", "1. e4 c5 2. Nf3 d6 3. d4 cxd4")

        res = find_game_deviations(color="white")
        # Opponent left the book, user did not deviate from prepared move
        self.assertEqual(res["total_deviations"], 0)
        self.assertEqual(res["compliance_pct"], 100.0)

    def test_black_repertoire_deviation(self):
        # Repertoire for Black: 1. e4 -> 1... c5 (Sicilian)
        board = chess.Board()
        self._insert_rep_move("black", board.fen(), "e2e4", "e4", is_user=False)
        board.push_san("e4")
        self._insert_rep_move("black", board.fen(), "c7c5", "c5", is_user=True, opening="Sicilian Defense", eco="B20")

        # In game, as Black, user played 1... e5
        self._insert_game("g4", "black", "1. e4 e5 2. Nf3 Nc6", white="Magnus")

        res = find_game_deviations(color="black")
        self.assertEqual(res["total_deviations"], 1)
        dev = res["deviations"][0]
        self.assertEqual(dev["user_color"], "black")
        self.assertEqual(dev["move_number"], 1)
        self.assertEqual(dev["ply"], 2)
        self.assertEqual(dev["played_san"], "e5")
        self.assertEqual(dev["prepared_san"], "c5")
        self.assertEqual(dev["opponent_name"], "Magnus")

    def test_color_filtering_isolation(self):
        self._insert_rep_move("white", chess.STARTING_FEN, "e2e4", "e4", is_user=True)
        self._insert_game("gw", "white", "1. d4 d5")  # white deviation
        self._insert_game("gb", "black", "1. e4 e5")  # black game

        res_white = find_game_deviations(color="white")
        self.assertEqual(res_white["total_games_analyzed"], 1)
        self.assertEqual(res_white["total_deviations"], 1)

        res_black = find_game_deviations(color="black")
        self.assertEqual(res_black["total_games_analyzed"], 1)
        self.assertEqual(res_black["total_deviations"], 0)

    def test_pagination_and_search(self):
        self._insert_rep_move("white", chess.STARTING_FEN, "e2e4", "e4", is_user=True)
        self._insert_game("g_alice", "white", "1. d4 d5", black="AlicePlayer")
        self._insert_game("g_bob", "white", "1. c4 e5", black="BobPlayer")

        res_all = find_game_deviations(color="white", limit=1, offset=0)
        self.assertEqual(res_all["total_deviations"], 2)
        self.assertEqual(len(res_all["deviations"]), 1)

        res_search = find_game_deviations(color="white", search="Alice")
        self.assertEqual(res_search["total_deviations"], 1)
        self.assertEqual(res_search["deviations"][0]["opponent_name"], "AlicePlayer")

    def test_api_endpoints(self):
        # Insert a deviation
        self._insert_rep_move("white", chess.STARTING_FEN, "e2e4", "e4", is_user=True, comment="Control center")
        self._insert_game("api_g1", "white", "1. d4 d5 2. c4 e6", black="Kasparov")

        # 1. GET /api/repertoire/deviations
        res = self.client.get("/api/repertoire/deviations?color=white")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total_deviations"], 1)
        self.assertEqual(data["deviations"][0]["played_san"], "d4")
        self.assertEqual(data["deviations"][0]["prepared_san"], "e4")

        # 2. GET /api/repertoire/train/deviation_drill
        res_drill = self.client.get("/api/repertoire/train/deviation_drill?color=white&game_id=api_g1")
        self.assertEqual(res_drill.status_code, 200)
        drill = res_drill.json()
        self.assertIsNotNone(drill)
        self.assertEqual(drill["expected_uci"], "e2e4")
        self.assertEqual(drill["expected_san"], "e4")
        self.assertEqual(drill["reason"], "session_lapse")
        self.assertEqual(drill["parent_fen"], chess.STARTING_FEN)

        # 3. GET /api/repertoire/train/next?mode=deviations
        res_next = self.client.get("/api/repertoire/train/next?color=white&mode=deviations")
        self.assertEqual(res_next.status_code, 200)
        next_drill = res_next.json()
        self.assertIsNotNone(next_drill)
        self.assertEqual(next_drill["expected_uci"], "e2e4")
        self.assertEqual(next_drill["deviation_index"], 1)
        self.assertEqual(next_drill["total_deviations"], 1)

    def test_interactive_deviations_navigation_and_payload(self):
        # Insert White repertoire: 1. e4 e5 2. Nf3
        board = chess.Board()
        self._insert_rep_move("white", board.fen(), "e2e4", "e4", is_user=True)
        board.push_san("e4")
        self._insert_rep_move("white", board.fen(), "e7e5", "e5", is_user=False)
        board.push_san("e5")
        self._insert_rep_move("white", board.fen(), "g1f3", "Nf3", is_user=True)

        # Game 1: User played 2. Bc4 instead of 2. Nf3
        self._insert_game("g_nav1", "white", "1. e4 e5 2. Bc4 Nf6", black="PlayerOne")
        # Game 2: User played 1. d4 instead of 1. e4
        self._insert_game("g_nav2", "white", "1. d4 d5 2. c4 e6", black="PlayerTwo")

        # Test index navigation
        res_idx0 = self.client.get("/api/repertoire/train/next?color=white&mode=deviations&index=0")
        self.assertEqual(res_idx0.status_code, 200)
        d0 = res_idx0.json()
        self.assertEqual(d0["deviation_index"], 1)
        self.assertEqual(d0["total_deviations"], 2)

        res_idx1 = self.client.get("/api/repertoire/train/next?color=white&mode=deviations&index=1")
        self.assertEqual(res_idx1.status_code, 200)
        d1 = res_idx1.json()
        self.assertEqual(d1["deviation_index"], 2)
        self.assertEqual(d1["total_deviations"], 2)

        # One of them has last_move (the move 2 deviation has Black's 1...e5)
        devs_res = find_game_deviations(color="white")
        dev2 = [d for d in devs_res["deviations"] if d["game_id"] == "g_nav1"][0]
        self.assertEqual(dev2["deviation_move_number"], 2)
        self.assertIsNotNone(dev2["last_move"])
        self.assertEqual(dev2["last_move"]["san"], "e5")
        self.assertEqual(dev2["last_move"]["from"], "e7")
        self.assertEqual(dev2["last_move"]["to"], "e5")
        self.assertEqual(dev2["path_formatted"], "1. e4 e5")

    def test_mark_deviation_resolved_removes_from_deviations_list(self):
        # Setup White repertoire: 1. e4
        self._insert_rep_move("white", chess.STARTING_FEN, "e2e4", "e4", is_user=True)
        # Game with deviation: user played 1. d4 instead of 1. e4
        self._insert_game("g_res1", "white", "1. d4 d5", black="Magnus")

        devs_before = find_game_deviations(color="white")
        self.assertEqual(devs_before["total_deviations"], 1)
        self.assertEqual(devs_before["compliance_pct"], 0.0)

        # Mark resolved on first try
        success = mark_deviation_resolved(
            game_id="g_res1",
            fen_before=chess.STARTING_FEN,
            played_uci="d2d4",
            prepared_uci="e2e4",
            user_color="white"
        )
        self.assertTrue(success)

        # Deviation must be removed from active deviations list
        devs_after = find_game_deviations(color="white")
        self.assertEqual(devs_after["total_deviations"], 0)
        self.assertEqual(devs_after["deviations"], [])
        self.assertEqual(devs_after["games_followed_repertoire"], 1)
        self.assertEqual(devs_after["compliance_pct"], 100.0)

    def test_resolve_deviation_api_endpoint(self):
        self._insert_rep_move("white", chess.STARTING_FEN, "e2e4", "e4", is_user=True)
        self._insert_game("g_api_res", "white", "1. d4 d5", black="Anand")

        res_before = self.client.get("/api/repertoire/deviations?color=white")
        self.assertEqual(res_before.json()["total_deviations"], 1)

        # POST /api/repertoire/deviations/resolve
        res_post = self.client.post("/api/repertoire/deviations/resolve", json={
            "game_id": "g_api_res",
            "fen_before": chess.STARTING_FEN,
            "played_uci": "d2d4",
            "prepared_uci": "e2e4",
            "user_color": "white"
        })
        self.assertEqual(res_post.status_code, 200)
        data = res_post.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["resolved"])
        self.assertEqual(data["remaining_deviations"], 0)

        # Confirm GET now returns 0 deviations
        res_after = self.client.get("/api/repertoire/deviations?color=white")
        self.assertEqual(res_after.json()["total_deviations"], 0)

    def test_attempt_endpoint_resolves_deviation_on_first_try_only(self):
        # White prepared 1. e4
        self._insert_rep_move("white", chess.STARTING_FEN, "e2e4", "e4", is_user=True)
        self._insert_game("g_try1", "white", "1. d4 d5", black="Caruana")

        # 1. User makes a WRONG move (e.g. 1. c4)
        res_wrong = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": chess.STARTING_FEN,
            "attempted_uci": "c2c4",
            "is_retry": False,
            "game_id": "g_try1",
            "is_deviation": True,
            "played_uci": "d2d4"
        })
        self.assertEqual(res_wrong.status_code, 200)
        self.assertFalse(res_wrong.json()["correct"])
        self.assertFalse(res_wrong.json().get("deviation_resolved", False))

        # Deviation MUST still be in the list!
        devs_after_wrong = find_game_deviations(color="white")
        self.assertEqual(devs_after_wrong["total_deviations"], 1)

        # 2. User retries and plays the CORRECT move (is_retry=True)
        # Should NOT resolve deviation because it was not on the first try!
        res_retry = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": chess.STARTING_FEN,
            "attempted_uci": "e2e4",
            "is_retry": True,
            "game_id": "g_try1",
            "is_deviation": True,
            "played_uci": "d2d4"
        })
        self.assertEqual(res_retry.status_code, 200)
        self.assertTrue(res_retry.json()["correct"])
        self.assertFalse(res_retry.json().get("deviation_resolved", False))

        # Deviation MUST still be in the list!
        devs_after_retry = find_game_deviations(color="white")
        self.assertEqual(devs_after_retry["total_deviations"], 1)

        # 3. User plays CORRECT move on FIRST TRY (is_retry=False)
        res_first_try = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": chess.STARTING_FEN,
            "attempted_uci": "e2e4",
            "is_retry": False,
            "game_id": "g_try1",
            "is_deviation": True,
            "played_uci": "d2d4"
        })
        self.assertEqual(res_first_try.status_code, 200)
        self.assertTrue(res_first_try.json()["correct"])
        self.assertTrue(res_first_try.json().get("deviation_resolved", False))

        # Deviation MUST now be removed from active list!
        devs_after_first_try = find_game_deviations(color="white")
        self.assertEqual(devs_after_first_try["total_deviations"], 0)

    def test_resolved_deviation_is_not_proposed_again_in_drills(self):
        self._insert_rep_move("white", chess.STARTING_FEN, "e2e4", "e4", is_user=True)
        self._insert_game("g_drill1", "white", "1. d4 d5", black="Nakamura")

        drill_before = get_deviation_drill(color="white")
        self.assertIsNotNone(drill_before)
        self.assertEqual(drill_before["game_id"], "g_drill1")

        # Resolve the deviation
        mark_deviation_resolved(
            game_id="g_drill1",
            fen_before=chess.STARTING_FEN,
            played_uci="d2d4",
            prepared_uci="e2e4",
            user_color="white"
        )

        # get_deviation_drill must now return None because all deviations are resolved
        drill_after = get_deviation_drill(color="white")
        self.assertIsNone(drill_after)

        # Next API endpoint reports all_deviations_completed
        res_next = self.client.get("/api/repertoire/train/next?color=white&mode=deviations")
        self.assertEqual(res_next.status_code, 200)
        self.assertTrue(res_next.json().get("all_deviations_completed"))

    def test_reset_resolved_deviations(self):
        self._insert_rep_move("white", chess.STARTING_FEN, "e2e4", "e4", is_user=True)
        self._insert_game("g_rst", "white", "1. d4 d5")

        mark_deviation_resolved(game_id="g_rst", fen_before=chess.STARTING_FEN, played_uci="d2d4", prepared_uci="e2e4")
        self.assertEqual(find_game_deviations(color="white")["total_deviations"], 0)

        # Reset via API
        res = self.client.post("/api/repertoire/deviations/reset")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "success")

        # Deviation is active again
        self.assertEqual(find_game_deviations(color="white")["total_deviations"], 1)

    def test_black_deviation_drill_perspective_and_repertoire_color(self):
        """When user deviates in a game as Black, the deviation drill must explicitly identify
        repertoire='black' and user_color='black', with parent_fen having side to move 'b'."""
        board = chess.Board()
        board.push_san("e4")
        fen_after_e4 = board.fen()
        # Prepared move for Black against 1. e4: 1... c5 (c7c5)
        self._insert_rep_move("black", fen_after_e4, "c7c5", "c5", is_user=True, opening="Sicilian Defense", eco="B20")

        # Game where user played Black and deviated: 1. e4 e5
        self._insert_game("g_black_dev", "black", "1. e4 e5 2. Nf3 Nc6", white="Kasparov", black="User")

        # Query drill directly
        drill = get_deviation_drill(color="black")
        self.assertIsNotNone(drill)
        self.assertEqual(drill["repertoire"], "black")
        self.assertEqual(drill["user_color"], "black")
        self.assertEqual(drill["expected_san"], "c5")
        self.assertEqual(drill["played_san"], "e5")
        self.assertEqual(drill["parent_fen"].split()[1], "b")

        # Query drill via API in mixed mode
        res_api = self.client.get("/api/repertoire/train/next?color=mixed&mode=deviations")
        self.assertEqual(res_api.status_code, 200)
        data = res_api.json()
        self.assertEqual(data.get("repertoire"), "black")
        self.assertEqual(data.get("user_color"), "black")
        self.assertEqual(data.get("expected_san"), "c5")
        self.assertEqual(data.get("parent_fen", "").split()[1], "b")


if __name__ == "__main__":
    unittest.main()
