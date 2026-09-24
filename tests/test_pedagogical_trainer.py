import unittest
import time
import os
import tempfile
import tests.test_setup
import chess
from fastapi.testclient import TestClient

from backend.db import get_db, init_db
from backend.repertoire import RepertoireManager
from backend.api import app

class TestPedagogicalTrainer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rm = RepertoireManager.get_instance()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        tests.test_setup.reset_fixtures()

    def setUp(self):
        conn = get_db()
        conn.execute("DELETE FROM repertoire_moves")
        conn.execute("DELETE FROM study_moves")
        conn.execute("DELETE FROM repertoire_studies")
        conn.commit()
        conn.close()

    def test_01_pgn_move_formatting(self):
        """Tests format_pgn_moves helper."""
        self.assertEqual(RepertoireManager.format_pgn_moves([]), "")
        self.assertEqual(RepertoireManager.format_pgn_moves(["e4"]), "1. e4")
        self.assertEqual(RepertoireManager.format_pgn_moves(["e4", "e6"]), "1. e4 e6")
        self.assertEqual(RepertoireManager.format_pgn_moves(["e4", "e6", "d4", "d5"]), "1. e4 e6 2. d4 d5")
        self.assertEqual(RepertoireManager.format_pgn_moves(["e4", "c6", "d4", "d5", "f3", "e6"]), "1. e4 c6 2. d4 d5 3. f3 e6")

    def test_02_progressive_depth_learnable_frontier(self):
        """
        Tests that when repertoire moves are unlearned,
        only root (depth 1) moves are in the learnable frontier.
        Once depth 1 is learned, depth 3 unlocks, etc.
        """
        # Setup 3-ply White French line: 1. e4 e6 2. d4 d5 3. e5
        b = chess.Board()
        self.rm.add_repertoire_move("white", b.fen(), "e2e4", is_user_move=True, study_name="French Defense")
        b.push_san("e4")
        self.rm.add_repertoire_move("white", b.fen(), "e7e6", is_user_move=False, study_name="French Defense")
        b.push_san("e6")
        self.rm.add_repertoire_move("white", b.fen(), "d2d4", is_user_move=True, study_name="French Defense")
        b.push_san("d4")
        self.rm.add_repertoire_move("white", b.fen(), "d7d5", is_user_move=False, study_name="French Defense")
        b.push_san("d5")
        self.rm.add_repertoire_move("white", b.fen(), "e4e5", is_user_move=True, study_name="French Defense")

        # Step 1: When all moves are unlearned, exactly 1 move (1. e4) is on the frontier
        graph = self.rm.build_repertoire_tree_graph("white")
        frontier = [n for n in graph.values() if n["is_frontier"]]
        self.assertEqual(len(frontier), 1)
        self.assertEqual(frontier[0]["move"]["move_san"], "e4")
        self.assertEqual(frontier[0]["ply_depth"], 1)

        drill = self.rm.get_next_trainer_drill("white")
        self.assertIsNotNone(drill)
        self.assertEqual(drill["expected_san"], "e4")
        self.assertEqual(drill["drill_reason"], "frontier_new")
        self.assertEqual(drill["ply_depth"], 1)

        # Step 2: Practice 1. e4 correctly
        res = self.rm.evaluate_trainer_move("white", chess.Board().fen(), "e2e4")
        self.assertTrue(res["correct"])

        # Step 3: Now 1. e4 is learned, 2. d4 becomes the next frontier move at depth 3
        graph2 = self.rm.build_repertoire_tree_graph("white")
        frontier2 = [n for n in graph2.values() if n["is_frontier"]]
        self.assertEqual(len(frontier2), 1)
        self.assertEqual(frontier2[0]["move"]["move_san"], "d4")
        self.assertEqual(frontier2[0]["ply_depth"], 3)
        self.assertEqual(frontier2[0]["path_formatted"], "1. e4 e6")

        # Step 4: Drill should now give 2. d4 with path 1. e4 e6
        drill2 = self.rm.get_next_trainer_drill("white")
        self.assertEqual(drill2["expected_san"], "d4")
        self.assertEqual(drill2["path_formatted"], "1. e4 e6")
        self.assertEqual(drill2["ply_depth"], 3)

    def test_03_in_session_lapse_prioritization(self):
        """
        Tests that when a blunder/mistake occurs during a session,
        it is prioritized in Tier 1 until answered correctly.
        """
        b = chess.Board()
        self.rm.add_repertoire_move("white", b.fen(), "e2e4", is_user_move=True)
        b.push_san("e4")
        self.rm.add_repertoire_move("white", b.fen(), "e7e6", is_user_move=False)
        b.push_san("e6")
        self.rm.add_repertoire_move("white", b.fen(), "d2d4", is_user_move=True)

        # User attempts blunder (d2d3 instead of e2e4)
        res = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": chess.Board().fen(),
            "attempted_uci": "d2d3"
        })
        data = res.json()
        self.assertFalse(data["correct"])
        self.assertIn("prepared_id", data)
        failed_id = data["prepared_id"]

        # Call /train/next passing this failed_id as a session lapse
        drill_res = self.client.get(f"/api/repertoire/train/next?color=white&lapses={failed_id}")
        self.assertEqual(drill_res.status_code, 200)
        drill_data = drill_res.json()
        self.assertEqual(drill_data["id"], failed_id)
        self.assertEqual(drill_data["drill_reason"], "session_lapse")

    def test_04_spaced_repetition_urgency_ordering(self):
        """
        Tests that overdue spaced repetition items are ordered by Ebbinghaus urgency.
        """
        conn = get_db()
        now = int(time.time())

        # Create two practiced user moves
        b1 = chess.Board()
        self.rm.add_repertoire_move("white", b1.fen(), "e2e4", is_user_move=True)
        b1.push_san("e4")
        self.rm.add_repertoire_move("white", b1.fen(), "e7e6", is_user_move=False)
        b1.push_san("e6")
        self.rm.add_repertoire_move("white", b1.fen(), "d2d4", is_user_move=True)

        # Move 1 (e4): Overdue by 1 day (next_review = now - 86400, interval = 1) -> Urgency = 1.0
        # Move 2 (d4): Overdue by 5 days (next_review = now - 5*86400, interval = 1) -> Urgency = 5.0
        conn.execute("""
        UPDATE repertoire_moves
        SET times_practiced = 2, times_correct = 2, interval_days = 1.0, next_review = ?
        WHERE move_uci = 'e2e4'
        """, (now - 86400,))

        conn.execute("""
        UPDATE repertoire_moves
        SET times_practiced = 2, times_correct = 2, interval_days = 1.0, next_review = ?
        WHERE move_uci = 'd2d4'
        """, (now - 5 * 86400,))
        conn.commit()
        conn.close()

        # Drill in due_only mode should pick d4 first because it has higher urgency (5.0 vs 1.0)
        drill = self.rm.get_next_trainer_drill("white", session_mode="due_only")
        self.assertIsNotNone(drill)
        self.assertEqual(drill["expected_san"], "d4")
        self.assertEqual(drill["drill_reason"], "due_review")

    def test_05_api_curriculum_modes(self):
        """
        Tests /api/repertoire/train/next with mode=due_only and mode=frontier_only.
        """
        b = chess.Board()
        self.rm.add_repertoire_move("white", b.fen(), "e2e4", is_user_move=True)

        # In due_only mode, when no moves are due, returns all_due_completed: True
        res = self.client.get("/api/repertoire/train/next?color=white&mode=due_only")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("all_due_completed"))

        # In frontier_only mode, returns 1. e4
        res_front = self.client.get("/api/repertoire/train/next?color=white&mode=frontier_only")
        self.assertEqual(res_front.status_code, 200)
        self.assertEqual(res_front.json()["expected_san"], "e4")
        self.assertEqual(res_front.json()["drill_reason"], "frontier_new")

    def test_06_retry_after_mistake_does_not_promote_unlearned_move(self):
        """
        Tests that when an unlearned move is missed, getting it right on retry:
        - returns is_retry=True
        - does NOT increment times_correct
        - does NOT advance spaced repetition interval
        - keeps mastery_status='unlearned'
        """
        b = chess.Board()
        self.rm.add_repertoire_move("white", b.fen(), "e2e4", is_user_move=True)

        # Confirm initially unlearned: times_correct = 0, times_practiced = 0
        conn = get_db()
        row = conn.execute("SELECT times_correct, times_practiced, interval_days FROM repertoire_moves WHERE move_uci='e2e4'").fetchone()
        conn.close()
        self.assertEqual(row["times_correct"], 0)
        self.assertEqual(row["times_practiced"], 0)

        # 1. User blunders first attempt (d2d3)
        res_blunder = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": b.fen(),
            "attempted_uci": "d2d3",
            "is_retry": False
        })
        self.assertEqual(res_blunder.status_code, 200)
        data_blunder = res_blunder.json()
        self.assertFalse(data_blunder["correct"])
        self.assertEqual(data_blunder["mastery_status"], "unlearned")
        self.assertEqual(data_blunder["times_practiced"], 1)
        self.assertEqual(data_blunder["times_correct"], 0)

        # 2. User plays the correct move on retry (is_retry=True)
        res_retry = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": b.fen(),
            "attempted_uci": "e2e4",
            "is_retry": True
        })
        self.assertEqual(res_retry.status_code, 200)
        data_retry = res_retry.json()
        self.assertTrue(data_retry["correct"])
        self.assertTrue(data_retry.get("is_retry"))
        # Crucial check: times_correct MUST remain 0, and mastery_status MUST NOT be 'learning'
        self.assertEqual(data_retry["times_correct"], 0)
        self.assertEqual(data_retry["mastery_status"], "unlearned")
        self.assertIn("Retried move", data_retry["message"])

        # Check DB to verify persistence invariants
        conn = get_db()
        row_after = conn.execute("SELECT times_correct, times_practiced, interval_days FROM repertoire_moves WHERE move_uci='e2e4'").fetchone()
        conn.close()
        self.assertEqual(row_after["times_correct"], 0)
        self.assertEqual(row_after["times_practiced"], 1)
        self.assertEqual(row_after["interval_days"], 1.0)

    def test_07_hint_assisted_move_is_retry_and_does_not_advance_mastery(self):
        """
        Tests that when a hint is used on an unlearned move, is_retry=True ensures
        the move remains unlearned and does not gain credit.
        """
        b = chess.Board()
        self.rm.add_repertoire_move("white", b.fen(), "e2e4", is_user_move=True)

        res = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": b.fen(),
            "attempted_uci": "e2e4",
            "is_retry": True
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["correct"])
        self.assertTrue(data["is_retry"])
        self.assertEqual(data["times_correct"], 0)
        self.assertEqual(data["mastery_status"], "unlearned")

    def test_08_lapses_only_mode_and_reinforcement_completion(self):
        """
        Tests mode='lapses_only' in /api/repertoire/train/next.
        """
        b = chess.Board()
        self.rm.add_repertoire_move("white", b.fen(), "e2e4", is_user_move=True)
        b.push_san("e4")
        self.rm.add_repertoire_move("white", b.fen(), "e7e6", is_user_move=False)
        b.push_san("e6")
        self.rm.add_repertoire_move("white", b.fen(), "d2d4", is_user_move=True)

        conn = get_db()
        m1_row = conn.execute("SELECT id FROM repertoire_moves WHERE move_uci='e2e4'").fetchone()
        conn.close()
        m1_id = m1_row["id"]

        # If lapses has m1_id, it is served
        res = self.client.get(f"/api/repertoire/train/next?color=white&mode=lapses_only&lapses={m1_id}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["id"], m1_id)
        self.assertEqual(data["drill_reason"], "session_lapse")

        # If lapses is empty or contains no valid moves in graph, returns all_lapses_completed
        res_empty = self.client.get("/api/repertoire/train/next?color=white&mode=lapses_only&lapses=999999")
        self.assertEqual(res_empty.status_code, 200)
        self.assertTrue(res_empty.json().get("all_lapses_completed"))

    def test_09_frontend_trainer_reinforcement_and_retry_contracts(self):
        """
        Tests that frontend/repertoire_trainer.js and frontend/index.html have the required
        contract methods and elements for retry tracking and reinforcement mode.
        """
        with open("frontend/repertoire_trainer.js", "r", encoding="utf-8") as f:
            js = f.read()

        self.assertIn("startReinforcementPhase", js)
        self.assertIn("isReinforcementPhase", js)
        self.assertIn("is_retry:", js)
        self.assertIn("lapses_only", js)
        self.assertIn("_advanceAfterLineEnd", js)

        with open("frontend/index.html", "r", encoding="utf-8") as f:
            html = f.read()

        self.assertIn("modalSessionReinforceBox", html)
        self.assertIn("btnModalReinforceSession", html)
        self.assertIn("modalBtnReinforceCount", html)

    def test_10_mixed_color_trainer_drills(self):
        """
        Tests that mixed color mode drills across both White and Black repertoires.
        """
        # Add White move: 1. e4
        bw = chess.Board()
        self.rm.add_repertoire_move("white", bw.fen(), "e2e4", is_user_move=True, study_name="White Rep")

        # Add Black move: 1. e4 c5
        bb = chess.Board()
        self.rm.add_repertoire_move("black", bb.fen(), "e2e4", is_user_move=False, study_name="Black Rep")
        bb.push_san("e4")
        self.rm.add_repertoire_move("black", bb.fen(), "c7c5", is_user_move=True, study_name="Black Rep")

        # Test next drill with color='mixed'
        drill_mixed = self.rm.get_next_trainer_drill("mixed")
        self.assertIsNotNone(drill_mixed)
        self.assertIn(drill_mixed["repertoire"], ("white", "black"))

        # Test API endpoint
        res = self.client.get("/api/repertoire/train/next?color=mixed")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn(data["repertoire"], ("white", "black"))

        # Progress API with mixed
        res_prog = self.client.get("/api/repertoire/train/progress?color=mixed")
        self.assertEqual(res_prog.status_code, 200)
        prog_data = res_prog.json()
        self.assertEqual(prog_data["repertoire"], "mixed")
        self.assertGreaterEqual(prog_data["total_moves"], 2)

    def test_11_trainer_board_orientation_logic(self):
        """
        Tests that frontend/repertoire_trainer.js has robust drill color resolution
        and correctly rotates the board to Black's perspective when drilling Black positions.
        """
        with open("frontend/repertoire_trainer.js", "r", encoding="utf-8") as f:
            js = f.read()

        self.assertIn("getDrillColor", js)
        self.assertIn("this.getDrillColor(data)", js)
        self.assertIn("this.board.setOrientation(drillColor)", js)

        # Runtime verification via Node harness if available
        import tests.frontend_runtime as frontend_runtime
        if frontend_runtime.node_available():
            scenario = """
            const trainer = new RepertoireTrainer();
            trainer.activeColor = 'mixed';

            const wDrill = { repertoire: 'white', parent_fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1' };
            const bDrill = { repertoire: 'black', parent_fen: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1' };
            const devBlackDrill = { user_color: 'black', parent_fen: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1' };
            const fenOnlyBlack = { parent_fen: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1' };

            const colorW = trainer.getDrillColor(wDrill);
            const colorB = trainer.getDrillColor(bDrill);
            const colorDevB = trainer.getDrillColor(devBlackDrill);
            const colorFenB = trainer.getDrillColor(fenOnlyBlack);

            return {
                pass: (colorW === 'white' && colorB === 'black' && colorDevB === 'black' && colorFenB === 'black'),
                colorW,
                colorB,
                colorDevB,
                colorFenB
            };
            """
            result = frontend_runtime.run_scenario(scenario)
            self.assertTrue(result.get("pass"), f"getDrillColor verification failed: {result}")
            self.assertEqual(result.get("colorW"), "white")
            self.assertEqual(result.get("colorB"), "black")
            self.assertEqual(result.get("colorDevB"), "black")
            self.assertEqual(result.get("colorFenB"), "black")


if __name__ == "__main__":
    unittest.main()

