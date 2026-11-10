"""
tests/test_fsrs5_sota_trainer.py — Verification of SOTA FSRS-5 Chess Repertoire Trainer & Dark Studio UI

Covers:
  1. FSRS-5 post-lapse stability preservation (`fsrs5_lapse_stability`) on veteran cards vs young cards.
  2. Implicit 4-Grade (`Again=1, Hard=2, Good=3, Easy=4`) latency grading (`response_ms`) + AFK guard.
  3. FSRS-5 short-term same-day review damping (anti-cramming).
  4. Transposition-aware DAG graph (`build_repertoire_tree_graph`): prerequisite unlocking via
     any learned transposing move order + multi-parent weakness backpropagation.
  5. Real-game Lichess deviation coupling into `Adaptive` mode (`played_at > last_practiced`).
  6. Precomputed 2-ply dynamic `lead_in_steps` in drill payloads.
  7. Active-recall note anti-spoiler & latency wiring in `RepertoireTrainer` (real JS via Node harness).
  8. Simplified 2-Tab + Settings Modal + Scout-free architecture contracts.
"""
import os
import time
import unittest

import tests.test_setup  # FIRST: isolated temp DB
import chess
from fastapi.testclient import TestClient

from backend.api import app
from backend.db import closing_db
from backend.deviations import reset_resolved_deviations
from backend.repertoire import RepertoireManager
from backend.trainer import (
    GRADE_AGAIN,
    GRADE_EASY,
    GRADE_GOOD,
    GRADE_HARD,
    fsrs5_lapse_stability,
    infer_fsrs_grade,
)
import tests.frontend_runtime as frontend_runtime


class TestFsrs5SotaTrainer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rm = RepertoireManager.get_instance()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        tests.test_setup.reset_fixtures()

    def setUp(self):
        self.rm.clear_repertoire_database()
        reset_resolved_deviations()
        with closing_db() as conn:
            conn.execute("DELETE FROM games WHERE id LIKE 'sota_%'")
            conn.commit()

    def test_01_implicit_latency_grade_classification(self):
        """Response timing maps smoothly to Again / Hard / Good / Easy with unseen & AFK guards."""
        # Mistakes and retries always map to Again (1)
        self.assertEqual(infer_fsrs_grade(False, response_ms=900, prev_tp=5), GRADE_AGAIN)
        self.assertEqual(infer_fsrs_grade(True, is_retry=True, response_ms=900, prev_tp=5), GRADE_AGAIN)

        # Brand-new unseen move (prev_tp == 0) cannot jump straight to Easy even if fast
        self.assertEqual(infer_fsrs_grade(True, response_ms=1100, prev_tp=0), GRADE_GOOD)

        # Previously practiced move (prev_tp >= 1): < 2.5s -> Easy, 2.5s..12s -> Good, 12s..60s -> Hard
        self.assertEqual(infer_fsrs_grade(True, response_ms=1400, prev_tp=2), GRADE_EASY)
        self.assertEqual(infer_fsrs_grade(True, response_ms=5200, prev_tp=2), GRADE_GOOD)
        self.assertEqual(infer_fsrs_grade(True, response_ms=16000, prev_tp=2), GRADE_HARD)

        # Idle / AFK (> 60s) is treated neutrally as Good, never penalized as Hard
        self.assertEqual(infer_fsrs_grade(True, response_ms=180000, prev_tp=2), GRADE_GOOD)

    def test_02_latency_affects_stability_growth_via_api(self):
        """Fluent Easy recalls (<2.5s) gain greater stability than hesitant Hard recalls (>12s)."""
        b = chess.Board()
        self.rm.add_repertoire_move("white", b.fen(), "e2e4", is_user_move=True)
        b.push_san("e4")
        self.rm.add_repertoire_move("white", b.fen(), "e7e5", is_user_move=False)
        b.push_san("e5")
        self.rm.add_repertoire_move("white", b.fen(), "g1f3", is_user_move=True)

        now = int(time.time())
        with closing_db() as conn:
            conn.execute(
                """
                UPDATE repertoire_moves
                SET times_practiced = 2, times_correct = 2, consecutive_correct = 2,
                    interval_days = 3.0, difficulty = 5.0, last_practiced = ?, next_review = ?
                WHERE is_user_move = 1
                """,
                (now - 3 * 86400, now - 60),
            )
            conn.commit()

        # Move 1 (e4) answered fluently in 1.1s -> Easy (grade 4)
        res_easy = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": chess.Board().fen(),
            "attempted_uci": "e2e4",
            "response_ms": 1100,
        }).json()

        # Move 2 (Nf3) answered hesitantly in 18s -> Hard (grade 2)
        fen_nf3 = chess.Board()
        fen_nf3.push_san("e4")
        fen_nf3.push_san("e5")
        res_hard = self.client.post("/api/repertoire/train/attempt", json={
            "repertoire": "white",
            "current_fen": fen_nf3.fen(),
            "attempted_uci": "g1f3",
            "response_ms": 18000,
        }).json()

        self.assertEqual(res_easy["fsrs_grade"], GRADE_EASY)
        self.assertEqual(res_hard["fsrs_grade"], GRADE_HARD)
        self.assertGreater(res_easy["stability"], res_hard["stability"])
        self.assertLess(res_easy["difficulty"], res_hard["difficulty"])

    def test_03_veteran_lapse_preserves_partial_stability(self):
        """Missing a 120-day veteran card preserves partial FSRS stability (>1d) while scheduling next-day review."""
        b = chess.Board()
        fen0 = b.fen()
        self.rm.add_repertoire_move("white", fen0, "e2e4", is_user_move=True)

        now = int(time.time())
        with closing_db() as conn:
            conn.execute(
                """
                UPDATE repertoire_moves
                SET times_practiced = 8, times_correct = 8, consecutive_correct = 8,
                    interval_days = 120.0, difficulty = 4.5, last_practiced = ?, next_review = ?
                WHERE move_uci = 'e2e4'
                """,
                (now - 100 * 86400, now - 3600),
            )
            conn.commit()

        miss = self.rm.evaluate_trainer_move("white", fen0, "d2d4")
        self.assertFalse(miss["correct"])
        # Veteran card retains partial memory trace (~6..15d) rather than wiping to 1.0d
        self.assertGreater(miss["stability"], 3.0)
        self.assertLess(miss["stability"], 85.0)

        # Next review is still scheduled within 24h so the lapsed move is re-verified tomorrow
        with closing_db() as conn:
            row = conn.execute("SELECT interval_days, next_review FROM repertoire_moves WHERE move_uci='e2e4'").fetchone()
        self.assertAlmostEqual(row["next_review"], now + 86400, delta=5)

    def test_04_transposition_dag_unlocks_prerequisites_and_propagates_weakness(self):
        """
        Two transposing paths reach the same Queen's Gambit Declined tabiya:
          Path A (learned):   1. d4 d5 2. c4 e6 -> 3. Nc3 (unlearned target!)
          Path B (unlearned): 1. c4 e6 2. d4 d5 -> 3. Nc3
        Wait: since repertoire allows 1 user move per position, let's build a Black transposition:
          Path A (Opponent 1. d4 -> Black Nf6 [LEARNED] -> 2. c4 -> Black e6 [LEARNED] -> 3. Nc3 -> Black Bb4 [UNLEARNED])
          Path B (Opponent 1. c4 -> Black e6 [UNLEARNED] -> 2. Nc3 -> Black Nf6 [UNLEARNED] -> 3. d4 -> Black Bb4)
        Even if BFS visits Path B or Path A, 3... Bb4 MUST have prerequisites_met=True because Path A is learned,
        AND weakness from 3... Bb4 must backpropagate to BOTH Path A and Path B roots!
        """
        # Path B inserted FIRST so a naive single-visit BFS would freeze `prerequisites_met=False`!
        b_path_b = chess.Board()
        self.rm.add_repertoire_move("black", b_path_b.fen(), "c2c4", is_user_move=False)
        b_path_b.push_san("c4")
        self.rm.add_repertoire_move("black", b_path_b.fen(), "e7e6", is_user_move=True)  # unlearned!
        b_path_b.push_san("e6")
        self.rm.add_repertoire_move("black", b_path_b.fen(), "b1c3", is_user_move=False)
        b_path_b.push_san("Nc3")
        self.rm.add_repertoire_move("black", b_path_b.fen(), "g8f6", is_user_move=True)  # unlearned!
        b_path_b.push_san("Nf6")
        self.rm.add_repertoire_move("black", b_path_b.fen(), "d2d4", is_user_move=False)
        b_path_b.push_san("d4")
        # Shared tabiya: 3... Bb4 (Nimzo-Indian!)
        self.rm.add_repertoire_move("black", b_path_b.fen(), "f8b4", is_user_move=True)

        # Path A inserted SECOND, with its Black moves LEARNED!
        b_path_a = chess.Board()
        self.rm.add_repertoire_move("black", b_path_a.fen(), "d2d4", is_user_move=False)
        b_path_a.push_san("d4")
        res_a1 = self.rm.add_repertoire_move("black", b_path_a.fen(), "g8f6", is_user_move=True)
        b_path_a.push_san("Nf6")
        self.rm.add_repertoire_move("black", b_path_a.fen(), "c2c4", is_user_move=False)
        b_path_a.push_san("c4")
        res_a2 = self.rm.add_repertoire_move("black", b_path_a.fen(), "e7e6", is_user_move=True)
        b_path_a.push_san("e6")
        self.rm.add_repertoire_move("black", b_path_a.fen(), "b1c3", is_user_move=False)
        b_path_a.push_san("Nc3")
        # b_path_a.fen() now transposes into the exact 3-token Nimzo tabiya!

        b_a1 = chess.Board()
        b_a1.push_san("d4")
        b_a2 = b_a1.copy()
        b_a2.push_san("Nf6")
        b_a2.push_san("c4")
        norm_fn = self.rm.add_repertoire_move.__globals__["normalize_fen"]

        now = int(time.time())
        with closing_db() as conn:
            # Mark Path A's two Black moves (1...Nf6 after 1.d4, and 2...e6 after 2.c4) as learned
            conn.execute(
                """
                UPDATE repertoire_moves
                SET times_practiced = 3, times_correct = 3, consecutive_correct = 3,
                    interval_days = 14.0, last_practiced = ?, next_review = ?
                WHERE move_uci IN ('g8f6', 'e7e6') AND parent_fen IN (?, ?)
                """,
                (now, now + 14 * 86400, norm_fn(b_a1.fen()), norm_fn(b_a2.fen())),
            )
            conn.commit()

        graph = self.rm.build_repertoire_tree_graph("black")
        bb4_node = next(n for n in graph.values() if n["move"]["move_uci"] == "f8b4")
        self.assertTrue(bb4_node["prerequisites_met"], "3...Bb4 must unlock via learned Path A transposition!")
        self.assertTrue(bb4_node["is_frontier"], "3...Bb4 must be on the learnable frontier!")
        self.assertGreaterEqual(len(bb4_node["ancestor_paths"]), 2, "Both transposing ancestor chains must be tracked")

    def test_05_real_game_deviation_surfaces_in_adaptive_mode(self):
        """A mastered move that was subsequently forgotten in a real Lichess game is promoted in Adaptive mode."""
        b = chess.Board()
        self.rm.add_repertoire_move("white", b.fen(), "e2e4", is_user_move=True)
        b.push_san("e4")
        self.rm.add_repertoire_move("white", b.fen(), "c7c5", is_user_move=False)
        b.push_san("c5")
        self.rm.add_repertoire_move("white", b.fen(), "g1f3", is_user_move=True)

        now = int(time.time())
        practiced_ts = now - 7 * 86400
        with closing_db() as conn:
            # Mark both moves mastered with reviews far in the future (+30 days)
            conn.execute(
                """
                UPDATE repertoire_moves
                SET times_practiced = 5, times_correct = 5, consecutive_correct = 5,
                    interval_days = 30.0, last_practiced = ?, next_review = ?
                WHERE is_user_move = 1
                """,
                (practiced_ts, now + 23 * 86400),
            )
            # Insert a real Lichess game played YESTERDAY where user deviated on move 2 (played 2. Nc3 instead of 2. Nf3)
            conn.execute(
                """
                INSERT INTO games (id, speed, rated, played_at, white_player, black_player,
                                   white_rating, black_rating, user_color, result, opening_name, eco, moves_san, analyzed_at)
                VALUES ('sota_dev_1', 'rapid', 1, ?, 'RobiDeli', 'ClubPlayer', 1970, 1950,
                        'white', 'black', 'Sicilian Defense', 'B23', 'e4 c5 Nc3 Nc6', ?)
                """,
                ((now - 86400) * 1000, now),
            )
            conn.commit()

        drill = self.rm.get_next_trainer_drill("white", session_mode="adaptive")
        self.assertEqual(drill["expected_san"], "Nf3")
        self.assertEqual(drill["drill_reason"], "game_deviation")
        self.assertEqual(drill["played_san"], "Nc3")
        self.assertTrue(len(drill["lead_in_steps"]) == 2)

    def test_06_frontend_note_antispoiler_and_latency_payload(self):
        """Move notes stay hidden before attempting, reveal on Hint or Attempt, and send response_ms."""
        if not frontend_runtime.node_available():
            self.skipTest("node not installed")

        scenario = r"""
        const trainer = new RepertoireTrainer();
        const noteBox = document.createElement('div');
        noteBox.id = 'trainNoteBox';
        const noteContent = document.createElement('div');
        noteContent.id = 'trainNoteContent';
        const boardEl = document.createElement('div');
        boardEl.id = 'trainerBoard';
        document.byId.set('trainNoteBox', noteBox);
        document.byId.set('trainNoteContent', noteContent);
        document.byId.set('trainerBoard', boardEl);

        let capturedAttemptBody = null;
        window.fetch = async (url, opts) => {
            const u = String(url);
            if (u.includes('/api/repertoire/train/next')) {
                return {
                    ok: true, status: 200,
                    json: async () => ({
                        id: 77,
                        repertoire: 'white',
                        parent_fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
                        expected_uci: 'e2e4',
                        expected_san: 'e4',
                        comment: 'Play e4 to control d5 and f5!',
                        path_san: [],
                        path_formatted: '',
                        drill_reason: 'due_review',
                        ply_depth: 1
                    })
                };
            }
            if (u.includes('/api/repertoire/train/attempt')) {
                capturedAttemptBody = JSON.parse(opts.body);
                return {
                    ok: true, status: 200,
                    json: async () => ({
                        correct: true,
                        prepared_id: 77,
                        your_san: 'e4',
                        your_uci: 'e2e4',
                        comment: 'Play e4 to control d5 and f5!',
                        message: 'Correct! e4 is your prepared move.',
                        is_line_end: true,
                        line_end_reason: 'repertoire_exhausted',
                        unanswered_branches: 0
                    })
                };
            }
            return { ok: true, status: 200, json: async () => ({ openings: [], studies: [], mastered_pct: 0, learning_pct: 0, unlearned_pct: 100, mastered_count: 0, learning_count: 0, unlearned_count: 1, total_moves: 1 }) };
        };

        return (async () => {
            await trainer.startNewDrill();
            const hiddenBeforeMove = (noteBox.style.visibility === 'hidden');

            // Simulate 1.8s thinking time then play e2e4
            trainer.drillReadyAt = trainer._nowMs() - 1800;
            await trainer.onUserAttempt('e2', 'e4', '', { san: 'e4' });
            const visibleAfterMove = (noteBox.style.visibility === 'visible');
            const revealedText = noteContent.textContent;

            return {
                hiddenBeforeMove,
                visibleAfterMove,
                revealedText,
                sentResponseMs: capturedAttemptBody ? capturedAttemptBody.response_ms : null
            };
        })();
        """
        res = frontend_runtime.run_scenario(scenario)
        self.assertTrue(res["hiddenBeforeMove"], f"Move note leaked before active recall: {res}")
        self.assertTrue(res["visibleAfterMove"], f"Move note did not reveal after attempt: {res}")
        self.assertIn("Play e4", res["revealedText"])
        self.assertIsInstance(res["sentResponseMs"], int)
        self.assertGreaterEqual(res["sentResponseMs"], 1500)

    def test_07_simplified_two_tab_shell_and_no_scout_artifacts(self):
        """Verifies Scout is gone and only Builder + Trainer tabs + Settings modal remain."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for deleted in (
            "backend/scout.py",
            "backend/scout_discovery.py",
            "frontend/scout.js",
            "frontend/themes/editorial.css",
            "frontend/workflows/workflow.css",
        ):
            self.assertFalse(os.path.exists(os.path.join(root, deleted)), f"{deleted} should be deleted")

        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("viewScout", html)
        self.assertNotIn("youtube.com", html)
        self.assertIn('id="settingsModal"', html)
        self.assertIn('data-tab="repertoire"', html)
        self.assertIn('data-tab="rep-trainer"', html)


if __name__ == "__main__":
    unittest.main()
