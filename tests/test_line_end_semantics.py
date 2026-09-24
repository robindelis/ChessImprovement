"""
AUDIT FIXER-V / TRAINER-09 — "A line should never finish on an opponent's move."

This module is the executable form of a **product decision from the project owner**, taken after
FIXER-T declined to fix TRAINER-09 because a test documented the opposite behaviour.

The overruled behaviour
-----------------------
`evaluate_trainer_move` collected the repertoire's opponent replies for the position the user had
just reached, split them into "has a prepared follow-up" / "has none", and:

* preferred the answered branches (so a dead end was merely *deprioritised*, still reachable);
* if **every** branch was a dead end, it picked one of them anyway, played it, and returned
  `is_line_end: True`.

So the board came to rest **on the opponent's move**, with nothing for the user to play, and the
client called that "🎉 Line Complete!". The line finished on the opponent's move.

The owner's ruling
------------------
> "A line should never finish on an opponent's move."

The distinction this module pins down
------------------------------------
| case | position reached by | user has a prepared reply there | classification |
| :--- | :--- | :--- | :--- |
| genuine end of repertoire | the **user's own** move | no (it is the opponent's turn) | `completed_line` — `is_line_end: True` |
| **the overruled case** | an **opponent's** move | no | `awaiting_user_reply` — **never** a line end |
| normal continuation | an opponent's move | yes | `continues` — `is_line_end: False` |

Three consequences are asserted against the real post-conditions:

1. an opponent branch with no prepared reply is never reported as a line end and never offered as
   a drill move;
2. a genuine end at the user's own move is still a line end, and says so for the right reason;
3. the completion/counting maths (`get_repertoire_tree`'s `coverage_pct` / `gaps` /
   `unanswered_branches`) excludes the dead end, and the client renders the new state without a
   false celebration and without an error.

Nothing here weakens an existing test. The two pre-existing tests that encoded the *overruled*
behaviour were rewritten in `tests/test_trainer_hint_and_line_completion.py` (see the loud
`AUDIT FIXER-V` docstrings there and §2 of `audit/patches/FIXER-V-01.md`).
"""
import unittest

import tests.test_setup  # FIRST: points CHESS_DB_PATH at a throwaway temp database
import chess
from fastapi.testclient import TestClient

from backend.db import get_db
from backend.repertoire import (
    LINE_POSITION_AWAITING_REPLY,
    LINE_POSITION_COMPLETED,
    LINE_POSITION_CONTINUES,
    RepertoireManager,
    classify_line_position,
    normalize_fen,
)
from backend.api import app
import tests.frontend_runtime as frontend_runtime

# --------------------------------------------------------------------- positions
FEN_START = chess.STARTING_FEN
FEN_AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3"
FEN_AFTER_E6 = "rnbqkbnr/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"
FEN_AFTER_D4 = "rnbqkbnr/pppp1ppp/4p3/8/3PP3/8/PPP2PPP/RNBQKBNR b KQkq d3"
FEN_AFTER_C5 = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6"


class LineEndSemanticsBase(unittest.TestCase):
    """Isolated empty repertoire + a hermetic 2000+ explorer, and the golden invariant."""

    @classmethod
    def setUpClass(cls):
        tests.test_setup.reset_fixtures()

    @classmethod
    def tearDownClass(cls):
        tests.test_setup.assert_isolated()

    def setUp(self):
        conn = get_db()
        conn.execute("DELETE FROM study_moves")
        conn.execute("DELETE FROM repertoire_studies")
        conn.execute("DELETE FROM repertoire_moves")
        conn.commit()
        conn.close()
        self.rm = RepertoireManager()

    # ---------------------------------------------------------------- helpers
    def _seed_explorer(self, repertoire, fens, moves):
        """Deterministic `get_candidate_moves_2000` for the given positions.

        `get_repertoire_tree` asks about `moves[i]["fen"]`, the 4-token `normalize_fen()` of the
        position, whose 4th (en-passant) token depends on how it was derived and so may not match
        the caller's spelling. The table is therefore keyed by the **3-token** prefix, which is
        the position identity the trainer, `get_trainer_progress` and the coverage maths all use
        (AGENTS.md §4F, TRAINER-01). A position that is not in the table answers with an empty
        (but NOT degraded) candidate list, so it contributes nothing to coverage and is not
        reported as an explorer outage.
        """
        table = {}
        for fen in fens:
            try:
                chess.Board(fen)
            except Exception:
                continue
            key = " ".join(normalize_fen(fen).split()[:3])
            table[key] = {
                "fen": normalize_fen(fen),
                "total_games": 1000,
                "white_total": 500, "draws_total": 200, "black_total": 300,
                "eval_convention": "white",
                "moves": [
                    {"san": san, "uci": uci, "games": 100 - 10 * i,
                     "popularity_pct": pct, "white_win_pct": 50.0, "draw_pct": 30.0,
                     "black_win_pct": 20.0, "win_rate": 50.0, "average_rating": 2300,
                     "eval": 20}
                    for i, (san, uci, pct) in enumerate(moves)
                ],
            }

        def fake(fen, ratings="2000,2200,2500", color="white"):
            key = " ".join(normalize_fen(fen).split()[:3])
            return table.get(key, {"fen": fen, "total_games": 1, "moves": []})

        self.rm.get_candidate_moves_2000 = fake
        self.rm.get_game_gaps = lambda *a, **kw: {
            "gaps": [], "patched_gaps": [], "unpatched_gaps": 0, "unpatched_count": 0,
            "patched_count": 0, "total_gaps": 0, "total_games_analyzed": 0,
            "total_games_color": 0, "opening_games_degraded": False,
            "unresolved_positions": [], "color": repertoire, "min_games": 30,
        }
        return table

    def _rows(self, sql, params=()):
        conn = get_db()
        conn.row_factory = None
        try:
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def assert_golden_invariant(self):
        """FIXER-A / FIXER-R2a's invariant; this patch is read-mostly but must not break it."""
        dups = self._rows(
            "SELECT repertoire, parent_fen, COUNT(*) n FROM repertoire_moves "
            "WHERE is_user_move = 1 GROUP BY repertoire, parent_fen HAVING n > 1"
        )
        self.assertEqual(dups, [], "more than one prepared user move at one position")
        orphans = self._rows(
            "SELECT sm.* FROM study_moves sm LEFT JOIN repertoire_moves rm "
            "ON rm.repertoire = sm.repertoire AND rm.parent_fen = sm.parent_fen "
            "AND rm.move_uci = sm.move_uci WHERE rm.id IS NULL"
        )
        self.assertEqual(orphans, [], "orphaned study_moves rows")

    def _build_dead_end_and_live_branch(self):
        """`1.e4` with two opponent branches: `1... e6 -> 2.d4` (ANSWERED) and `1... c5` (DEAD END)."""
        self.rm.add_repertoire_move("white", FEN_START, "e2e4", is_user_move=True)
        self.rm.add_repertoire_move("white", FEN_AFTER_E4, "e7e6", is_user_move=False,
                                     popularity_pct=1.0)
        self.rm.add_repertoire_move("white", FEN_AFTER_E6, "d2d4", is_user_move=True)
        self.rm.add_repertoire_move("white", FEN_AFTER_E4, "c7c5", is_user_move=False,
                                     popularity_pct=100.0)


# ===================================================================== 1. the rule
class TestTheRuleItself(LineEndSemanticsBase):
    def test_a_position_reached_by_an_opponent_move_is_never_a_completed_line(self):
        """The classifier, at its narrowest: the two halves the owner separated."""
        self.assertEqual(classify_line_position(True, False), LINE_POSITION_COMPLETED)
        self.assertEqual(classify_line_position(False, False), LINE_POSITION_AWAITING_REPLY)
        self.assertEqual(classify_line_position(False, True), LINE_POSITION_CONTINUES)
        self.assertNotEqual(
            classify_line_position(False, False),
            classify_line_position(True, False),
            "an unanswered opponent branch must never be classified as a completed line",
        )
        self.assertNotEqual(LINE_POSITION_AWAITING_REPLY, LINE_POSITION_COMPLETED)
        self.assertNotEqual("awaiting_user_reply", "completed_line")


# ======================================== 2. not a line end / not offered as a drill
class TestAnUnansweredBranchIsNotADrill(LineEndSemanticsBase):
    def test_a_dead_end_branch_is_never_reported_as_a_line_end(self):
        """THE CORE REGRESSION. Pre-fix this returned `is_line_end: True` **with** the
        unanswered `1... c5` played, i.e. the line finished on the opponent's move."""
        self._build_dead_end_and_live_branch()
        for _ in range(12):
            res = self.rm.evaluate_trainer_move("white", FEN_START, "e2e4")
            self.assertTrue(res["correct"])
            # Never an end while the answer exists...
            self.assertFalse(res["is_line_end"])
            self.assertIsNone(res["line_end_reason"])
            self.assertEqual(res["line_position"], LINE_POSITION_CONTINUES)
            self.assert_golden_invariant()

    def test_a_dead_end_branch_is_never_offered_as_the_opponent_move(self):
        """`1... c5` carries `popularity_pct = 100.0` against the live `1... e6`'s `1.0`, so the
        pre-fix weighting would have selected it ~99% of the time once the filter was gone."""
        self._build_dead_end_and_live_branch()
        seen = set()
        for _ in range(25):
            res = self.rm.evaluate_trainer_move("white", FEN_START, "e2e4")
            self.assertIsNotNone(res["opponent_reply"], "no opponent reply was offered at all")
            seen.add(res["opponent_reply"]["uci"])
            self.assertIsNotNone(res["next_expected_uci"],
                                 "an offered opponent move must always have a prepared answer")
        self.assertEqual(seen, {"e7e6"},
                         "a branch the user has no answer to is not a drill choice")

    def test_a_line_whose_only_branch_is_a_dead_end_stops_on_the_users_own_move(self):
        """THE OTHER HALF: when the ONLY branch is a dead end, the line stops on the USER's own
        move and says it stopped because of a gap, not because the line is finished."""
        self.rm.add_repertoire_move("white", FEN_START, "e2e4", is_user_move=True)
        self.rm.add_repertoire_move("white", FEN_AFTER_E4, "c7c5", is_user_move=False)
        for _ in range(12):
            res = self.rm.evaluate_trainer_move("white", FEN_START, "e2e4")
            self.assertIsNone(res["opponent_reply"],
                              "the board must never be moved onto an unanswered opponent move")
            self.assertIsNone(res["next_expected_uci"])
            self.assertEqual(res["line_position"], LINE_POSITION_COMPLETED)
            self.assertTrue(res["is_line_end"])
            self.assertEqual(res["line_end_reason"], "unanswered_opponent_branches")
            self.assertEqual(res["unanswered_branches"], 1)
            self.assertEqual(res["unanswered_branch_sans"], ["c5"])
            self.assert_golden_invariant()

    def test_a_dead_end_branch_is_never_reported_as_a_line_end_api(self):
        """Same post-condition over the real HTTP boundary (`api.py` returns the manager's dict
        verbatim, so the new fields must survive serialisation)."""
        self._build_dead_end_and_live_branch()
        with TestClient(app) as client:
            res = client.post("/api/repertoire/train/attempt", json={
                "repertoire": "white", "current_fen": FEN_START, "attempted_uci": "e2e4",
            })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["correct"])
        self.assertEqual(data["opponent_reply"]["uci"], "e7e6")
        self.assertEqual(data["next_expected_uci"], "d2d4")
        self.assertFalse(data["is_line_end"])
        self.assertEqual(data["line_position"], "continues")
        self.assertIsNone(data["line_end_reason"])


# ======================================== 3. a genuine end of the repertoire still is
class TestAGenuineEndIsStillAnEnd(LineEndSemanticsBase):
    def test_the_users_own_last_prepared_move_is_a_completed_line(self):
        """`3. e5` is the end of the Anti-French the user has prepared: no opponent branch exists,
        so this is a real completion and is still reported as one."""
        board = chess.Board()
        for uci, user in (("e2e4", True), ("e7e6", False), ("d2d4", True), ("d7d5", False)):
            self.rm.add_repertoire_move("white", board.fen(), uci, is_user_move=user)
            board.push_uci(uci)
        fen_before_e5 = board.fen()
        self.rm.add_repertoire_move("white", fen_before_e5, "e4e5", is_user_move=True)

        res = self.rm.evaluate_trainer_move("white", fen_before_e5, "e4e5")
        self.assertTrue(res["correct"])
        self.assertIsNone(res["opponent_reply"],
                          "a completed line must not be extended by an opponent move")
        self.assertTrue(res["is_line_end"])
        self.assertEqual(res["line_position"], LINE_POSITION_COMPLETED)
        self.assertEqual(res["line_end_reason"], "repertoire_exhausted",
                         "a genuine end must be reported as a genuine end")
        self.assertEqual(res["unanswered_branches"], 0)
        self.assert_golden_invariant()

    def test_a_completed_line_still_reports_no_unanswered_branches_through_the_api(self):
        self.rm.add_repertoire_move("white", FEN_START, "e2e4", is_user_move=True)
        with TestClient(app) as client:
            res = client.post("/api/repertoire/train/attempt", json={
                "repertoire": "white", "current_fen": FEN_START, "attempted_uci": "e2e4",
            })
        data = res.json()
        self.assertTrue(data["correct"])
        self.assertTrue(data["is_line_end"])
        self.assertEqual(data["line_end_reason"], "repertoire_exhausted")
        self.assertEqual(data["unanswered_branches"], 0)
        self.assertEqual(data["line_position"], "completed_line")


# ======================================== 4. the completion / counting maths
class TestTheCountingMathsExcludesTheDeadEnd(LineEndSemanticsBase):
    """`get_repertoire_tree`'s `coverage_pct` used to be "is there a repertoire row for this
    opponent move", so recording `1... c5` and answering nothing to it counted as full coverage
    of `c5` — a dead end counted as a finished line."""

    CANDIDATES = (("e6", "e7e6", 50.0), ("c5", "c7c5", 30.0), ("d5", "d7d5", 20.0))

    def test_a_dead_end_branch_is_not_counted_as_covered(self):
        self._build_dead_end_and_live_branch()          # 1... e6 answered, 1... c5 dead
        self._seed_explorer("white", [FEN_AFTER_E4], list(self.CANDIDATES))
        tree = self.rm.get_repertoire_tree("white")

        # e6 is answered (2. d4 follows) and c5 is not: 50 of 100 points, NOT 80 of 100.
        self.assertAlmostEqual(tree["coverage_pct"], 50.0, places=1)
        self.assertNotAlmostEqual(tree["coverage_pct"], 80.0, places=1)

    def test_a_dead_end_branch_is_reported_as_a_gap_not_silently_dropped(self):
        self._build_dead_end_and_live_branch()
        self._seed_explorer("white", [FEN_AFTER_E4], list(self.CANDIDATES))
        tree = self.rm.get_repertoire_tree("white")
        gaps = {(g["opponent_uci"], g["gap_kind"]) for g in tree["gaps"]}
        self.assertIn(("c7c5", "unanswered_branch"), gaps,
                      "a branch the user holds but cannot answer is a gap, not a completion")
        self.assertIn(("c7c5", "unanswered_branch"), gaps)
        self.assertNotIn(("c7c5", "missing_branch"), gaps,
                         "the branch IS in the repertoire; it is unanswered, not missing")
        self.assertNotIn(("e7e6", "unanswered_branch"), gaps,
                         "an answered branch is not a gap")
        self.assertIn(("d7d5", "missing_branch"), gaps)
        for gap in tree["gaps"]:
            self.assertIn("in_repertoire", gap)

    def test_the_repertoire_side_dead_end_count_is_reported(self):
        self._build_dead_end_and_live_branch()
        self._seed_explorer("white", [FEN_AFTER_E4], list(self.CANDIDATES))
        tree = self.rm.get_repertoire_tree("white")
        self.assertEqual(tree["unanswered_branches"], 1)

    def test_adding_an_answer_to_the_dead_end_restores_its_coverage(self):
        """The differential that isolates the dead end from every other effect.

        The same position, the same explorer payload, twice: once with `1... c5` unanswered and
        once with a prepared `2. Nf3` answering it. `1... c5` carries 30 of the 100 available
        popularity points, so the two coverage figures must differ by exactly 30 - and must be
        EQUAL when the answer is added. Before the patch `1... c5` counted as covered in *both*
        cases, because "is there a repertoire row for it" was the whole test.

        The rows are written with `normalize_fen()` on both sides so the stored 4-token keys are
        exactly consistent; otherwise the *separate* en-passant-twin ambiguity (CODE-3 /
        FIXER-T residual) would also change the number and the comparison would not isolate the
        dead end.
        """
        def write(answer_c5):
            conn = get_db()
            rows = [
                # (parent, fen, uci, san, is_user_move)
                (normalize_fen(FEN_START), normalize_fen(FEN_AFTER_E4), "e2e4", "e4", 1),
                (normalize_fen(FEN_AFTER_E4), normalize_fen(FEN_AFTER_E6), "e7e6", "e6", 0),
                (normalize_fen(FEN_AFTER_E6), normalize_fen(FEN_AFTER_D4), "d2d4", "d4", 1),
                (normalize_fen(FEN_AFTER_E4), normalize_fen(FEN_AFTER_C5), "c7c5", "c5", 0),
            ]
            if answer_c5:
                rows.append((normalize_fen(FEN_AFTER_C5), normalize_fen(FEN_AFTER_C5 + " "),
                             "g1f3", "Nf3", 1))
            for parent, fen, uci, san, user in rows:
                if answer_c5 and uci == "g1f3":
                    fen = normalize_fen("rnbqkbnr/pp1ppppp/8/2p5/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq -")
                conn.execute(
                    "INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san,"
                    " is_user_move) VALUES ('white', ?, ?, ?, ?, ?)",
                    (parent, fen, uci, san, user))
            conn.commit()
            conn.close()

        def coverage(answer_c5):
            conn = get_db()
            for table in ("study_moves", "repertoire_studies", "repertoire_moves"):
                conn.execute("DELETE FROM %s" % table)
            conn.commit()
            conn.close()
            write(answer_c5)
            self._seed_explorer("white", [FEN_AFTER_E4], list(self.CANDIDATES))
            return self.rm.get_repertoire_tree("white")["coverage_pct"]

        with_dead_end = coverage(answer_c5=False)
        with_answer = coverage(answer_c5=True)

        self.assertAlmostEqual(with_dead_end, 50.0, places=1,
                               msg="only the ANSWERED branch (1... e6, 50 points) is coverage")
        self.assertAlmostEqual(with_answer, 80.0, places=1,
                               msg="once 1... c5 is answered, its 30 points are coverage too")
        self.assertAlmostEqual(with_answer - with_dead_end, 30.0, places=1,
                               msg="a dead end must contribute exactly 0 to coverage")

    def test_a_fully_covered_repertoire_reports_100_percent_and_no_gaps(self):
        """The non-regression half: a repertoire that answers everything is unaffected."""
        self.rm.add_repertoire_move("white", FEN_START, "e2e4", is_user_move=True)
        self.rm.add_repertoire_move("white", FEN_AFTER_E4, "e7e6", is_user_move=False)
        self.rm.add_repertoire_move("white", FEN_AFTER_E6, "d2d4", is_user_move=True)
        self.rm.add_repertoire_move("white", FEN_AFTER_E4, "c7c5", is_user_move=False)
        self.rm.add_repertoire_move("white", FEN_AFTER_C5, "g1f3", is_user_move=True)
        # The only opponent position with a seeded candidate is the one after 1.e4, and its one
        # candidate (`1... e6`) is answered by the prepared `2. d4`. The other two opponent
        # positions get the stub's empty (non-degraded) list and so contribute nothing.
        self._seed_explorer("white", [FEN_AFTER_E4], [("e6", "e7e6", 100.0)])
        tree = self.rm.get_repertoire_tree("white")
        self.assertEqual(tree["unanswered_branches"], 0)
        self.assertEqual(tree["coverage_pct"], 100.0)
        self.assertEqual(tree["gaps"], [])

    def test_progress_counting_only_ever_counted_prepared_user_moves(self):
        """`get_trainer_progress` is computed from `is_user_move = 1` rows only, so a dead-end
        opponent branch was never a "completed move" in the mastery maths. Pinned so a future
        change cannot quietly widen the denominator."""
        self._build_dead_end_and_live_branch()
        progress = self.rm.get_trainer_progress("white")
        self.assertEqual(progress["total_moves"], 2, "only 1.e4 and 2.d4 are prepared user moves")
        self.assertEqual(progress["mastered_count"], 0)
        self.assertEqual(progress["unlearned_count"], 2)
        # Drilling 1.e4 is unaffected by the dead end being present.
        self.rm.evaluate_trainer_move("white", FEN_START, "e2e4")
        progress2 = self.rm.get_trainer_progress("white")
        self.assertEqual(progress2["total_moves"], 2)


# ======================================== 5. the client
class TestTheClientRendersTheNewState(LineEndSemanticsBase):
    """The REAL `frontend/repertoire_trainer.js`, executed by `tests/frontend_harness.js`."""

    SCENARIO = r"""
        // 1. A genuine completion: the user's own move is the last thing prepared.
        const trainer = new RepertoireTrainer();
        const completed = trainer._lineEndPresentation({
            is_line_end: true,
            line_end_reason: 'repertoire_exhausted',
            line_position: 'completed_line',
            unanswered_branches: 0
        });

        // 2. The new state: the repertoire holds opponent branches the user cannot answer.
        const pausedOne = trainer._lineEndPresentation({
            is_line_end: true,
            line_end_reason: 'unanswered_opponent_branches',
            line_position: 'completed_line',
            unanswered_branches: 1
        });
        const pausedTwo = trainer._lineEndPresentation({
            is_line_end: true,
            line_end_reason: 'unanswered_opponent_branches',
            line_position: 'completed_line',
            unanswered_branches: 2
        });

        // 3. A line that continues must never render as an end.
        const continuing = trainer._lineEndPresentation({
            is_line_end: false,
            line_end_reason: null,
            line_position: 'continues',
            next_expected_uci: 'd2d4',
            opponent_reply: { san: 'e6' }
        });

        // 4. An old-shaped payload (no line_end_reason) that still claims an end on an
        //    opponent's move must not be celebrated either.
        const legacy = trainer._lineEndPresentation({
            is_line_end: true,
            opponent_reply: { san: 'O-O' },
            next_expected_uci: null
        });

        return {
            completed, pausedOne, pausedTwo, continuing, legacy,
            hasMethod: typeof trainer._lineEndPresentation === 'function'
        };
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.node_missing = frontend_runtime.missing_frontend_sources()
        if not cls.node_missing and frontend_runtime.node_available():
            cls.result = frontend_runtime.run_scenario(cls.SCENARIO)
            cls.provenance = frontend_runtime.harness_provenance(cls.result)
        else:
            cls.result = None
            cls.provenance = None

    def setUp(self):
        super().setUp()
        if self.node_missing or not frontend_runtime.node_available():
            self.skipTest("node unavailable: %s" % (self.node_missing,))
        # A missing or emptied frontend file is a FAILURE, never a skip.
        self.assertNotIn("frontend/repertoire_trainer.js", self.node_missing)

    def test_the_harness_actually_executed_the_real_trainer_source(self):
        from tests.frontend_runtime import frontend_sha256
        self.assertIn("frontend/repertoire_trainer.js", self.provenance)
        self.assertEqual(self.provenance["frontend/repertoire_trainer.js"],
                         frontend_sha256("frontend/repertoire_trainer.js"),
                         "the harness did not execute the shipped repertoire_trainer.js")

    def test_a_genuine_end_is_still_celebrated(self):
        done = self.result["completed"]
        self.assertTrue(done["completed"])
        self.assertIn("Line Complete", done["toast"])
        self.assertIn("Line Complete", done["text"])
        self.assertEqual(done["color"], "#4ade80")

    def test_a_dead_end_is_not_celebrated_and_is_not_an_error(self):
        for key, n in (("pausedOne", 1), ("pausedTwo", 2)):
            paused = self.result[key]
            self.assertFalse(paused["completed"],
                             "%s: a dead end must never be reported as a completed line" % key)
            self.assertNotIn("Line Complete", paused["toast"])
            self.assertNotIn("Line Complete", paused["text"])
            self.assertNotIn("🎉", paused["toast"])
            # Non-alarming: amber, and it names the fix instead of blaming the user.
            self.assertEqual(paused["color"], "#f59e0b")
            self.assertIn("Line paused", paused["text"])
            self.assertIn("Repertoire Builder", paused["text"])
            self.assertIn("Repertoire Builder", paused["toast"])
            self.assertIn(str(n), paused["text"])

    def test_a_continuing_line_is_not_rendered_as_an_end(self):
        going = self.result["continuing"]
        self.assertFalse(going["completed"])
        self.assertIn("Line paused", going["text"])
        self.assertEqual(going["color"], "#f59e0b")

    def test_a_legacy_payload_that_ends_on_an_opponent_move_is_not_celebrated(self):
        legacy = self.result["legacy"]
        self.assertFalse(legacy["completed"],
                         "a payload that ends the line on the opponent's move must not be "
                         "celebrated, even if it carries no line_end_reason")
        self.assertNotIn("Line Complete", legacy["text"])
        self.assertEqual(legacy["color"], "#f59e0b")

    def test_the_source_no_longer_celebrates_an_opponent_move_as_a_completed_line(self):
        source = frontend_runtime.frontend_source("frontend/repertoire_trainer.js")
        # The only place a completion is announced must be the presentation helper, and the
        # "opponent replied ... Line Complete" string must be gone for good.
        self.assertNotIn("Opponent replied with ${opp.san}. 🎉 Line Complete!", source)
        self.assertIn("_lineEndPresentation", source)
        # And the honest fallback must exist for a payload that does carry an unanswered reply.
        self.assertIn("no prepared answer here", source)


if __name__ == "__main__":
    unittest.main()
