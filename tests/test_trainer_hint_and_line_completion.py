import unittest
import os
import sqlite3
import tests.test_setup
from backend.db import get_db, init_db
from backend.repertoire import RepertoireManager

class TestTrainerHintAndLineCompletion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This module used to hijack `CHESS_DB_PATH` to a *second*, private
        # database ("/tmp/test_trainer_hint.db") and then delete the env var in
        # `tearDownClass` — silently repointing every later test module at
        # whatever `backend.db.DB_PATH` happened to be. Use the one temp DB that
        # `tests/test_setup` owns and reset it to the synthetic baseline instead.
        tests.test_setup.reset_fixtures()
        cls.test_db_path = tests.test_setup.temp_db_path()
        init_db()

    @classmethod
    def tearDownClass(cls):
        # Leave the process pointing at the suite's temp DB, never at production.
        tests.test_setup.assert_isolated()

    def setUp(self):
        # Clear repertoire tables
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM repertoire_moves")
        c.execute("DELETE FROM study_moves")
        c.execute("DELETE FROM repertoire_studies")
        conn.commit()
        conn.close()
        self.manager = RepertoireManager()

    def test_unanswered_opponent_branch_is_not_played_and_not_called_a_line_end(self):
        """
        AUDIT FIXER-V / TRAINER-09 — REWRITTEN. This test previously asserted the behaviour the
        project owner has since OVERRULED, namely:

            White plays O-O.  Opponent replies O-O.  White has NO further move.
            => opponent_reply = {e8g8, O-O}  and  is_line_end = True

        The owner ruled: **"A line should never finish on an opponent's move."** The old
        expectation played the opponent's `O-O`, left the board sitting on the opponent's move with
        nothing to play, and then called that position a completed line.

        The new rule, and what is asserted instead:
          * the unanswered opponent move is NOT offered at all (`opponent_reply is None`) - the
            trainer refuses to move the board onto a position the user cannot answer;
          * `is_line_end` is `True` only because the line stops on the **user's own** `O-O`, and
            `line_end_reason` says why: `unanswered_opponent_branches` (a gap), not
            `repertoire_exhausted` (a real completion);
          * the client can therefore say "add a reply in the Builder" instead of celebrating.

        The old assertions on `next_expected_uci is None` are kept: with no reply played there is
        nothing to answer, which is still the correct value and still prevents the wrong-hint bug.
        """
        # Create study
        self.manager.create_study("Anti French Defense", "white")

        # Position before White's O-O:
        # rnbqk2r/pp2bppp/4p3/3pPn2/3p1P2/P2B1N2/1PP3PP/RNBQK2R w KQkq -
        fen_before_white_oo = "rnbqk2r/pp2bppp/4p3/3pPn2/3p1P2/P2B1N2/1PP3PP/RNBQK2R w KQkq -"
        fen_after_white_oo = "rnbqk2r/pp2bppp/4p3/3pPn2/3p1P2/P2B1N2/1PP3PP/RNBQ1RK1 b kq -"
        fen_after_black_oo = "rnbq1rk1/pp2bppp/4p3/3pPn2/3p1P2/P2B1N2/1PP3PP/RNBQ1RK1 w - -"

        conn = get_db()
        c = conn.cursor()
        # White prepared move O-O
        c.execute("""
        INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san, is_user_move, study_name)
        VALUES ('white', ?, ?, 'e1g1', 'O-O', 1, 'Anti French Defense')
        """, (fen_before_white_oo, fen_after_white_oo))

        # Black opponent reply O-O -- a DEAD END: the user has nothing prepared after it.
        c.execute("""
        INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san, is_user_move, study_name)
        VALUES ('white', ?, ?, 'e8g8', 'O-O', 0, 'Anti French Defense')
        """, (fen_after_white_oo, fen_after_black_oo))
        conn.commit()
        conn.close()

        # Evaluate White's attempt
        res = self.manager.evaluate_trainer_move(
            repertoire="white",
            current_fen=fen_before_white_oo,
            attempted_uci="e1g1",
            study_filter="Anti French Defense"
        )

        self.assertTrue(res["correct"])
        self.assertEqual(res["your_san"], "O-O")

        # FIXER-V: the opponent move the user has no answer to is never played.
        self.assertIsNone(
            res["opponent_reply"],
            "an opponent branch with no prepared user reply must not be offered as a drill move",
        )
        self.assertIsNone(res["next_expected_uci"], "next_expected_uci must be None when no user response exists")
        self.assertIsNone(res["next_expected_san"], "next_expected_san must be None when no user response exists")

        # The line stops on the user's OWN move, and the reason says *why* it stopped.
        self.assertTrue(res["is_line_end"])
        self.assertEqual(
            res["line_end_reason"], "unanswered_opponent_branches",
            "a repertoire branch with no prepared reply is a gap, not a completed line",
        )
        self.assertEqual(res["unanswered_branches"], 1)
        self.assertEqual(res["unanswered_branch_sans"], ["O-O"])

    def test_line_continuation_when_user_has_followup(self):
        """
        Test that when user DOES have a follow-up, line does not end and next_expected_uci is populated.
        """
        fen_0 = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
        fen_1 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3"
        fen_2 = "rnbqkbnr/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"
        fen_3 = "rnbqkbnr/pppp1ppp/4p3/8/3PP3/8/PPP2PPP/RNBQKBNR b KQkq d3"

        conn = get_db()
        c = conn.cursor()
        c.execute("""
        INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san, is_user_move, study_name)
        VALUES ('white', ?, ?, 'e2e4', 'e4', 1, 'French Defense')
        """, (fen_0, fen_1))
        c.execute("""
        INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san, is_user_move, study_name)
        VALUES ('white', ?, ?, 'e7e6', 'e6', 0, 'French Defense')
        """, (fen_1, fen_2))
        c.execute("""
        INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san, is_user_move, study_name)
        VALUES ('white', ?, ?, 'd2d4', 'd4', 1, 'French Defense')
        """, (fen_2, fen_3))
        conn.commit()
        conn.close()

        res = self.manager.evaluate_trainer_move(
            repertoire="white",
            current_fen=fen_0,
            attempted_uci="e2e4",
            study_filter="French Defense"
        )

        self.assertTrue(res["correct"])
        self.assertIsNotNone(res["opponent_reply"])
        self.assertEqual(res["opponent_reply"]["san"], "e6")
        self.assertEqual(res["next_expected_uci"], "d2d4")
        self.assertEqual(res["next_expected_san"], "d4")
        self.assertFalse(res["is_line_end"], "Line should continue when user has prepared follow-up d4")

    def test_an_opponent_branch_with_no_reply_is_never_offered_as_a_drill_move(self):
        """
        AUDIT FIXER-V / TRAINER-09 — REWRITTEN. This test previously asserted (over 10
        iterations, with ``assertNotIn("c5", chosen_replies)``) that the trainer
        *prioritises* the reply that continues the drill, i.e. that a dead-end branch may be
        chosen as long as it is weighted lower. The project owner has since OVERRULED the
        documented pedagogy behind that behaviour with the rule:

            "A line should never finish on an opponent's move."

        Prioritising a dead end is not enough: the dead end must be *impossible*. The assertion is
        therefore strengthened rather than loosened — over 10 weighted draws the trainer must
        answer EVERY time, must NEVER return `None` for `next_expected_uci`, and must NEVER claim
        the line has ended.
        """
        fen_0 = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
        fen_1 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3"
        # Opponent branch 1: 1... e6 (has user follow-up 2. d4)
        fen_french = "rnbqkbnr/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"
        fen_d4 = "rnbqkbnr/pppp1ppp/4p3/8/3PP3/8/PPP2PPP/RNBQKBNR b KQkq d3"
        # Opponent branch 2: 1... c5 (dead end leaf, no user follow-up)
        fen_sicilian = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6"

        conn = get_db()
        c = conn.cursor()
        c.execute("""
        INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san, is_user_move, study_name, popularity_pct)
        VALUES ('white', ?, ?, 'e2e4', 'e4', 1, 'MultiTest', 100.0)
        """, (fen_0, fen_1))
        # `c5` is given popularity 100.0 and `e6` only 1.0, so the OLD weighting would have been
        # overwhelmingly likely to pick the dead end whenever the filter was removed. Nothing may
        # survive that.
        c.execute("""
        INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san, is_user_move, study_name, popularity_pct)
        VALUES ('white', ?, ?, 'e7e6', 'e6', 0, 'MultiTest', 1.0)
        """, (fen_1, fen_french))
        c.execute("""
        INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san, is_user_move, study_name)
        VALUES ('white', ?, ?, 'd2d4', 'd4', 1, 'MultiTest')
        """, (fen_french, fen_d4))
        c.execute("""
        INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san, is_user_move, study_name, popularity_pct)
        VALUES ('white', ?, ?, 'c7c5', 'c5', 0, 'MultiTest', 100.0)
        """, (fen_1, fen_sicilian))
        conn.commit()
        conn.close()

        chosen_replies = set()
        for _ in range(10):
            res = self.manager.evaluate_trainer_move(
                repertoire="white",
                current_fen=fen_0,
                attempted_uci="e2e4",
                study_filter="MultiTest"
            )
            chosen_replies.add(res["opponent_reply"]["san"])
            self.assertEqual(res["next_expected_uci"], "d2d4")
            self.assertEqual(res["next_expected_san"], "d4")
            self.assertFalse(res["is_line_end"])
            self.assertIsNone(res["line_end_reason"])

        self.assertEqual(chosen_replies, {"e6"})
        self.assertNotIn("c5", chosen_replies,
                         "a dead-end opponent branch is not a drill choice at all (owner ruling)")

    def test_a_position_reached_by_an_opponent_move_is_never_a_completed_line(self):
        """
        AUDIT FIXER-V: the unit test of the rule itself, at its narrowest.

        ``classify_line_position`` is the single definition of "is this line finished?":
        a completed line is a position reached at the end of the USER's own prepared move.
        A position reached by an OPPONENT move that the user has no prepared reply for is a
        gap, and must never be reported as an end - however convenient that would be.
        """
        from backend.repertoire import classify_line_position

        # Genuine completion: the user's own move produced the position, nothing more prepared.
        self.assertEqual(
            classify_line_position(reached_by_user_move=True, has_prepared_reply=False),
            "completed_line")
        # THE BUG: the opponent moved and the user has nothing prepared. A gap, NOT a line end.
        self.assertEqual(
            classify_line_position(reached_by_user_move=False, has_prepared_reply=False),
            "awaiting_user_reply")
        self.assertNotEqual(
            classify_line_position(reached_by_user_move=False, has_prepared_reply=False),
            classify_line_position(reached_by_user_move=True, has_prepared_reply=False),
            "an unanswered opponent branch must never be classified as a completed line")
        # The opponent moved and the user HAS a reply: answered, the line continues.
        self.assertEqual(
            classify_line_position(reached_by_user_move=False, has_prepared_reply=True),
            "continues")

if __name__ == '__main__':
    unittest.main()
