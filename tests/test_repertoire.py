import unittest
import json
import sqlite3
import time
import chess
import tests.test_setup
from tests import fixtures
from backend.db import get_db, init_db
from backend.evaluator import STOCKFISH_PATH
from backend.repertoire import RepertoireManager, normalize_fen


def _parse_sse_frame(raw):
    """Parse one `data: {...}` SSE block from `stream_eval` into a dict."""
    block = raw.strip()
    self_lines = [ln for ln in block.splitlines() if ln.startswith("data: ")]
    assert self_lines, "SSE block must start with a data: line, got %r" % (block,)
    return json.loads(self_lines[0][len("data: "):])


#: Fool's mate — 1. f3 e5 2. g4 Qh4#. Black is mated, so the stream must answer
#: from the documented terminal frame (depth 99, mate set) with no engine.
FEN_FOOLS_MATE = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"


class TestRepertoire(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Restore the documented synthetic baseline so this module never inherits
        # rows another module wrote into the shared temp DB.
        tests.test_setup.reset_fixtures()
        init_db()
        cls.rm = RepertoireManager.get_instance()

    def setUp(self):
        """Every test in this module starts from an empty repertoire tree.

        Without this the module was order-dependent: `test_trainer_evaluation`
        silently relied on `test_repertoire_tree_and_add` having inserted 1. e4
        first, so a reordering or an added test changed the verdict. Each test
        now creates the moves it needs.
        """
        conn = get_db()
        conn.execute("DELETE FROM study_moves")
        conn.execute("DELETE FROM repertoire_studies")
        conn.execute("DELETE FROM repertoire_moves")
        conn.commit()
        conn.close()

    def test_candidate_moves_2000(self):
        # Query the Italian Game position (served from the synthetic explorer cache)
        data = self.rm.get_candidate_moves_2000(fixtures.FEN_ITALIAN_Bc5)
        self.assertIn("moves", data)
        self.assertTrue(len(data["moves"]) > 0)
        # Check popular move Bc5 or Nf6
        sans = [m["san"] for m in data["moves"]]
        self.assertIn("Bc5", sans)
        self.assertIn("Nf6", sans)
        # The response is sorted by (threat_score, popularity_pct, games) DESC.
        # This position has a personal stat: the synthetic White games play
        # 3...Bc5 here, so Bc5 carries a non-zero threat score and leads.
        keys = [(m["my_stats"]["threat_score"], m["popularity_pct"], m["games"])
                for m in data["moves"]]
        self.assertEqual(keys, sorted(keys, reverse=True))
        self.assertEqual(data["moves"][0]["san"], "Bc5")

    def test_repertoire_tree_and_add(self):
        # Add a move to white repertoire: 1. e4
        res = self.rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
        self.assertEqual(res["status"], "success")

        tree = self.rm.get_repertoire_tree("white", gap_threshold=5.0)
        self.assertIn("total_moves", tree)
        self.assertEqual(tree["total_moves"], 1)

        # Re-adding the same move is idempotent, not a duplicate row.
        again = self.rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
        self.assertEqual(again["status"], "success")
        self.assertEqual(self.rm.get_repertoire_tree("white")["total_moves"], 1)

    def test_trainer_evaluation(self):
        # In starting position, White's prepared move is e2e4
        self.rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
        res_correct = self.rm.evaluate_trainer_move("white", chess.STARTING_FEN, "e2e4")
        self.assertTrue(res_correct["correct"])

        res_wrong = self.rm.evaluate_trainer_move("white", chess.STARTING_FEN, "d2d4")
        self.assertFalse(res_wrong["correct"])

    def test_game_gaps_ordered_by_biggest_blunder(self):
        """Every invariant is asserted unconditionally.

        CODE-05-05/06 flagged `if gaps:` wrapping the whole body: on an empty
        database the test asserted nothing. The synthetic fixture
        (`tests/fixtures.py`) guarantees real gaps in both colors, so the
        `if` is gone — a regression that empties the gap list is now a failure.
        """
        for color in ("white", "black"):
            expected_keys = (
                fixtures.VISIBLE_WHITE_BLUNDERS if color == "white"
                else fixtures.VISIBLE_BLACK_BLUNDERS
            )
            gaps_data = self.rm.get_game_gaps(color, max_move=15)
            self.assertIn("gaps", gaps_data)
            gaps = gaps_data["gaps"]
            self.assertGreaterEqual(
                len(gaps), len(expected_keys),
                "the synthetic fixture must produce gaps for %s" % color,
            )

            # Max move must be <= 15 and position must not be already completely losing
            for g in gaps:
                self.assertLessEqual(g["move_number"], 15)
                self.assertGreaterEqual(g["eval_before"], -200)
                self.assertIn("game_moves", g)
                self.assertIn("all_blunders", g)
                self.assertEqual(g["color"], color)

            # First element must have largest cpl_loss (biggest blunder)
            cpl_losses = [g["cpl_loss"] for g in gaps]
            self.assertEqual(gaps[0]["cpl_loss"], max(cpl_losses))
            self.assertEqual(cpl_losses, sorted(cpl_losses, reverse=True),
                             "gaps must be ordered by cpl_loss DESC")

            # The negative fixtures must never surface.
            returned = {g["played_san"] for g in gaps}
            for key in fixtures.FILTERED_BLUNDERS:
                row = fixtures.blunder_by_key(key)
                self.assertNotIn(
                    row["played_san"] + "@" + row["game_id"], returned,
                    "%s must be filtered out of the gap list" % key,
                )

    def test_opening_games_threshold(self):
        """The 2000+ opening cutoff must actually cut something off.

        `min_games=0` includes every non-lost blunder, `min_games=30` must drop
        the fixture's rare line (opening_games=11).
        """
        gaps_30 = self.rm.get_game_gaps("white", min_games=30)
        self.assertIn("gaps", gaps_30)
        for g in gaps_30["gaps"]:
            self.assertGreaterEqual(g["opening_games"], 30)
            self.assertGreaterEqual(g["eval_before"], -200)

        # min_games=0 includes all non-lost blunders
        gaps_all = self.rm.get_game_gaps("white", min_games=0)
        self.assertGreaterEqual(len(gaps_all["gaps"]), len(gaps_30["gaps"]))

        # The rare synthetic line (opening_games = 11) exists but is cut at 30.
        rare = fixtures.blunder_by_key("synB07")
        self.assertLess(rare["opening_games"], 30)
        rare_blacks = {g["played_san"] for g in self.rm.get_game_gaps("black", min_games=0)["gaps"]}
        self.assertIn(rare["played_san"], rare_blacks,
                      "the rare line must be visible at min_games=0")
        self.assertNotIn(rare["played_san"], {g["played_san"] for g in
                                              self.rm.get_game_gaps("black", min_games=30)["gaps"]},
                         "the rare line must be cut at min_games=30")

    def test_async_engine_evaluator(self):
        from backend.evaluator import AsyncEngineEvaluator
        evaluator = AsyncEngineEvaluator.get_instance()
        self.assertIsNotNone(evaluator)
    def test_patched_blunder_excluded_from_active_gaps(self):
        """Patching a gap must move it out of the active list and re-rank the top.

        CODE-05-05/06 flagged the whole core of this test being wrapped in
        `if unpatched["patched_count"] > 0:` — true only for a user who has
        already patched a move, i.e. never in CI. The test now patches a gap
        itself, so the branch is guaranteed to run.
        """
        # The top white gap from the synthetic fixture: synB01 (cpl_loss 230).
        before = self.rm.get_game_gaps("white", min_games=30, include_patched=False)
        self.assertGreater(len(before["gaps"]), 1,
                           "need at least two gaps to observe the #1 -> #2 promotion")
        top = before["gaps"][0]
        self.assertFalse(top["is_patched"])
        runner_up_cpl = before["gaps"][1]["cpl_loss"]

        # Adopt the engine's fix as a prepared White move: that is what "patching"
        # means, and it is what makes `is_patched` true.
        from backend.repertoire import normalize_fen
        parent = normalize_fen(top["fen_before"])
        res = self.rm.add_repertoire_move("white", parent, top["best_uci"], is_user_move=True)
        self.assertEqual(res["status"], "success", res.get("message", ""))

        after = self.rm.get_game_gaps("white", min_games=30, include_patched=False)
        all_gaps = self.rm.get_game_gaps("white", min_games=30, include_patched=True)

        self.assertIn("patched_count", after)
        self.assertIn("unpatched_count", after)
        self.assertIn("patched_gaps", after)
        self.assertEqual(after["patched_count"], 1, "the patched gap must be counted as patched")

        # All items in after["gaps"] must not be patched
        for g in after["gaps"]:
            self.assertFalse(g["is_patched"])
        self.assertNotIn(top["id"], [g["id"] for g in after["gaps"]],
                         "a patched gap must not appear in the active list")

        # The previously-#2 gap is now the biggest active blunder.
        self.assertEqual(after["gaps"][0]["cpl_loss"], runner_up_cpl)
        self.assertEqual(
            after["gaps"][0]["cpl_loss"],
            max(g["cpl_loss"] for g in after["gaps"]),
        )

        # include_patched=True brings it back, and the list is a strict superset.
        self.assertGreater(len(all_gaps["gaps"]), len(after["gaps"]))
        self.assertIn(top["id"], [g["id"] for g in all_gaps["gaps"]])
        self.assertTrue(any(g["is_patched"] for g in all_gaps["gaps"]))

    def test_user_position_stats_and_threat_scoring(self):
        # 1. Verify user stats dictionary structure.
        #    CODE-05-05 flagged `if stats:` — with an empty games table the whole
        #    block was skipped. The synthetic fixture guarantees entries, so the
        #    guard is gone.
        stats = self.rm.get_user_position_stats("white")
        self.assertIsInstance(stats, dict)
        self.assertTrue(stats, "the synthetic fixture must produce user position stats")
        sample = next(iter(stats.values()))
        for key in ("games", "wins", "draws", "losses", "win_rate", "threat_score"):
            self.assertIn(key, sample)
        for (parent_fen, uci), entry in stats.items():
            self.assertIsInstance(parent_fen, str)
            self.assertIsInstance(uci, str)
            self.assertGreaterEqual(entry["games"], 1)
            self.assertEqual(
                entry["games"], entry["wins"] + entry["draws"] + entry["losses"],
                "games must equal wins + draws + losses for %s/%s" % (parent_fen, uci),
            )
            if entry["win_rate"] is not None:
                self.assertGreaterEqual(entry["win_rate"], 0.0)
                self.assertLessEqual(entry["win_rate"], 100.0,
                                    "win_rate is a percentage (0-100), not a fraction")

        # 2. Mathematical validation of the user's constraints:
        # Move A: 30 games, 50% winrate
        score_A = 30 * ((1.0 - 0.50)**2 + 0.02)  # 30 * 0.27 = 8.1
        # Move B: 2 games, 0% winrate
        score_B = 2 * ((1.0 - 0.0)**2 + 0.02)    # 2 * 1.02 = 2.04
        # Constraint 1: Move A > Move B (frequent 50% ranks higher than 2-game fluke)
        self.assertGreater(score_A, score_B)

        # Move C: 20 games, 50% winrate
        score_C = 20 * ((1.0 - 0.50)**2 + 0.02)  # 20 * 0.27 = 5.4
        # Move D: 10 games, 20% winrate
        score_D = 10 * ((1.0 - 0.20)**2 + 0.02)  # 10 * 0.66 = 6.6
        # Constraint 2: Move D > Move C (critical leak ranks higher than 20-game 50% hold)
        self.assertGreater(score_D, score_C)

        # 3. Verify get_candidate_moves_2000 includes my_stats and sorts by threat_score DESC
        # After 1. e4 (served from the synthetic explorer cache: no network, no engine).
        fen_after_e4 = fixtures.FEN_AFTER_E4
        data = self.rm.get_candidate_moves_2000(fen_after_e4, color="white")
        self.assertIn("moves", data)
        self.assertGreater(len(data["moves"]), 0)
        for m in data["moves"]:
            self.assertIn("my_stats", m)
            self.assertIn("threat_score", m["my_stats"])
            self.assertIn("games", m["my_stats"])

        # Check sorted by threat_score descending
        threats = [m["my_stats"]["threat_score"] for m in data["moves"]]
        self.assertEqual(threats, sorted(threats, reverse=True))

        # Every move the user actually played in this position must be surfaced
        # with a non-zero game count (the "sidelines" merge).
        self.assertTrue(
            any(m["my_stats"]["games"] > 0 for m in data["moves"]),
            "the synthetic fixture plays 1. e4 as White; it must appear in the "
            "explorer response with its personal stats",
        )

    def test_all_games_scan_and_count(self):
        """The game counters must reflect the database the suite is pointed at.

        CODE-05-07: this used to be `assertGreaterEqual(cnt, 280)` — the number of
        rated games user `RobiDeli` happened to have played at the time. The
        assertion is now anchored to the synthetic fixture, so it says something
        about the code instead of about the user's Lichess history.
        """
        total_cnt = self.rm.get_total_games_count()
        self.assertGreaterEqual(
            total_cnt, fixtures.SYNTHETIC_GAME_COUNT,
            "the synthetic fixture must be present in the DB under test",
        )

        # The synthetic fixture is the ONLY source of games: no production row
        # leaked into the temp database.
        conn = get_db()
        rows = conn.execute("SELECT id FROM games ORDER BY id").fetchall()
        conn.close()
        synthetic_ids = {g["id"] for g in fixtures.synthetic_games()}
        stray = {r["id"] for r in rows} - synthetic_ids
        self.assertEqual(
            stray, set(),
            "unexpected non-fixture games in the test DB: %s "
            "(the production database must never be copied into the test DB)" % (sorted(stray),),
        )

        # Check explore endpoint returns user_games_total
        fen_after_e4 = fixtures.FEN_AFTER_E4
        data = self.rm.get_candidate_moves_2000(fen_after_e4, color="white")
        self.assertIn("user_games_total", data)
        self.assertEqual(data["user_games_total"], total_cnt)

        # Check game gaps reflects total_games_analyzed
        gaps = self.rm.get_game_gaps("white", min_games=30)
        self.assertEqual(gaps["total_games_analyzed"], total_cnt)
        self.assertEqual(gaps["total_games_color"], fixtures.SYNTHETIC_WHITE_GAME_COUNT)

    def test_normalize_fen_four_tokens(self):
        from backend.repertoire import normalize_fen
        fen_with_ep = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
        norm = normalize_fen(fen_with_ep)
        self.assertEqual(norm, "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3")

    def test_add_repertoire_move_fen_validation(self):
        for bad_fen in ["garbage_fen", "8/8/8/8/8/8/8/8", "not_a_fen"]:
            res = self.rm.add_repertoire_move("white", bad_fen, "e2e4")
            self.assertEqual(res["status"], "error")
            self.assertIn("Invalid FEN string", res["message"])

    def test_add_repertoire_move_legality_and_promotions(self):
        # 1. Illegal move
        res_illegal = self.rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e5")
        self.assertEqual(res_illegal["status"], "error")
        self.assertIn("Illegal move", res_illegal["message"])

        # 2. Promotion without piece
        promo_fen = "8/4P3/8/8/8/8/8/k6K w - - 0 1"
        res_no_promo = self.rm.add_repertoire_move("white", promo_fen, "e7e8")
        self.assertEqual(res_no_promo["status"], "error")
        self.assertIn("Illegal move", res_no_promo["message"])

        # 3. Valid promotion
        res_promo = self.rm.add_repertoire_move("white", promo_fen, "e7e8q")
        self.assertEqual(res_promo["status"], "success")
        self.assertEqual(res_promo["san"], "e8=Q")

    def test_add_repertoire_move_color_validation(self):
        res_bad_color = self.rm.add_repertoire_move("purple", chess.STARTING_FEN, "e2e4")
        self.assertEqual(res_bad_color["status"], "error")
        self.assertIn("Invalid repertoire color", res_bad_color["message"])

    def test_trainer_null_popularity_pct(self):
        """A NULL `popularity_pct` on an opponent branch must not break the trainer.

        AUDIT FIXER-V (SETUP ONLY - no assertion was removed or loosened): the original fixture
        gave the opponent a branch (`1... f1c4`) that the user had **no prepared answer to**, and
        then asserted the trainer returned it as `opponent_reply`. That is the behaviour the
        project owner has overruled ("a line should never finish on an opponent's move"), so the
        fixture now states its real precondition: the branch has a prepared follow-up, which is
        what the popularity weighting is actually reached for. The NULL `popularity_pct` under
        test is still on the *opponent* row (the row whose weight is read), and the assertions
        below are strictly more numerous than the two they replace.
        """
        unique_fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
        # User move
        self.rm.add_repertoire_move("black", unique_fen, "b8c6", is_user_move=True)
        # Opponent move with NULL popularity_pct
        self.rm.add_repertoire_move(
            "black",
            "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
            "f1c4",
            is_user_move=False,
            popularity_pct=None
        )
        # FIXER-V: the answer to that branch, so the branch is a real drill choice.
        self.rm.add_repertoire_move(
            "black",
            "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
            "d7d5",
            is_user_move=True,
        )
        res = self.rm.evaluate_trainer_move("black", unique_fen, "b8c6")
        self.assertTrue(res["correct"])
        self.assertIsNotNone(res["opponent_reply"])
        self.assertEqual(res["opponent_reply"]["uci"], "f1c4")
        # The NULL popularity was tolerated AND the line continued.
        self.assertEqual(res["next_expected_uci"], "d7d5")
        self.assertEqual(res["line_position"], "continues")
        self.assertFalse(res["is_line_end"])
        self.assertIsNone(res["line_end_reason"])

    def test_trainer_records_failed_attempt(self):
        unique_fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
        self.rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
        # Play wrong move d2d4 instead of e2e4
        res = self.rm.evaluate_trainer_move("white", chess.STARTING_FEN, "d2d4")
        self.assertFalse(res["correct"])
        self.assertIn("Not in your repertoire", res["message"])

    def test_trainer_rejects_illegal_move(self):
        # Play illegal move e2e5 on starting board
        res = self.rm.evaluate_trainer_move("white", chess.STARTING_FEN, "e2e5")
        self.assertFalse(res["correct"])
        self.assertIn("Illegal move", res["message"])

    def test_evaluator_illegal_fen_safety(self):
        """An illegal FEN must produce a typed error frame, not kill the engine.

        CODE-05-06: this test used to define an `async def run_eval()` and never
        call it — zero assertions, zero awaits, green even with
        `AsyncEngineEvaluator` deleted. It now actually awaits the coroutine and
        asserts the real contract, then proves the shared engine still works.
        """
        import asyncio
        import json as _json
        from backend.evaluator import AsyncEngineEvaluator, AsyncEnginePool

        async def _async_test():
            pool = AsyncEnginePool()
            evaluator = AsyncEngineEvaluator(pool=pool)

            async def collect(fen, max_depth=10):
                frames = []
                async for raw in evaluator.stream_eval(fen, max_depth=max_depth):
                    frames.append(raw)
                return frames

            def pool_state():
                return (pool.spawned, pool.in_use, pool.live, pool._closed)

            try:
                # 1. A FEN python-chess accepts syntactically but that is not a valid
                #    board: an illegal pawn on the 8th rank.
                before = pool_state()
                frames = await collect(fixtures.FEN_ILLEGAL_PAWN_ON_BACKRANK)

                self.assertGreaterEqual(len(frames), 1, "an invalid FEN must still produce a frame")
                payloads = [_parse_sse_frame(f) for f in frames]
                self.assertEqual(len(payloads), len(frames))

                last = payloads[-1]
                self.assertEqual(last["type"], "error")
                self.assertEqual(last["code"], "invalid_fen")
                self.assertTrue(last["final"], "an error frame must be final so a client stops reading")
                from backend.evaluator import SSE_CONTRACT_VERSION
                self.assertEqual(last["v"], SSE_CONTRACT_VERSION,
                                 "every frame carries the SSE contract version")
                self.assertEqual(last["depth"], 0)
                self.assertIsNone(last["score_cp"])
                self.assertNotIn(STOCKFISH_PATH, last["error"],
                                 "error messages must be sanitized: no filesystem paths")

                # 2. Refusal has NO side effect on engine pool
                self.assertEqual(
                    pool_state(), before,
                    "an invalid FEN must be rejected before the engine pool is touched",
                )

                # 3. Total garbage
                for bad in ("not a fen at all", "", "8/8/8/8/8/8/8/8 w - - 0 1"):
                    before_bad = pool_state()
                    bad_frames = [_parse_sse_frame(f) for f in await collect(bad)]
                    self.assertEqual(len(bad_frames), 1, "%r must produce exactly one terminal frame" % bad)
                    self.assertEqual(bad_frames[0]["code"], "invalid_fen", bad)
                    self.assertTrue(bad_frames[0]["final"], bad)
                    self.assertEqual(pool_state(), before_bad, "%r must not touch the engine pool" % bad)

                # 4. Decided position
                before_mate = pool_state()
                mate_frames = [_parse_sse_frame(f) for f in await collect(FEN_FOOLS_MATE)]
                self.assertEqual(len(mate_frames), 1)
                mate = mate_frames[0]
                self.assertEqual(mate["type"], "eval")
                self.assertEqual(mate["depth"], 99, "a decided position is reported at depth 99")
                self.assertTrue(mate["final"])
                self.assertTrue(mate["is_mate"])
                self.assertEqual(mate["mate"], -1, "Black delivered mate, so White is mated")
                self.assertEqual(mate["score_cp"], -10000)
                self.assertEqual(mate["eval"], mate["score_cp"])
                self.assertEqual(pool_state(), before_mate)

                # 5. Real search afterwards
                last = None
                for _ in range(3):
                    events = await collect(chess.STARTING_FEN, max_depth=1)
                    self.assertTrue(len(events) > 0)
                    last = _parse_sse_frame(events[-1])
                    if last["type"] == "eval":
                        break
                    self.assertIn(last.get("code"), ("engine_busy", "engine_unavailable"))
                    await asyncio.sleep(0.01)
                if last["type"] == "eval":
                    self.assertGreater(last["depth"], 0)
                    self.assertIsNotNone(last["score_cp"])
                self.assertEqual(pool.in_use, 0)
            finally:
                await pool.close()

        asyncio.run(_async_test())

        # 6. The process-wide singleton is untouched by a refusal.
        self.assertIs(AsyncEngineEvaluator.get_instance(),
                      AsyncEngineEvaluator.get_instance(),
                      "the shared singleton must survive a refused FEN")

    def test_clear_repertoire_database(self):
        # Ensure we have moves in both repertoires
        self.rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
        self.rm.add_repertoire_move("black", chess.STARTING_FEN, "d2d4", is_user_move=False)
        tree_before = self.rm.get_repertoire_tree("white")
        self.assertGreater(tree_before["total_moves"], 0)

        # 1. Clear white repertoire specifically
        deleted_white = self.rm.clear_repertoire_database("white")
        self.assertGreaterEqual(deleted_white, 1)
        tree_white = self.rm.get_repertoire_tree("white")
        self.assertEqual(tree_white["total_moves"], 0)

        # 2. Add moves again
        self.rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
        self.rm.add_repertoire_move("black", chess.STARTING_FEN, "d2d4", is_user_move=False)

        # 3. Clear all repertoires
        deleted_all = self.rm.clear_repertoire_database()
        self.assertGreaterEqual(deleted_all, 2)
        self.assertEqual(self.rm.get_repertoire_tree("white")["total_moves"], 0)
        self.assertEqual(self.rm.get_repertoire_tree("black")["total_moves"], 0)

    def test_update_move_metadata_and_trainer_priority(self):
        # 1. Add move to white repertoire
        add_res = self.rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
        self.assertEqual(add_res["status"], "success")

        # 2. Update metadata: comment, arrows, importance
        meta_res = self.rm.update_move_metadata(
            repertoire="white",
            parent_fen=chess.STARTING_FEN,
            move_uci="e2e4",
            is_important=True,
            comment="Control the center and open diagonals for bishop and queen.",
            arrows=json.dumps([{"from": "e2", "to": "e4", "color": "#22c55e"}])
        )
        self.assertEqual(meta_res["status"], "success")
        self.assertEqual(meta_res["is_important"], 1)
        self.assertIn("Control the center", meta_res["comment"])
        self.assertIn("e2", meta_res["arrows"])

        # 3. Verify trainer returns this drill and prioritizes important moves
        drill = self.rm.get_next_trainer_drill("white")
        self.assertIsNotNone(drill)
        self.assertTrue(drill.get("is_important"))
        self.assertEqual(drill.get("comment"), "Control the center and open diagonals for bishop and queen.")
        self.assertIn("e2", drill.get("arrows", ""))

        # 4. Verify evaluate_trainer_move returns the metadata
        eval_res = self.rm.evaluate_trainer_move("white", chess.STARTING_FEN, "e2e4")
        self.assertTrue(eval_res["correct"])
        self.assertTrue(eval_res["is_important"])
        self.assertEqual(eval_res["comment"], "Control the center and open diagonals for bishop and queen.")
        self.assertIn("e2", eval_res["arrows"])

    def test_delete_repertoire_branch(self):
        # Add a 2-ply sequence: 1. e4 e5
        self.rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
        board = chess.Board()
        board.push_san("e4")
        fen_after_e4 = board.fen()
        self.rm.add_repertoire_move("white", fen_after_e4, "e7e5", is_user_move=False)

        # Delete opponent reply e7e5
        self.rm.delete_repertoire_branch("white", fen_after_e4, "e7e5")

        # Verify e7e5 is deleted but e2e4 remains
        tree = self.rm.get_repertoire_tree("white")
        parent_e4 = " ".join(fen_after_e4.split()[:3])
        children = tree["moves_by_parent"].get(parent_e4, [])
        self.assertFalse(any(m["move_uci"] == "e7e5" for m in children))

    def test_candidate_moves_white_eval_convention(self):
        """Evaluations must always be **White's point of view**, for both colors.

        CODE-05-05/06 flagged both assertions here being wrapped in
        `if top_move.get("eval") is not None:` — with a network-supplied eval a
        `null` made the convention regression undetectable.

        The fixture seeds this position in `explorer_cache` with
        `eval_convention: "stm"` and side-to-move (Black) scores. This test writes
        its own side-to-move row and then checks the read path
        (`get_candidate_moves_2000`) performs the conversion for **both** `color=`
        values. Fully deterministic: no network, no engine, no `None` escapes.

        Note `init_db()`'s `_migrate_explorer_cache_eval_convention` is a
        **one-shot** migration gated on `PRAGMA user_version` (audit CODE-02-18),
        so it does not re-run on every launch. The conversion under test is
        therefore the *read path*, not the migration.
        """
        fen = fixtures.FEN_WHITE_CENTRE_ADVANTAGE
        key = fixtures.norm4(fen)

        # Locate the synthetic payload for this position.
        payload = None
        for row_fen, ratings, candidate in fixtures.synthetic_explorer_rows():
            if row_fen == key:
                payload = candidate
                break
        self.assertIsNotNone(payload, "the synthetic fixture must cover this position")
        seeded = {m["san"]: m["eval"] for m in payload["moves"]}
        self.assertTrue(seeded)
        for san, value in seeded.items():
            self.assertIsNotNone(value, "every seeded move must carry a score")
            self.assertLess(value, 0, "the seed is in Black (side-to-move) POV: negative")

        # (a) The one-shot boot migration must still exist. It is gated on
        #     `PRAGMA user_version` (audit CODE-02-18), so it does NOT re-run on
        #     every launch, and it correctly does not touch the row the fixture
        #     seeds *after* `init_db()`. This assertion exists so the migration
        #     cannot be silently deleted; the conversion itself is asserted in (b).
        from backend import db as db_mod
        self.assertTrue(
            hasattr(db_mod, "_migrate_explorer_cache_eval_convention"),
            "backend/db.py must keep a migration that normalises cached "
            "explorer payloads to the white-perspective eval convention",
        )
        conn = get_db()
        seeded_row = conn.execute(
            "SELECT data_json FROM explorer_cache WHERE fen = ? AND ratings = ?",
            (key, "2000,2200,2500"),
        ).fetchone()
        self.assertIsNotNone(
            seeded_row, "the synthetic explorer cache must cover this position")
        self.assertIn(
            json.loads(seeded_row["data_json"]).get("eval_convention"),
            ("stm", "white"),
            "the cached payload must declare its eval convention explicitly",
        )
        conn.close()

        # (b) The read path must convert a freshly written side-to-move row.
        fresh = json.loads(json.dumps(payload))
        fresh["eval_convention"] = "stm"
        for move in fresh["moves"]:
            move["eval"] = seeded[move["san"]]
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO explorer_cache (fen, ratings, data_json, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (key, "2000,2200,2500", json.dumps(fresh), int(time.time())),
        )
        conn.commit()
        conn.close()

        for color in ("white", "black"):
            res = self.rm.get_candidate_moves_2000(fen, color=color)
            self.assertIn("moves", res)
            self.assertGreater(len(res["moves"]), 0,
                               "a cache hit must return the seeded moves for color=%s" % color)
            by_san = {m["san"]: m for m in res["moves"] if m["san"] in seeded}
            self.assertEqual(
                set(by_san), set(seeded),
                "every seeded move must be returned for color=%s" % color,
            )
            for san, value in seeded.items():
                self.assertIsNotNone(
                    by_san[san].get("eval"),
                    "eval must be populated (not null) for %s/%s" % (san, color),
                )
                self.assertEqual(
                    by_san[san]["eval"], -value,
                    "eval for %s must be negated to White's POV regardless of the "
                    "user's color (got %r, seeded STM %r)"
                    % (san, by_san[san]["eval"], value),
                )
                self.assertGreater(
                    by_san[san]["eval"], 0,
                    "in this position the side to move is behind, so the "
                    "white-POV score must be positive",
                )

# ===================================================================== 8. CODE-3 determinism
#: `1. e4 c6 2. e5 d5`, White is the user. The last move is the OPPONENT's, which is what the
#: trainer needs: `evaluate_trainer_move` grades the user's move, then plays an opponent reply,
#: then resolves the user's *follow-up* at the position that reply produced. A **double pawn
#: push** is the only way that position can carry an en-passant token, because python-chess
#: prints `-` for a square no pawn can legally take — and here White has a pawn on e5, so `d6`
#: survives `board.fen()` and therefore `normalize_fen()`.
#:
#:   1.e4  (user)   1...c6 (opponent) -> _AFTER_C6 : the trainer's `current_fen`
#:   2.e5  (user)                    -> _AFTER_E5 : `after_fen`
#:   2...d5 (opponent)               -> _R_EP     : `opp["fen"]`, the follow-up's parent
_AFTER_C6 = "rnbqkbnr/pp1ppppp/2p5/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
_AFTER_E5 = "rnbqkbnr/pp1ppppp/2p5/4P3/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2"
_R_EP = "rnbqkbnr/pp2pppp/2p5/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq d6"
#: The same logical position with the en-passant token spelled `-` — what a PGN round-trip, or
#: a `normalize_fen` one ply later, produces. It matches the lookup's fuzzy
#: `LIKE '<3-token> %'` arm and NOT its exact `parent_fen = ?` arm, and because `-` (0x2D) sorts
#: before every file letter in the `(repertoire, parent_fen, move_uci)` index it is also the row
#: an unordered `LIMIT 1` answers with.
_R_NOEP = "rnbqkbnr/pp2pppp/2p5/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq -"


class TestPreparedMoveLookupIsDeterministic(unittest.TestCase):
    """AUDIT FIXER-W-01 / CODE-3 — the trainer's follow-up lookup must not be a coin flip.

    `evaluate_trainer_move` resolved the user's prepared follow-up to an opponent move with

        WHERE ... AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?)
          AND rm.is_user_move = 1
          AND (rs.name = ? OR rm.study_name = ?)
        LIMIT 1                      # <- and NO ORDER BY at all

    Two independent things make that predicate multi-row: the fuzzy 3-token/`LIKE` arm also
    matches the `-`-spelled twin of the position, and the `LEFT JOIN` to `study_moves`
    multiplies a row that belongs to several studies. With no `ORDER BY`, SQLite walks the
    `(repertoire, parent_fen, move_uci)` index, so it answered with the **lexicographically
    smallest `parent_fen`** — which is the twin, because `-` sorts first. The fix is the
    tie-breaker the rest of `backend/repertoire.py` already uses: exact 4-token parent first,
    then `is_important DESC`, then `id ASC`.

    The twin rows below are inserted with raw SQL because that is the only way to *create* the
    ambiguity: `add_repertoire_move` enforces one user move per logical (3-token) position, so
    this shape can only arrive from a legacy import — which is exactly the state CODE-3 is about.
    """

    @classmethod
    def setUpClass(cls):
        tests.test_setup.reset_fixtures()
        init_db()
        cls.rm = RepertoireManager.get_instance()

    def setUp(self):
        conn = get_db()
        for table in ("study_moves", "repertoire_studies", "repertoire_moves"):
            conn.execute("DELETE FROM %s" % table)
        conn.commit()
        conn.close()
        self.study = self.rm.create_study("Determinism Fixture", "white")["study"]

    @staticmethod
    def _rows(sql, params=()):
        conn = get_db()
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _insert_row(self, parent_fen, uci, san, study_name, is_important=0):
        """Insert a prepared row directly, bypassing the one-move-per-position guard."""
        conn = get_db()
        board = chess.Board(parent_fen)
        board.push_uci(uci)
        cur = conn.execute(
            """INSERT INTO repertoire_moves
               (repertoire, parent_fen, fen, move_uci, move_san, is_user_move,
                study_name, is_important)
               VALUES ('white', ?, ?, ?, ?, 1, ?, ?)""",
            (parent_fen, normalize_fen(board.fen()), uci, san, study_name, is_important))
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id

    def _plant_both_spellings(self, study_name):
        """`1.e4 c6 2.e5 d5`, with the resulting position stored BOTH ways.

        Returns `(twin_id, exact_id)`. The twin is written first, so it has the lower rowid
        *and* the lexicographically smaller `parent_fen`: both natural orders an unordered
        `LIMIT 1` could use point at the wrong row.
        """
        self.rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4",
                                    is_user_move=True, study_name=study_name)
        self.rm.add_repertoire_move("white", fixtures.FEN_AFTER_E4, "c7c6",
                                    is_user_move=False, popularity_pct=100.0)
        self.rm.add_repertoire_move("white", _AFTER_C6, "e4e5",
                                    is_user_move=True, study_name=study_name)
        res = self.rm.add_repertoire_move("white", _AFTER_E5, "d7d5",
                                          is_user_move=False, popularity_pct=100.0)
        self.assertEqual(res["status"], "success", res)
        # The premise, asserted: the opponent row really does carry the en-passant token, so the
        # exact arm and the fuzzy arm of the follow-up lookup are genuinely different rows.
        stored = self._rows("SELECT fen FROM repertoire_moves WHERE move_uci = 'd7d5'")
        self.assertEqual([r["fen"] for r in stored], [_R_EP])
        self.assertTrue(chess.Board(_R_EP).has_legal_en_passant(),
                        "the premise needs a LEGAL en-passant square, or python-chess prints '-'")

        twin = self._insert_row(_R_NOEP, "f1c4", "Bc4", study_name)
        exact = self._insert_row(_R_EP, "d2d4", "d4", study_name)
        self.assertLess(twin, exact, "the twin must have the lower rowid to be a real trap")
        self.assertLess(_R_NOEP, _R_EP,
                        "the twin must sort first in the (repertoire, parent_fen, move_uci) "
                        "index, which is the order an unordered LIMIT 1 walks")
        return twin, exact

    def test_the_study_scoped_arm_serves_the_exact_parent_row(self):
        """The study-scoped arm: `next_expected_uci` must be the exact-parent answer.

        Pre-fix this query had NO `ORDER BY` at all, so it returned the `-`-spelled twin and
        told the user to prepare `Bc4` at a position where their own prepared move is `d4`.
        """
        self._plant_both_spellings(self.study["name"])
        res = self.rm.evaluate_trainer_move(
            "white", _AFTER_C6, "e4e5", study_filter=self.study["name"])
        self.assertTrue(res["correct"], res.get("message"))
        self.assertIsNotNone(res["opponent_reply"])
        self.assertEqual(res["opponent_reply"]["uci"], "d7d5")
        self.assertEqual(
            res["next_expected_uci"], "d2d4",
            "the trainer offered %r, which is the prepared move of the `-`-spelled TWIN of the "
            "position the board is on" % res["next_expected_uci"])
        self.assertEqual(res["next_expected_san"], "d4")
        self.assertEqual(res["line_position"], "continues")

    def test_the_unscoped_fallback_arm_also_prefers_the_exact_parent(self):
        """The fallback arm already ordered by `is_important DESC, id ASC` but was still
        missing the exact-parent preference, so it answered from the twin."""
        self._plant_both_spellings("")
        res = self.rm.evaluate_trainer_move("white", _AFTER_C6, "e4e5")
        self.assertTrue(res["correct"], res.get("message"))
        self.assertEqual(res["next_expected_uci"], "d2d4",
                         "the unscoped arm answered from the wrong position")

    def test_only_the_chosen_row_is_graded_and_scheduled(self):
        """The counters must move on the row the trainer actually chose — and only there.

        This is the observable half of the CODE-3 class: an unordered `LIMIT 1` over the
        *prepared* lookup (FIXER-R2a) graded the attempt against one row and incremented a
        *different* row's `times_practiced` / `times_correct` / `next_review`. The same
        ambiguity is reproduced here through raw SQL, with the two rows as 3-token twins of
        the position the user is actually sitting in.
        """
        twin = self._insert_row(_R_NOEP, "f1c4", "Bc4", "")
        exact = self._insert_row(_R_EP, "d2d4", "d4", "")
        self.assertLess(twin, exact)
        self.assertLess(_R_NOEP, _R_EP)

        res = self.rm.evaluate_trainer_move("white", _R_EP, "d2d4")
        self.assertTrue(res["correct"], res.get("message"))
        self.assertEqual(res["prepared_id"], exact)

        by_id = {r["id"]: r for r in self._rows(
            "SELECT id, move_uci, times_practiced, times_correct, next_review "
            "FROM repertoire_moves WHERE id IN (?, ?)", (twin, exact))}
        self.assertEqual(by_id[exact]["times_practiced"], 1)
        self.assertEqual(by_id[exact]["times_correct"], 1)
        self.assertGreater(by_id[exact]["next_review"], 0)
        self.assertEqual(
            by_id[twin]["times_practiced"], 0,
            "the attempt incremented the counters of a move at a DIFFERENT position")
        self.assertEqual(by_id[twin]["times_correct"], 0)
        self.assertEqual(by_id[twin]["next_review"], 0)

    def test_the_grade_is_reported_against_the_chosen_move(self):
        """A wrong attempt must be graded against the exact row's move, not the twin's."""
        twin = self._insert_row(_R_NOEP, "f1c4", "Bc4", "")
        exact = self._insert_row(_R_EP, "d2d4", "d4", "")
        self.assertLess(twin, exact)
        self.assertLess(_R_NOEP, _R_EP)
        res = self.rm.evaluate_trainer_move("white", _R_EP, "f1c4")
        self.assertFalse(res["correct"])
        self.assertEqual(res["prepared_id"], exact)
        self.assertEqual(
            res["expected_uci"], "d2d4",
            "the trainer graded the attempt against %r, which belongs to a different position"
            % res["expected_uci"])


if __name__ == "__main__":
    unittest.main()
