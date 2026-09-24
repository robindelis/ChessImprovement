import unittest
import chess
import tests.test_setup
from backend.db import init_db, get_db
from backend.repertoire import RepertoireManager, normalize_fen
from fastapi.testclient import TestClient
from backend.api import app

class TestRepertoireStudies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_setup.reset_fixtures()
        init_db()
        cls.rm = RepertoireManager.get_instance()
        cls.client = TestClient(app)

    def setUp(self):
        """Start from an empty repertoire tree.

        CODE-05-05 (escalation): this module was order-dependent.
        `test_study_crud` runs before `test_trainer_drill_from_move_one` and
        leaves moves behind, so the latter's `seed_default_repertoires()` (no
        `force`) silently *skipped* and the drill it asserted on came from the
        previous test's leftovers. The green result was luck, not a contract.
        """
        conn = get_db()
        conn.execute("DELETE FROM study_moves")
        conn.execute("DELETE FROM repertoire_studies")
        conn.execute("DELETE FROM repertoire_moves")
        conn.commit()
        conn.close()

    def test_study_crud(self):
        # 1. Create a new study
        study_name = "Test Caro-Kann Fantasy"
        res = self.rm.create_study(study_name, "white", "Fantasy variation tests")
        self.assertIn(res["status"], ("success", "exists"))

        # 2. Get study by name
        study = self.rm.get_study_by_name(study_name, "white")
        self.assertIsNotNone(study)
        self.assertEqual(study["name"], study_name)
        self.assertEqual(study["repertoire"], "white")

        # 3. Add move to study
        move_res = self.rm.add_repertoire_move(
            "white",
            chess.STARTING_FEN,
            "e2e4",
            is_user_move=True,
            study_name=study_name
        )
        self.assertEqual(move_res["status"], "success")

        # 4. List studies
        studies = self.rm.get_studies("white")
        found = any(s["name"] == study_name for s in studies)
        self.assertTrue(found)

        # 5. Delete study
        del_res = self.rm.delete_study(study["id"], delete_moves=False)
        self.assertEqual(del_res["status"], "success")
        studies_after = self.rm.get_studies("white")
        self.assertFalse(any(s["name"] == study_name for s in studies_after))

    def test_trainer_no_invented_names(self):
        # Seed default repertoires
        self.rm.seed_default_repertoires(force=True)

        # Openings list must only contain named studies
        res = self.rm.get_repertoire_openings("white")
        openings = res["openings"]
        self.assertTrue(len(openings) > 0)

        names = [o["opening_name"] for o in openings]
        # Must include Italian Game
        self.assertIn("Italian Game", names)

        # Must not contain weird single-ply ECO names like "French Defense: Paulsen Variation"
        for name in names:
            self.assertFalse("Paulsen" in name)
            self.assertFalse("Guatemala" in name)

    def test_trainer_drill_from_move_one(self):
        # `force=True`: `seed_default_repertoires()` is a no-op when any move
        # already exists (AGENTS.md §9.4 seeding guard), so without `force` this
        # test's precondition depends on what ran before it.
        self.rm.seed_default_repertoires(force=True)

        # AUDIT FIXER-A / CODE-1: the drill scheduler serves the *frontier* (the deepest untrained
        # move), so this test only sees a move-one drill when the root move is untrained. It used to
        # get that for free because re-adding 1.e4 deleted and re-inserted its row, silently
        # resetting the spaced-repetition counters (that data-loss bug is now fixed). Reset the
        # counters explicitly so the test asserts "a move-one drill is reachable" deterministically
        # instead of depending on the old wipe.
        conn = get_db()
        conn.execute(
            """
            UPDATE repertoire_moves
            SET times_practiced = 0, times_correct = 0, last_practiced = 0, next_review = 0
            WHERE is_user_move = 1 AND parent_fen = ? AND move_uci = 'e2e4'
            """,
            (normalize_fen(chess.STARTING_FEN),),
        )
        conn.commit()
        conn.close()

        # White Italian Game drill
        drill = self.rm.get_next_trainer_drill("white", opening_filter="Italian Game")
        self.assertIsNotNone(drill)
        # Root parent fen is starting position
        self.assertTrue(drill["parent_fen"].startswith("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"))
        self.assertEqual(drill["expected_uci"], "e2e4")
        self.assertEqual(drill["opening_name"], "Italian Game")

    def test_trainer_evaluation_with_study_filter(self):
        self.rm.seed_default_repertoires(force=True)

        # Test evaluating White's 1. e4 with study_filter='Italian Game'
        res = self.rm.evaluate_trainer_move("white", chess.STARTING_FEN, "e2e4", study_filter="Italian Game")
        self.assertTrue(res["correct"])
        # Opponent reply should be 1... e5 (Italian Game line)
        if res.get("opponent_reply"):
            self.assertEqual(res["opponent_reply"]["uci"], "e7e5")

    def test_api_studies_endpoints(self):
        # 1. GET /api/repertoire/studies
        res = self.client.get("/api/repertoire/studies?repertoire=white")
        self.assertEqual(res.status_code, 200)
        studies = res.json()
        self.assertIsInstance(studies, list)

        # 2. POST /api/repertoire/studies
        payload = {
            "name": "API Test Study",
            "repertoire": "black",
            "description": "Created via API"
        }
        res_post = self.client.post("/api/repertoire/studies", json=payload)
        self.assertEqual(res_post.status_code, 200)
        data = res_post.json()
        study_id = data["study"]["id"]

        # 3. DELETE /api/repertoire/studies/{study_id}
        res_del = self.client.delete(f"/api/repertoire/studies/{study_id}")
        self.assertEqual(res_del.status_code, 200)

    def test_trainer_progress_and_mastery(self):
        self.rm.seed_default_repertoires(force=True)
        progress = self.rm.get_trainer_progress("white")
        self.assertIn("total_moves", progress)
        self.assertIn("mastered_count", progress)
        self.assertIn("learning_count", progress)
        self.assertIn("unlearned_count", progress)
        self.assertIn("mastered_pct", progress)
        self.assertIn("studies", progress)
        self.assertGreater(progress["total_moves"], 0)

        # Test API endpoint
        res = self.client.get("/api/repertoire/train/progress?color=white")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["repertoire"], "white")
        self.assertIsInstance(data["studies"], list)

    def test_multi_study_trainer_mixing(self):
        # AUDIT FIXER-A / CODE-2: force-seeding no longer replaces a move the user has already
        # prepared at a position (that was the data-loss bug this audit removed). A previous test
        # module may have left e.g. 1.d4 at the root, so state the test's own precondition
        # explicitly instead of relying on the force-seed silently overwriting it.
        self.rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
        self.rm.seed_default_repertoires(force=True)
        # Create second White study
        self.rm.create_study("Caro-Kann White Prep", "white")
        board = chess.Board()
        board.push_san("e4")
        fen_e4 = board.fen()
        board.push_san("c6")
        fen_c6 = board.fen()
        self.rm.add_repertoire_move("white", fen_e4, "c7c6", is_user_move=False, study_name="Caro-Kann White Prep")
        self.rm.add_repertoire_move("white", fen_c6, "d2d4", is_user_move=True, study_name="Caro-Kann White Prep")

        # Drill without filter should succeed and contain study_name
        drill = self.rm.get_next_trainer_drill("white")
        self.assertIsNotNone(drill)
        self.assertIn("study_name", drill)
        self.assertTrue(len(drill["study_name"]) > 0)

        # Evaluating with study filter keeps opponent reply in that study
        res_italian = self.rm.evaluate_trainer_move("white", chess.STARTING_FEN, "e2e4", study_filter="Italian Game")
        self.assertTrue(res_italian["correct"])
        self.assertEqual(res_italian["opponent_reply"]["san"], "e5")

        res_caro = self.rm.evaluate_trainer_move("white", chess.STARTING_FEN, "e2e4", study_filter="Caro-Kann White Prep")
        self.assertTrue(res_caro["correct"])
        self.assertEqual(res_caro["opponent_reply"]["san"], "c6")

    def test_group_subtree_into_study(self):
        # Build a small tree: 1. e4 c6 2. d4 d5 3. Nc3
        b = chess.Board()
        f0 = b.fen()
        b.push_san("e4")
        f1 = b.fen()
        b.push_san("c6")
        f2 = b.fen()
        b.push_san("d4")
        f3 = b.fen()
        b.push_san("d5")
        f4 = b.fen()
        b.push_san("Nc3")
        f5 = b.fen()

        self.rm.add_repertoire_move("white", f0, "e2e4", is_user_move=True)
        self.rm.add_repertoire_move("white", f1, "c7c6", is_user_move=False)
        self.rm.add_repertoire_move("white", f2, "d2d4", is_user_move=True)
        self.rm.add_repertoire_move("white", f3, "d7d5", is_user_move=False)
        self.rm.add_repertoire_move("white", f4, "b1c3", is_user_move=True)

        # Group starting from position after 1... c6 (f2)
        res = self.rm.group_subtree_into_study(
            repertoire="white",
            start_fen=f2,
            study_name="Caro-Kann Main Line",
            include_path_from_root=True,
            path_uci=["e2e4", "c7c6"]
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["study_name"], "Caro-Kann Main Line")
        self.assertEqual(res["descendant_moves"], 3)
        self.assertEqual(res["prelude_moves"], 2)

        # Verify study exists and has study_moves
        studies = self.rm.get_studies("white")
        caro = next((s for s in studies if s["name"] == "Caro-Kann Main Line"), None)
        self.assertIsNotNone(caro)
        self.assertEqual(caro["total_moves"], 5)

        # Test API endpoint
        api_res = self.client.post("/api/repertoire/studies/group_subtree", json={
            "repertoire": "white",
            "start_fen": f4,
            "study_name": "Caro-Kann Sub-Variation",
            "include_path_from_root": False
        })
        self.assertEqual(api_res.status_code, 200)
        api_data = api_res.json()
        self.assertEqual(api_data["status"], "success")
        self.assertEqual(api_data["descendant_moves"], 1)
        self.assertEqual(api_data["prelude_moves"], 0)

if __name__ == "__main__":
    unittest.main()
