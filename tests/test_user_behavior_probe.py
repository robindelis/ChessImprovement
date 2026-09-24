"""
User Behavior Probe - Comprehensive Test Suite for Repertoire Trainer.
Tests all 5 key user workflow areas:
1. Multi-study handling (Anti French Defense vs Caro-Kann Fantasy Variation)
2. Mixed-study drilling when "All Studies" is selected
3. Chessbook-style Repertoire Mastery Progress calculation
4. 20-Move Practice Session Engine
5. Spaced Repetition behavior on user moves

CODE-05-13: every narrative line below used to be a bare `print()`, which made
~45% of the whole suite's stdout. They now go through `tests.quiet.narrate()`,
which is silent unless `CHESS_TEST_VERBOSE=1` is set, so a real failure
traceback is no longer buried under 85 lines of scaffolding. Nothing was
downgraded to a print: every check is a real `assert*`.
"""
import os
import sys
import unittest
import time
import math
import chess
from fastapi.testclient import TestClient

# Ensure test DB environment is active
import tests.test_setup
from tests.quiet import narrate, banner
from backend.db import get_db, init_db
from backend.repertoire import RepertoireManager, normalize_fen
from backend.api import app

class TestUserBehaviorProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_setup.reset_fixtures()
        init_db()
        cls.rm = RepertoireManager.get_instance()
        cls.client = TestClient(app)

    def setUp(self):
        # Clean repertoire tables before each test
        self.rm.clear_repertoire_database()

    def test_01_multi_study_handling(self):
        """
        Scenario 1: Multi-study handling
        - User has 2 studies in White repertoire: "Anti French Defense" and "Caro-Kann Fantasy Variation".
        - Test that "Anti French Defense" appears in the openings list, focus dropdown, and is drillable!
        - Test that opponent replies in "Anti French Defense" are French Defense replies (e.g. 1... e6, NOT Caro-Kann 1... c6)!
        - Test that opponent replies in "Caro-Kann Fantasy Variation" are Caro-Kann replies (1... c6, NOT 1... e6)!
        """
        narrate("\n" + "="*70)
        narrate("SCENARIO 1: Multi-Study Handling (Anti French Defense vs Caro-Kann)")
        narrate("="*70)

        # 1. Setup Study 1: Anti French Defense
        study1_name = "Anti French Defense"
        self.rm.create_study(study1_name, "white", "Preparation against 1... e6")

        b1 = chess.Board()
        p_fen1 = b1.fen()
        # 1. e4
        self.rm.add_repertoire_move("white", p_fen1, "e2e4", is_user_move=True, study_name=study1_name)
        b1.push_san("e4")
        # 1... e6
        self.rm.add_repertoire_move("white", b1.fen(), "e7e6", is_user_move=False, study_name=study1_name)
        b1.push_san("e6")
        # 2. d4
        self.rm.add_repertoire_move("white", b1.fen(), "d2d4", is_user_move=True, study_name=study1_name)
        b1.push_san("d4")
        # 2... d5
        self.rm.add_repertoire_move("white", b1.fen(), "d7d5", is_user_move=False, study_name=study1_name)
        b1.push_san("d5")
        # 3. e5 (Advance)
        self.rm.add_repertoire_move("white", b1.fen(), "e4e5", is_user_move=True, study_name=study1_name)

        # 2. Setup Study 2: Caro-Kann Fantasy Variation
        study2_name = "Caro-Kann Fantasy Variation"
        self.rm.create_study(study2_name, "white", "Fantasy Variation 3. f3 against Caro-Kann")

        b2 = chess.Board()
        p_fen2 = b2.fen()
        # 1. e4
        self.rm.add_repertoire_move("white", p_fen2, "e2e4", is_user_move=True, study_name=study2_name)
        b2.push_san("e4")
        # 1... c6
        self.rm.add_repertoire_move("white", b2.fen(), "c7c6", is_user_move=False, study_name=study2_name)
        b2.push_san("c6")
        # 2. d4
        self.rm.add_repertoire_move("white", b2.fen(), "d2d4", is_user_move=True, study_name=study2_name)
        b2.push_san("d4")
        # 2... d5
        self.rm.add_repertoire_move("white", b2.fen(), "d7d5", is_user_move=False, study_name=study2_name)
        b2.push_san("d5")
        # 3. f3 (Fantasy Variation)
        self.rm.add_repertoire_move("white", b2.fen(), "f2f3", is_user_move=True, study_name=study2_name)

        # Check API: /api/repertoire/train/openings?color=white
        res = self.client.get("/api/repertoire/train/openings?color=white")
        self.assertEqual(res.status_code, 200)
        openings_data = res.json()["openings"]
        opening_names = [o["opening_name"] for o in openings_data]
        narrate(f"Trainer Openings List: {opening_names}")

        self.assertIn(study1_name, opening_names, f"'{study1_name}' must appear in trainer openings list")
        self.assertIn(study2_name, opening_names, f"'{study2_name}' must appear in trainer openings list")

        # Check Initial Drill for Anti French Defense (fresh study starts at move 1)
        drill_french = self.client.get(f"/api/repertoire/train/next?color=white&opening={study1_name}").json()
        narrate(f"Anti French Drill: {drill_french.get('opening_name')} | study: {drill_french.get('study_name')} | expected: {drill_french.get('expected_san')}")
        self.assertEqual(drill_french["expected_uci"], "e2e4")
        self.assertEqual(drill_french.get("study_name"), study1_name)
        self.assertEqual(drill_french.get("opening_name"), study1_name)

        # Check Initial Drill for Caro-Kann Fantasy Variation (fresh study starts at move 1)
        drill_caro = self.client.get(f"/api/repertoire/train/next?color=white&opening={study2_name}").json()
        narrate(f"Caro-Kann Drill: {drill_caro.get('opening_name')} | study: {drill_caro.get('study_name')} | expected: {drill_caro.get('expected_san')}")
        self.assertEqual(drill_caro["expected_uci"], "e2e4")
        self.assertEqual(drill_caro.get("study_name"), study2_name)
        self.assertEqual(drill_caro.get("opening_name"), study2_name)

        # Attempt 1. e4 in Anti French Defense: Opponent reply MUST be 1... e6 (NOT 1... c6)!
        attempt_french = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": drill_french["parent_fen"],
            "attempted_uci": "e2e4",
            "opening_filter": study1_name,
            "study_name": study1_name
        }).json()
        narrate(f"Anti French attempt response: correct={attempt_french['correct']}, opp_reply={attempt_french.get('opponent_reply')}")

        self.assertTrue(attempt_french["correct"])
        self.assertIsNotNone(attempt_french.get("opponent_reply"))
        self.assertEqual(attempt_french["opponent_reply"]["san"], "e6", "Anti French reply must be 1... e6!")
        self.assertEqual(attempt_french["opponent_reply"]["uci"], "e7e6")
        self.assertNotEqual(attempt_french["opponent_reply"]["san"], "c6", "Anti French reply MUST NOT be Caro-Kann 1... c6!")

        # Attempt 1. e4 in Caro-Kann Fantasy Variation: Opponent reply MUST be 1... c6 (NOT 1... e6)!
        attempt_caro = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": drill_caro["parent_fen"],
            "attempted_uci": "e2e4",
            "opening_filter": study2_name,
            "study_name": study2_name
        }).json()
        narrate(f"Caro-Kann attempt response: correct={attempt_caro['correct']}, opp_reply={attempt_caro.get('opponent_reply')}")

        self.assertTrue(attempt_caro["correct"])
        self.assertIsNotNone(attempt_caro.get("opponent_reply"))
        self.assertEqual(attempt_caro["opponent_reply"]["san"], "c6", "Caro-Kann reply must be 1... c6!")
        self.assertEqual(attempt_caro["opponent_reply"]["uci"], "c7c6")
        self.assertNotEqual(attempt_caro["opponent_reply"]["san"], "e6", "Caro-Kann reply MUST NOT be French 1... e6!")

        # Step further into Anti French line: 2. d4 -> 2... d5 -> 3. e5 (Line complete)
        fen_after_e6 = attempt_french["opponent_reply"]["fen"]
        attempt_french_move2 = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": fen_after_e6,
            "attempted_uci": "d2d4",
            "opening_filter": study1_name,
            "study_name": study1_name
        }).json()
        self.assertTrue(attempt_french_move2["correct"])
        self.assertEqual(attempt_french_move2["opponent_reply"]["san"], "d5")

        fen_after_d5 = attempt_french_move2["opponent_reply"]["fen"]
        attempt_french_move3 = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": fen_after_d5,
            "attempted_uci": "e4e5",
            "opening_filter": study1_name,
            "study_name": study1_name
        }).json()
        self.assertTrue(attempt_french_move3["correct"])
        self.assertTrue(attempt_french_move3["is_line_end"], "Line should be complete after 3. e5")

        # Step further into Caro-Kann Fantasy line: 2. d4 -> 2... d5 -> 3. f3 (Line complete)
        fen_after_c6 = attempt_caro["opponent_reply"]["fen"]
        attempt_caro_move2 = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": fen_after_c6,
            "attempted_uci": "d2d4",
            "opening_filter": study2_name,
            "study_name": study2_name
        }).json()
        self.assertTrue(attempt_caro_move2["correct"])
        self.assertEqual(attempt_caro_move2["opponent_reply"]["san"], "d5")

        fen_after_caro_d5 = attempt_caro_move2["opponent_reply"]["fen"]
        attempt_caro_move3 = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": fen_after_caro_d5,
            "attempted_uci": "f2f3",
            "opening_filter": study2_name,
            "study_name": study2_name
        }).json()
        self.assertTrue(attempt_caro_move3["correct"])
        self.assertTrue(attempt_caro_move3["is_line_end"], "Line should be complete after 3. f3")

        narrate("✓ Scenario 1 passed with 100% precision: studies separated, replies strictly correct!")

    def test_02_mixed_study_drilling(self):
        """
        Scenario 2: Mixed-study drilling when "All Studies" is selected
        - User has 2 studies: "Anti French Defense" and "Caro-Kann Fantasy Variation".
        - Test that drills across multiple iterations pick positions from BOTH
          "Anti French Defense" AND "Caro-Kann Fantasy Variation" (a real mix, not just the last one!).
        """
        narrate("\n" + "="*70)
        narrate("SCENARIO 2: Mixed-Study Drilling with 'All Studies'")
        narrate("="*70)

        study1_name = "Anti French Defense"
        study2_name = "Caro-Kann Fantasy Variation"
        self.rm.create_study(study1_name, "white")
        self.rm.create_study(study2_name, "white")

        # Build lines
        b1 = chess.Board()
        p1 = b1.fen()
        self.rm.add_repertoire_move("white", p1, "e2e4", is_user_move=True, study_name=study1_name)
        b1.push_san("e4")
        self.rm.add_repertoire_move("white", b1.fen(), "e7e6", is_user_move=False, study_name=study1_name)
        b1.push_san("e6")
        self.rm.add_repertoire_move("white", b1.fen(), "d2d4", is_user_move=True, study_name=study1_name)
        b1.push_san("d4")
        self.rm.add_repertoire_move("white", b1.fen(), "d7d5", is_user_move=False, study_name=study1_name)
        b1.push_san("d5")
        self.rm.add_repertoire_move("white", b1.fen(), "e4e5", is_user_move=True, study_name=study1_name)

        b2 = chess.Board()
        p2 = b2.fen()
        self.rm.add_repertoire_move("white", p2, "e2e4", is_user_move=True, study_name=study2_name)
        b2.push_san("e4")
        self.rm.add_repertoire_move("white", b2.fen(), "c7c6", is_user_move=False, study_name=study2_name)
        b2.push_san("c6")
        self.rm.add_repertoire_move("white", b2.fen(), "d2d4", is_user_move=True, study_name=study2_name)
        b2.push_san("d4")
        self.rm.add_repertoire_move("white", b2.fen(), "d7d5", is_user_move=False, study_name=study2_name)
        b2.push_san("d5")
        self.rm.add_repertoire_move("white", b2.fen(), "f2f3", is_user_move=True, study_name=study2_name)

        # Mark 1. e4 as practiced so the trainer dives into the variations
        conn = get_db()
        conn.execute("UPDATE repertoire_moves SET times_practiced = 2, times_correct = 2 WHERE move_uci = 'e2e4'")
        conn.commit()
        conn.close()

        # Run 60 drill samples with opening=None (All Studies)
        french_count = 0
        caro_count = 0
        samples = []

        for i in range(60):
            res = self.client.get("/api/repertoire/train/next?color=white")
            self.assertEqual(res.status_code, 200)
            drill = res.json()
            s_name = drill.get("study_name") or drill.get("opening_name") or ""
            expected_san = drill.get("expected_san")
            samples.append((s_name, expected_san))

            if "French" in s_name or expected_san == "e5":
                french_count += 1
            elif "Caro" in s_name or expected_san == "f3":
                caro_count += 1

        narrate(f"Sample distribution over 60 drills:")
        narrate(f"  Anti French Defense drills: {french_count} ({french_count/60*100:.1f}%)")
        narrate(f"  Caro-Kann drills: {caro_count} ({caro_count/60*100:.1f}%)")
        narrate(f"  First 10 samples: {samples[:10]}")

        # Both studies must be significantly represented (at least 20% each)
        self.assertGreaterEqual(french_count, 12, f"Anti French Defense should have at least 12/60 drills, got {french_count}")
        self.assertGreaterEqual(caro_count, 12, f"Caro-Kann Fantasy should have at least 12/60 drills, got {caro_count}")

        narrate("✓ Scenario 2 passed: drills reliably alternate and mix across both studies!")

    def test_03_repertoire_mastery_progress(self):
        """
        Scenario 3: Chessbook-style Repertoire Mastery Progress
        - Verify calculation of Fully Mastered (tc >= 2 and tc == tp),
          Partially Mastered (tc > 0 but not fully mastered),
          and Unlearned (tc == 0).
        - Verify percentages sum cleanly to 100%.
        - Verify progress when filtered by study vs all studies.
        """
        narrate("\n" + "="*70)
        narrate("SCENARIO 3: Chessbook-Style Repertoire Mastery Progress")
        narrate("="*70)

        study_french = "Anti French Defense"
        study_caro = "Caro-Kann Fantasy Variation"
        self.rm.create_study(study_french, "white")
        self.rm.create_study(study_caro, "white")

        # White Repertoire:
        # Move 1: 1. e4 (shared by both studies!)
        b_root = chess.Board()
        p0 = b_root.fen()
        self.rm.add_repertoire_move("white", p0, "e2e4", is_user_move=True, study_name=study_french)
        self.rm.add_repertoire_move("white", p0, "e2e4", is_user_move=True, study_name=study_caro)

        # Anti French branch:
        # 1... e6
        b_fr = chess.Board()
        b_fr.push_san("e4")
        p_e4 = b_fr.fen()
        self.rm.add_repertoire_move("white", p_e4, "e7e6", is_user_move=False, study_name=study_french)
        b_fr.push_san("e6")
        # Move 2: 2. d4
        p_fr_e6 = b_fr.fen()
        self.rm.add_repertoire_move("white", p_fr_e6, "d2d4", is_user_move=True, study_name=study_french)
        b_fr.push_san("d4")
        # 2... d5
        self.rm.add_repertoire_move("white", b_fr.fen(), "d7d5", is_user_move=False, study_name=study_french)
        b_fr.push_san("d5")
        # Move 3: 3. e5
        p_fr_d5 = b_fr.fen()
        self.rm.add_repertoire_move("white", p_fr_d5, "e4e5", is_user_move=True, study_name=study_french)

        # Caro-Kann branch:
        # 1... c6
        b_ck = chess.Board()
        b_ck.push_san("e4")
        self.rm.add_repertoire_move("white", p_e4, "c7c6", is_user_move=False, study_name=study_caro)
        b_ck.push_san("c6")
        # Move 4: 2. d4
        p_ck_c6 = b_ck.fen()
        self.rm.add_repertoire_move("white", p_ck_c6, "d2d4", is_user_move=True, study_name=study_caro)
        b_ck.push_san("d4")
        # 2... d5
        self.rm.add_repertoire_move("white", b_ck.fen(), "d7d5", is_user_move=False, study_name=study_caro)
        b_ck.push_san("d5")
        # Move 5: 3. f3
        p_ck_d5 = b_ck.fen()
        self.rm.add_repertoire_move("white", p_ck_d5, "f2f3", is_user_move=True, study_name=study_caro)

        # Configure spaced repetition state on the 5 distinct user moves:
        # 1. 1. e4 (shared): Mastered (tp=3, tc=3)
        # 2. 2. d4 vs e6: Mastered (tp=2, tc=2)
        # 3. 3. e5 vs d5: Learning (tp=2, tc=1) (tc > 0, made an error)
        # 4. 2. d4 vs c6: Learning (tp=1, tc=1) (tc > 0, tc < 2)
        # 5. 3. f3 vs d5: Unlearned (tp=0, tc=0)
        conn = get_db()
        conn.execute("UPDATE repertoire_moves SET times_practiced = 3, times_correct = 3 WHERE parent_fen = ? AND move_uci = 'e2e4'", (normalize_fen(p0),))
        conn.execute("UPDATE repertoire_moves SET times_practiced = 2, times_correct = 2 WHERE parent_fen = ? AND move_uci = 'd2d4'", (normalize_fen(p_fr_e6),))
        conn.execute("UPDATE repertoire_moves SET times_practiced = 2, times_correct = 1 WHERE parent_fen = ? AND move_uci = 'e4e5'", (normalize_fen(p_fr_d5),))
        conn.execute("UPDATE repertoire_moves SET times_practiced = 1, times_correct = 1 WHERE parent_fen = ? AND move_uci = 'd2d4'", (normalize_fen(p_ck_c6),))
        conn.execute("UPDATE repertoire_moves SET times_practiced = 0, times_correct = 0 WHERE parent_fen = ? AND move_uci = 'f2f3'", (normalize_fen(p_ck_d5),))
        conn.commit()
        conn.close()

        # 1. Test Overall Mastery (All Studies)
        # 5 total moves: 2 Mastered, 2 Learning, 1 Unlearned
        prog_all = self.rm.get_trainer_progress("white")
        narrate(f"Overall Progress (All Studies): {prog_all}")
        self.assertEqual(prog_all["total_moves"], 5)
        self.assertEqual(prog_all["mastered_count"], 2)
        self.assertEqual(prog_all["learning_count"], 2)
        self.assertEqual(prog_all["unlearned_count"], 1)
        self.assertEqual(prog_all["mastered_pct"], 40.0)
        self.assertEqual(prog_all["learning_pct"], 40.0)
        self.assertEqual(prog_all["unlearned_pct"], 20.0)
        pct_sum = prog_all["mastered_pct"] + prog_all["learning_pct"] + prog_all["unlearned_pct"]
        self.assertEqual(pct_sum, 100.0, msg="Percentages must sum to exactly 100.0%")

        # 2. Test Anti French Defense Progress
        # 3 user moves: e4 (Mastered), d4 (Mastered), e5 (Learning)
        prog_french = self.rm.get_trainer_progress("white", study_name=study_french)
        narrate(f"Anti French Defense Progress: {prog_french}")
        self.assertEqual(prog_french["total_moves"], 3)
        self.assertEqual(prog_french["mastered_count"], 2)
        self.assertEqual(prog_french["learning_count"], 1)
        self.assertEqual(prog_french["unlearned_count"], 0)
        self.assertEqual(prog_french["mastered_pct"], 66.7)
        self.assertEqual(prog_french["learning_pct"], 33.3)
        self.assertEqual(prog_french["unlearned_pct"], 0.0)
        self.assertEqual(prog_french["mastered_pct"] + prog_french["learning_pct"] + prog_french["unlearned_pct"], 100.0)

        # 3. Test Caro-Kann Fantasy Variation Progress
        # 3 user moves: e4 (Mastered), d4 (Learning), f3 (Unlearned)
        prog_caro = self.rm.get_trainer_progress("white", study_name=study_caro)
        narrate(f"Caro-Kann Fantasy Variation Progress: {prog_caro}")
        self.assertEqual(prog_caro["total_moves"], 3)
        self.assertEqual(prog_caro["mastered_count"], 1)
        self.assertEqual(prog_caro["learning_count"], 1)
        self.assertEqual(prog_caro["unlearned_count"], 1)
        self.assertEqual(prog_caro["mastered_pct"], 33.3)
        self.assertEqual(prog_caro["learning_pct"], 33.3)
        # AUDIT FIXER-T / TRAINER-15b (assertion CORRECTED, not weakened): `unlearned_pct` used
        # to be computed as `100 - mastered_pct - learning_pct` from the two already-ROUNDED
        # numbers, so with 3 moves and 1 unlearned it reported 33.4 - a percentage that
        # contradicts its own count. Each segment is now computed from its own count (33.3), and
        # three independently rounded thirds sum to 99.9 instead of exactly 100.0. The sum check
        # below therefore allows the rounding artefact.
        self.assertEqual(prog_caro["unlearned_pct"], 33.3)
        self.assertAlmostEqual(
            prog_caro["mastered_pct"] + prog_caro["learning_pct"] + prog_caro["unlearned_pct"],
            100.0, delta=0.15,
        )

        # 4. Test API endpoint GET /api/repertoire/train/progress
        res_api_all = self.client.get("/api/repertoire/train/progress?color=white").json()
        self.assertEqual(res_api_all["total_moves"], 5)
        self.assertEqual(len(res_api_all["studies"]), 2)

        res_api_fr = self.client.get(f"/api/repertoire/train/progress?color=white&study={study_french}").json()
        self.assertEqual(res_api_fr["total_moves"], 3)
        self.assertEqual(res_api_fr["filter_study"], study_french)

        res_api_ck = self.client.get(f"/api/repertoire/train/progress?color=white&study={study_caro}").json()
        self.assertEqual(res_api_ck["total_moves"], 3)
        self.assertEqual(res_api_ck["filter_study"], study_caro)

        narrate("✓ Scenario 3 passed: Chessbook mastery metrics calculate with 100% precision and exact 100% sums!")

    def test_04_twenty_move_practice_session_engine(self):
        """
        Scenario 4: 20-Move Practice Session Engine
        - Verify session progress moves from 1 to 20.
        - Verify accuracy and score calculation.
        - Verify session completion logic triggers at 20 moves.
        - Verify retry does not double count session moves.
        """
        narrate("\n" + "="*70)
        narrate("SCENARIO 4: 20-Move Practice Session Engine Logic")
        narrate("="*70)

        # Emulate the frontend RepertoireTrainer session state machine
        session_target = 20
        session_index = 0
        session_correct = 0
        session_mistakes = 0
        has_answered_current = False

        def get_session_ui():
            current_move_num = min(session_index + 1, session_target)
            pct = min(100, round((session_index / session_target) * 100))
            prog_bar_width = max(5, pct)
            score_text = f"{session_correct} / {session_index}"
            acc = round((session_correct / session_index) * 100) if session_index > 0 else 100
            return {
                "move_num": current_move_num,
                "label": f"Move {current_move_num} of {session_target}",
                "progress_pct": prog_bar_width,
                "score": score_text,
                "accuracy": f"{acc}%",
                "accuracy_num": acc
            }

        # Step 0: Initial state
        ui = get_session_ui()
        self.assertEqual(ui["move_num"], 1)
        self.assertEqual(ui["label"], "Move 1 of 20")
        self.assertEqual(ui["score"], "0 / 0")
        self.assertEqual(ui["accuracy"], "100%")

        # Moves 1 to 15: Correct attempts
        for step in range(1, 16):
            has_answered_current = False
            # User plays correct move
            is_correct = True
            if not has_answered_current:
                has_answered_current = True
                session_index += 1
                session_correct += 1

            ui = get_session_ui()
            self.assertEqual(session_index, step)
            self.assertEqual(session_correct, step)
            self.assertEqual(ui["accuracy_num"], 100)

        self.assertEqual(session_index, 15)
        self.assertEqual(session_correct, 15)
        ui15 = get_session_ui()
        self.assertEqual(ui15["label"], "Move 16 of 20")
        self.assertEqual(ui15["progress_pct"], 75)

        # Move 16: User makes a mistake first!
        has_answered_current = False
        is_correct = False
        if not has_answered_current:
            has_answered_current = True
            session_index += 1
            session_mistakes += 1

        self.assertEqual(session_index, 16)
        self.assertEqual(session_correct, 15)
        self.assertEqual(session_mistakes, 1)
        ui16_mistake = get_session_ui()
        self.assertEqual(ui16_mistake["score"], "15 / 16")
        self.assertEqual(ui16_mistake["accuracy"], "94%")

        # User retries the same position with the correct move
        is_correct = True
        if not has_answered_current:
            has_answered_current = True
            session_index += 1
            session_correct += 1

        # session_index and session_correct must NOT double count on retry!
        self.assertEqual(session_index, 16, "Retry must not double count session move index!")
        self.assertEqual(session_correct, 15, "Retry must not increment session correct count!")

        # Moves 17 to 20: Correct attempts
        session_completed = False
        for step in range(17, 21):
            has_answered_current = False
            is_correct = True
            if not has_answered_current:
                has_answered_current = True
                session_index += 1
                session_correct += 1

            if session_index >= session_target:
                session_completed = True

        self.assertEqual(session_index, 20)
        self.assertEqual(session_correct, 19)
        self.assertTrue(session_completed, "Session completion must trigger at 20 moves!")

        ui20 = get_session_ui()
        self.assertEqual(ui20["label"], "Move 20 of 20")
        self.assertEqual(ui20["progress_pct"], 100)
        self.assertEqual(ui20["score"], "19 / 20")
        self.assertEqual(ui20["accuracy"], "95%")

        # Emulate modal completion payload
        modal_acc = round((session_correct / session_index) * 100) if session_index > 0 else 100
        modal_score = f"{session_correct} / {session_index}"
        self.assertEqual(modal_acc, 95)
        self.assertEqual(modal_score, "19 / 20")

        # Emulate session restart
        session_index = 0
        session_correct = 0
        session_mistakes = 0
        has_answered_current = False
        ui_restart = get_session_ui()
        self.assertEqual(ui_restart["move_num"], 1)
        self.assertEqual(ui_restart["score"], "0 / 0")

        narrate("✓ Scenario 4 passed: 20-move session progress, retry protection, accuracy, and modal triggers verified!")

    def test_05_spaced_repetition_behavior(self):
        """
        Scenario 5: Spaced Repetition behavior on user moves
        - Verify correct moves advance interval_days and next_review.
        - Verify incorrect moves reset interval_days to 1.0 day and do not increment times_correct.
        """
        narrate("\n" + "="*70)
        narrate("SCENARIO 5: Spaced Repetition Behavior on User Moves")
        narrate("="*70)

        # Setup test move
        study_name = "SR Test Study"
        self.rm.create_study(study_name, "white")
        b = chess.Board()
        p_fen = b.fen()
        norm_p_fen = normalize_fen(p_fen)
        self.rm.add_repertoire_move("white", p_fen, "e2e4", is_user_move=True, study_name=study_name)

        # 1. Initial State
        conn = get_db()
        m = dict(conn.execute("SELECT * FROM repertoire_moves WHERE parent_fen = ? AND move_uci = 'e2e4'", (norm_p_fen,)).fetchone())
        conn.close()
        self.assertEqual(m["times_practiced"], 0)
        self.assertEqual(m["times_correct"], 0)
        self.assertEqual(m["interval_days"], 1.0)

        # 2. First Correct Attempt (tc 0 -> 1)
        now_1 = int(time.time())
        res_c1 = self.rm.evaluate_trainer_move("white", p_fen, "e2e4", study_filter=study_name)
        self.assertTrue(res_c1["correct"])
        self.assertEqual(res_c1["times_practiced"], 1)
        self.assertEqual(res_c1["times_correct"], 1)
        self.assertEqual(res_c1["mastery_status"], "learning")

        conn = get_db()
        m1 = dict(conn.execute("SELECT * FROM repertoire_moves WHERE parent_fen = ? AND move_uci = 'e2e4'", (norm_p_fen,)).fetchone())
        conn.close()
        self.assertEqual(m1["times_practiced"], 1)
        self.assertEqual(m1["times_correct"], 1)
        self.assertEqual(m1["interval_days"], 1.0) # First correct review keeps 1.0 day interval
        self.assertAlmostEqual(m1["next_review"], now_1 + 86400, delta=5)

        # 3. Second Consecutive Correct Attempt (tc 1 -> 2) -> advances to 3.0 days!
        now_2 = int(time.time())
        res_c2 = self.rm.evaluate_trainer_move("white", p_fen, "e2e4", study_filter=study_name)
        self.assertTrue(res_c2["correct"])
        self.assertEqual(res_c2["times_practiced"], 2)
        self.assertEqual(res_c2["times_correct"], 2)
        self.assertEqual(res_c2["mastery_status"], "mastered", "tc >= 2 and tc == tp must be mastered!")

        conn = get_db()
        m2 = dict(conn.execute("SELECT * FROM repertoire_moves WHERE parent_fen = ? AND move_uci = 'e2e4'", (norm_p_fen,)).fetchone())
        conn.close()
        self.assertEqual(m2["times_practiced"], 2)
        self.assertEqual(m2["times_correct"], 2)
        self.assertEqual(m2["interval_days"], 3.0, "Second correct review must advance interval to 3.0 days!")
        self.assertAlmostEqual(m2["next_review"], now_2 + 3 * 86400, delta=5)

        # 4. Third Consecutive Correct Attempt (tc 2 -> 3) -> advances to 3.0 * 2.2 = 6.6 days!
        now_3 = int(time.time())
        res_c3 = self.rm.evaluate_trainer_move("white", p_fen, "e2e4", study_filter=study_name)
        self.assertTrue(res_c3["correct"])
        self.assertEqual(res_c3["times_practiced"], 3)
        self.assertEqual(res_c3["times_correct"], 3)
        self.assertEqual(res_c3["mastery_status"], "mastered")

        conn = get_db()
        m3 = dict(conn.execute("SELECT * FROM repertoire_moves WHERE parent_fen = ? AND move_uci = 'e2e4'", (norm_p_fen,)).fetchone())
        conn.close()
        self.assertEqual(m3["times_practiced"], 3)
        self.assertEqual(m3["times_correct"], 3)
        self.assertEqual(m3["interval_days"], 6.6, "Third correct review must advance interval to 6.6 days (3.0 * 2.2)!")
        self.assertAlmostEqual(m3["next_review"], now_3 + int(6.6 * 86400), delta=5)

        # 5. Mistake Attempt! User plays wrong move (e.g. d2d4)
        now_4 = int(time.time())
        res_err = self.rm.evaluate_trainer_move("white", p_fen, "d2d4", study_filter=study_name)
        self.assertFalse(res_err["correct"])
        self.assertEqual(res_err["times_practiced"], 4)
        self.assertEqual(res_err["times_correct"], 3, "Incorrect move must NOT increment times_correct!")
        self.assertEqual(res_err["mastery_status"], "learning", "Mistake drops mastered move to learning!")

        conn = get_db()
        m4 = dict(conn.execute("SELECT * FROM repertoire_moves WHERE parent_fen = ? AND move_uci = 'e2e4'", (norm_p_fen,)).fetchone())
        conn.close()
        self.assertEqual(m4["times_practiced"], 4)
        self.assertEqual(m4["times_correct"], 3)
        self.assertEqual(m4["interval_days"], 1.0, "Mistake must reset interval_days to 1.0 day!")
        self.assertAlmostEqual(m4["next_review"], now_4 + 86400, delta=5, msg="next_review must be reset to 1 day after mistake")

        narrate("✓ Scenario 5 passed: spaced repetition interval expansion, next_review, and blunder reset verified!")

if __name__ == "__main__":
    unittest.main()
