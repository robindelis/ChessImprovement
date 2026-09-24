"""FIXER-K-01 regression tests for `backend/analyzer.py`.

Covers the findings owned by this module:

* **SYNC-06** -- a mate score collapsed to ``+/-9999`` and a plain ``-95.00``
  evaluation was explained to the user as "Checkmate in 1".  The mate distance
  must come from the engine score's own ``mate`` field, never from a magnitude
  test on centipawns, and the three required cases (a real mate in 1, a real
  mate in 2, and a -95.00 cp score) must each be correct and mutually
  different.
* **SYNC-15** -- re-analysing a game duplicated its `blunders` rows.  The write
  path must be an idempotent upsert on the natural key
  ``(game_id, ply, played_move)``.
* per-game atomicity: one transaction, the connection closed on every path, and
  a caller cannot mark a game analysed unless the analysis really completed.

Engine policy: only `TestRealEngineMateScores` touches the binary.  It uses the
process-wide pool (FIXER-C), depth 8, tiny endgames, and closes the pool in
`tearDownClass`.  Every other test uses `chess.engine.Cp` / `chess.engine.Mate`
score objects, so it is deterministic and instant.

Database policy: the write-path tests run against a **private scratch database**
created by `init_db()` in `setUpClass`, never the shared suite database.  That
is deliberate: `backend/db.py` (FIXER-P0) does not create a `best_pv` column
even though `analyzer.py` and `api.py` both read and write it, so on a fresh
schema the pre-fix `INSERT` fails outright with
``sqlite3.OperationalError: no column named best_pv``.  The scratch database
adds that column here so these tests can measure the behaviour under test; the
schema gap itself is handed off to FIXER-P0, not worked around in the module.
"""
import os
import sqlite3
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.test_setup  # noqa: E402  FIRST backend-adjacent import (temp DB only)
import chess  # noqa: E402
import chess.engine  # noqa: E402
import backend.db as db_mod  # noqa: E402
from backend.analyzer import (  # noqa: E402
    MATE_SCORE_CP,
    MAX_CPL_LOSS,
    GameAnalyzer,
    centipawn_loss,
    describe_score_mate,
    mate_distance_moves,
    normalize_score,
)
from backend.db import get_db  # noqa: E402

FIXERK_PREFIX = "fixerk_"

#: The three required explanations must never claim a mate.  "no forced mate" is
#: an explicit disambiguator, so the forbidden substrings are the claims.
MATE_CLAIM_MARKERS = ("checkmate", "mate in")


def assert_no_mate_claim(case, text):
    lowered = text.lower()
    for marker in MATE_CLAIM_MARKERS:
        case.assertNotIn(marker, lowered, "explanation claims a mate: %r" % text)


def pv_of(board, sans):
    b = board.copy()
    out = []
    for san in sans:
        try:
            m = b.parse_san(san)
        except Exception:
            m = next(iter(b.legal_moves), None)
            if m is None:
                break
        out.append(m)
        b.push(m)
    return out


class ScriptedEngine:
    """Engine stub: `fn(board)` -> (score from the *user's* colour, [pv moves])."""

    def __init__(self, fn, user_color=chess.WHITE):
        self.fn = fn
        self.uc = user_color
        self.calls = 0

    def analyse(self, board, limit=None):
        self.calls += 1
        score, pv = self.fn(board)
        return {"score": chess.engine.PovScore(score, self.uc), "pv": pv}


def explanation_for(analyzer, score_after, mate_after=None, mate_before=None, cpl_loss=None):
    """Call the real explanation generator with a concrete score."""
    board = chess.Board()
    if cpl_loss is None:
        cpl_loss = 300 if mate_after is None else MATE_SCORE_CP
    return analyzer._generate_tactical_explanation(
        board, chess.Move.from_uci("e2e4"), chess.Move.from_uci("e2e4"), [],
        cpl_loss, "Test Opening", score_after=score_after,
        mate_after=mate_after, mate_before=mate_before,
    )


class ScratchDbTestCase(unittest.TestCase):
    """Base class for tests that write rows: a private, throwaway database."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._shared_db_path = os.environ.get("CHESS_DB_PATH")
        cls._scratch_path = os.path.join(
            os.environ.get("TMPDIR", "/tmp"),
            "chess_test_fixerk_%s_%d.db" % (cls.__name__, os.getpid()),
        )
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(cls._scratch_path + suffix):
                os.unlink(cls._scratch_path + suffix)
        os.environ["CHESS_DB_PATH"] = cls._scratch_path
        db_mod.set_db_path(cls._scratch_path)
        db_mod.init_db()
        # See the module docstring: `best_pv` is missing from `db.py`'s schema.
        conn = get_db()
        try:
            present = {row[1] for row in conn.execute("PRAGMA table_info(blunders)")}
            if "best_pv" not in present:
                conn.execute("ALTER TABLE blunders ADD COLUMN best_pv TEXT")
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        if cls._shared_db_path is None:
            os.environ.pop("CHESS_DB_PATH", None)
        else:
            os.environ["CHESS_DB_PATH"] = cls._shared_db_path
        if cls._shared_db_path:
            db_mod.set_db_path(cls._shared_db_path)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(cls._scratch_path + suffix):
                try:
                    os.unlink(cls._scratch_path + suffix)
                except OSError:
                    pass
        super().tearDownClass()


def _blunder_row(game_id, ply, played_move="d2d4", **overrides):
    row = {
        "game_id": game_id, "move_number": 1, "ply": ply, "color": "white",
        "fen_before": chess.STARTING_FEN, "fen_after": chess.STARTING_FEN,
        "played_move": played_move, "played_san": "d4",
        "best_move": "g1f3", "best_san": "Nf3",
        "eval_before": 20, "eval_after": -280, "cpl_loss": 300,
        "opening_name": "Queen's Pawn", "eco": "A40", "is_book_move": 1,
        "refutation_pv": "[]", "best_pv": "[]",
        "explanation": "Eval loss: 300 centipawns (-2.80 pawns, no forced mate).",
        "strategic_idea": "Engine top choice: Nf3.", "category": "Blunder",
        "created_at": int(time.time()),
    }
    row.update(overrides)
    return row


def _rows(game_id):
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, ply, played_move, cpl_loss, explanation, eval_before, eval_after "
            "FROM blunders WHERE game_id = ? ORDER BY id", (game_id,)).fetchall()]
    finally:
        conn.close()


def _count_blunders(game_id):
    return len(_rows(game_id))


def _insert_raw_blunder(conn, game_id, ply, played_move, cpl_loss, created_at=1):
    conn.execute(
        "INSERT INTO blunders (game_id, move_number, ply, color, fen_before, fen_after, "
        "played_move, played_san, best_move, best_san, eval_before, eval_after, cpl_loss, "
        "opening_name, eco, is_book_move, refutation_pv, best_pv, explanation, "
        "strategic_idea, category, created_at) "
        "VALUES (?, 1, ?, 'white', 'fen', 'fen', ?, 'd4', 'g1f3', 'Nf3', 20, -280, ?, "
        "'Q', 'A40', 1, '[]', '[]', 'x', 'y', 'Blunder', ?)",
        (game_id, ply, played_move, cpl_loss, created_at),
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


class TestMateDistanceIsNeverAMagnitudeTest(unittest.TestCase):
    """SYNC-06: the mate field decides, the centipawn magnitude never does."""

    def setUp(self):
        self.analyzer = GameAnalyzer()

    def tearDown(self):
        self.analyzer.close()

    def explanation(self, score_after, mate_after=None, mate_before=None, cpl_loss=None):
        return explanation_for(self.analyzer, score_after, mate_after, mate_before, cpl_loss)

    def test_real_mate_in_1_is_explained_as_checkmate_in_1(self):
        # What the engine really returns after 1.f3 e5 2.g4: PovScore(Mate(-1)),
        # white's POV, i.e. white is getting mated (verified with the real binary
        # in audit/scripts/FIXER-K-01_materepro.py).
        cp, mate = normalize_score(chess.engine.Mate(-1))
        self.assertEqual(mate, -1)
        self.assertEqual(cp, -MATE_SCORE_CP)
        why, _idea, category = self.explanation(cp, mate_after=mate)
        self.assertIn("checkmate in 1", why.lower())
        self.assertNotIn("checkmate in 2", why.lower())
        self.assertEqual(category, "Blunder")

    def test_real_mate_in_2_is_explained_as_checkmate_in_2(self):
        # Real Stockfish on "6RK/6PP/5n2/8/2n5/8/8/7k b - - 0 1" (black to move)
        # gives PovScore(Mate(-2)) for white: 1...Ne5 2...Ng6#.
        cp, mate = normalize_score(chess.engine.Mate(-2))
        self.assertEqual(mate, -2)
        why, _idea, _cat = self.explanation(cp, mate_after=mate)
        self.assertIn("checkmate in 2", why.lower())
        self.assertNotIn("checkmate in 1", why.lower())

    def test_minus_95_cp_is_never_called_a_checkmate(self):
        cp, mate = normalize_score(chess.engine.Cp(-95))
        self.assertIsNone(mate)
        why, _idea, _cat = self.explanation(cp, mate_after=mate, cpl_loss=95)
        assert_no_mate_claim(self, why)
        self.assertIn("95 centipawns", why)
        self.assertIn("-0.95", why)

    def test_the_three_required_cases_are_correct_and_mutually_different(self):
        cp1, mate1 = normalize_score(chess.engine.Mate(-1))
        cp2, mate2 = normalize_score(chess.engine.Mate(-2))
        cp95, mate95 = normalize_score(chess.engine.Cp(-95))
        text_mate1 = explanation_for(self.analyzer, cp1, mate_after=mate1)[0]
        text_mate2 = explanation_for(self.analyzer, cp2, mate_after=mate2)[0]
        text_cp95 = explanation_for(self.analyzer, cp95, mate_after=mate95, cpl_loss=95)[0]
        self.assertEqual(len({text_mate1, text_mate2, text_cp95}), 3,
                         (text_mate1, text_mate2, text_cp95))
        self.assertIn("checkmate in 1", text_mate1.lower())
        self.assertIn("checkmate in 2", text_mate2.lower())
        assert_no_mate_claim(self, text_cp95)

    def test_a_huge_centipawn_score_is_not_a_checkmate(self):
        # -9500 cp is -95.00 pawns: a completely lost but perfectly playable
        # position.  The pre-fix `score_after <= -9000` test called this mate.
        for cp in (-9000, -9500, -10000, -12345):
            text, _idea, _cat = self.explanation(cp, mate_after=None, cpl_loss=9500)
            assert_no_mate_claim(self, text)
            self.assertIn("centipawns", text)

    def test_mate_distance_comes_from_the_field_not_the_magnitude(self):
        # Every mate distance saturates to the same cp sentinel, so only the
        # mate field can tell them apart.  Pre-fix all of these said
        # "Checkmate in 1".
        texts = {}
        for distance in (1, 2, 3, 15, 99):
            cp, mate = normalize_score(chess.engine.Mate(-distance))
            self.assertEqual(cp, -MATE_SCORE_CP)
            texts[distance] = explanation_for(self.analyzer, cp, mate_after=mate)[0]
        self.assertEqual(len(set(texts.values())), len(texts), texts)
        self.assertIn("checkmate in 1", texts[1].lower())
        self.assertIn("checkmate in 15", texts[15].lower())
        self.assertIn("checkmate in 99", texts[99].lower())

    def test_a_mate_the_user_is_delivering_is_not_described_as_a_loss(self):
        cp, mate = normalize_score(chess.engine.Mate(3))
        self.assertEqual(cp, MATE_SCORE_CP)
        text = self.explanation(cp, mate_after=mate)[0]
        self.assertIn("forced mate in 3", text.lower())
        self.assertNotIn("allows checkmate", text.lower())

    def test_walking_into_a_mate_you_had_is_reported_as_such(self):
        text = self.explanation(-MATE_SCORE_CP, mate_after=-1, mate_before=2)[0]
        self.assertIn("checkmate in 1", text.lower())
        self.assertIn("forced mate in 2 for you before the move", text.lower())

    def test_describe_score_mate_never_invents_a_mate(self):
        self.assertEqual(describe_score_mate(None), "")
        self.assertEqual(describe_score_mate(-1), "allows checkmate in 1")
        self.assertEqual(describe_score_mate(-2), "allows checkmate in 2")
        self.assertEqual(describe_score_mate(4), "reaches a forced mate in 4 for you")
        self.assertEqual(mate_distance_moves(None), 0)
        self.assertEqual(mate_distance_moves(0), 1)

    def test_normalize_score_saturates_and_preserves_the_mate_field(self):
        self.assertEqual(normalize_score(chess.engine.Cp(120)), (120, None))
        self.assertEqual(normalize_score(chess.engine.Cp(-95)), (-95, None))
        self.assertEqual(normalize_score(chess.engine.Mate(1)), (MATE_SCORE_CP, 1))
        self.assertEqual(normalize_score(chess.engine.Mate(-1)), (-MATE_SCORE_CP, -1))
        self.assertEqual(normalize_score(chess.engine.Mate(-30)), (-MATE_SCORE_CP, -30))
        # A PovScore is re-pointed before the split, exactly like the old call.
        pov = chess.engine.PovScore(chess.engine.Mate(-2), chess.WHITE)
        self.assertEqual(normalize_score(pov, chess.WHITE), (-MATE_SCORE_CP, -2))
        self.assertEqual(normalize_score(pov, chess.BLACK), (MATE_SCORE_CP, 2))
        # MateGiven (a score with no numeric value at all) must not blow up.
        self.assertEqual(normalize_score(chess.engine.MateGiven, chess.WHITE),
                         (MATE_SCORE_CP, 0))
        self.assertEqual(normalize_score(chess.engine.Mate(0), chess.WHITE),
                         (MATE_SCORE_CP, 0))

    def test_centipawn_loss_is_bounded(self):
        self.assertEqual(centipawn_loss(120, -300), 420)
        self.assertEqual(centipawn_loss(-300, 120), 0)
        # mate for the user -> mated in one: 20000 raw, clamped.
        self.assertEqual(centipawn_loss(MATE_SCORE_CP, -MATE_SCORE_CP), MAX_CPL_LOSS)
        self.assertLessEqual(centipawn_loss(MATE_SCORE_CP, -MATE_SCORE_CP), MATE_SCORE_CP)


class TestMateAwareEndToEnd(unittest.TestCase):
    """The whole `analyze_game_openings` pipeline, with a scripted engine."""

    def setUp(self):
        self.analyzer = GameAnalyzer()
        # Pin the threshold so the cpl arithmetic under test is deterministic.
        self._prev_threshold = db_mod.get_setting("blunder_threshold_cp", None)
        db_mod.set_setting("blunder_threshold_cp", "150")

    def tearDown(self):
        if self._prev_threshold is None:
            db_mod.set_setting("blunder_threshold_cp", "150")
        else:
            db_mod.set_setting("blunder_threshold_cp", self._prev_threshold)
        self.analyzer.close()

    def _run(self, game_id, scores):
        """scores: list of (before, after) score objects from the user's POV."""
        state = {"n": 0}

        def fn(board):
            before, after = scores[min(state["n"] // 2, len(scores) - 1)]
            score = before if state["n"] % 2 == 0 else after
            state["n"] += 1
            return score, pv_of(board, ["e5", "Nf6", "d4"])

        self.analyzer._engine = ScriptedEngine(fn)
        self.analyzer._pool = None
        return self.analyzer.analyze_game_openings({
            "id": game_id, "user_color": "white", "moves": "e4 e5 Nf3", "opening": {},
        })

    def test_walking_into_a_mate_in_1(self):
        blunders = self._run(FIXERK_PREFIX + "m1", [
            (chess.engine.Cp(200), chess.engine.Mate(-1)),
            (chess.engine.Cp(200), chess.engine.Mate(-1)),
        ])
        self.assertEqual(len(blunders), 1, blunders)
        b = blunders[0]
        self.assertEqual(b["eval_after"], -MATE_SCORE_CP)   # saturated, not -9999
        self.assertIsNone(b["eval_before_mate"])
        self.assertEqual(b["eval_after_mate"], -1)
        self.assertIn("checkmate in 1", b["explanation"].lower())
        self.assertLessEqual(b["cpl_loss"], MAX_CPL_LOSS)

    def test_walking_into_a_mate_in_2_is_not_reported_as_mate_in_1(self):
        blunders = self._run(FIXERK_PREFIX + "m2", [
            (chess.engine.Cp(200), chess.engine.Mate(-2)),
            (chess.engine.Cp(200), chess.engine.Mate(-2)),
        ])
        self.assertEqual(len(blunders), 1, blunders)
        b = blunders[0]
        self.assertEqual(b["eval_after_mate"], -2)
        self.assertIn("checkmate in 2", b["explanation"].lower())
        self.assertNotIn("checkmate in 1", b["explanation"].lower())

    def test_losing_a_forced_mate_is_bounded_and_kept(self):
        # eval_before = mate in 5 for the user, after the move = -100 cp.
        blunders = self._run(FIXERK_PREFIX + "lostmate", [
            (chess.engine.Mate(5), chess.engine.Cp(-100)),
            (chess.engine.Mate(5), chess.engine.Cp(-100)),
        ])
        self.assertEqual(len(blunders), 1, blunders)
        b = blunders[0]
        self.assertEqual(b["eval_before"], MATE_SCORE_CP)
        self.assertEqual(b["eval_before_mate"], 5)
        self.assertIsNone(b["eval_after_mate"])
        # Pre-fix this row stored cpl_loss = 10100 and the raw 9995 sentinel.
        self.assertEqual(b["cpl_loss"], MAX_CPL_LOSS)
        # The position after the move is only -1.00: no checkmate is claimed,
        # the mate that was *available* is what gets reported.
        self.assertNotIn("checkmate", b["explanation"].lower())
        self.assertNotIn("allows checkmate", b["explanation"].lower())
        self.assertIn("forced mate in 5 for you before the move", b["explanation"].lower())

    def test_huge_cp_blunder_is_never_described_as_a_checkmate(self):
        blunders = self._run(FIXERK_PREFIX + "cp", [
            (chess.engine.Cp(-100), chess.engine.Cp(-9500)),
            (chess.engine.Cp(-100), chess.engine.Cp(-9500)),
        ])
        self.assertEqual(len(blunders), 1, blunders)
        b = blunders[0]
        self.assertEqual(b["eval_after"], -9500)
        self.assertIsNone(b["eval_after_mate"])
        assert_no_mate_claim(self, b["explanation"])
        self.assertIn("9400 centipawns", b["explanation"])
        self.assertIn("-95.00", b["explanation"])

    def test_ordinary_95_cp_blunder_is_never_described_as_a_checkmate(self):
        blunders = self._run(FIXERK_PREFIX + "cp95", [
            (chess.engine.Cp(100), chess.engine.Cp(-95)),
            (chess.engine.Cp(100), chess.engine.Cp(-95)),
        ])
        self.assertEqual(len(blunders), 1, blunders)
        b = blunders[0]
        assert_no_mate_claim(self, b["explanation"])
        self.assertEqual(b["cpl_loss"], 195)
        self.assertIn("195 centipawns", b["explanation"])
        self.assertIn("-0.95", b["explanation"])


def _fen_after(*sans):
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    return board.fen()


#: After 1.f3 e5 2.g4 the engine's best reply is 2...Qh4#.
REAL_MATE_IN_1 = _fen_after("f3", "e5", "g4")
#: 1...Ne5 2...Ng6#: no mate in 1 exists and every white reply after 1...Ne5 is
#: met by Ng6#/Nf7#.
REAL_MATE_IN_2 = "6RK/6PP/5n2/8/2n5/8/8/7k b - - 0 1"


class TestRealEngineMateScores(unittest.TestCase):
    """The same three cases, but the scores come from the real Stockfish binary.

    Bounded: depth 8 on two tiny endgames, one engine from the shared pool,
    released in tearDownClass.  Each FEN is proved to be the position it claims
    to be (python-chess), so the test cannot pass on a mis-transcribed FEN.
    """

    REAL_MATE_IN_1 = REAL_MATE_IN_1
    REAL_MATE_IN_2 = REAL_MATE_IN_2

    @classmethod
    def setUpClass(cls):
        from backend.analyzer import STOCKFISH_PATH
        if not os.path.exists(STOCKFISH_PATH):
            raise unittest.SkipTest("stockfish binary not present")
        cls.analyzer = GameAnalyzer()
        cls.engine = cls.analyzer._get_engine()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "analyzer", None) is not None:
            cls.analyzer.close()
        try:
            from backend.evaluator import get_sync_engine_pool
            get_sync_engine_pool().close_all()
        except Exception:
            pass

    def setUp(self):
        # Guard: the first FEN must really be "black mates in one move".
        probe = chess.Board(self.REAL_MATE_IN_1)
        mates = []
        for move in list(probe.legal_moves):
            trial = probe.copy()
            trial.push(move)
            if trial.is_checkmate():
                mates.append(probe.san(move))
        self.assertEqual(mates, ["Qh4#"], mates)

        # Guard: the second FEN must have no mate in 1 and a forced mate in 2.
        probe2 = chess.Board(self.REAL_MATE_IN_2)
        self.assertEqual(probe2.turn, chess.BLACK)
        for move in list(probe2.legal_moves):
            trial = probe2.copy()
            trial.push(move)
            self.assertFalse(trial.is_checkmate(), probe2.san(move))
        setup = probe2.copy()
        setup.push_san("Ne5")
        for move in list(setup.legal_moves):
            trial = setup.copy()
            trial.push(move)
            mated = False
            for reply in list(trial.legal_moves):
                after = trial.copy()
                after.push(reply)
                if after.is_checkmate():
                    mated = True
                    break
            self.assertTrue(mated, "white escapes with %s" % setup.san(move))

    def _score(self, fen):
        board = chess.Board(fen)
        info = self.engine.analyse(board, chess.engine.Limit(depth=8))
        return info["score"].pov(chess.WHITE)

    def test_real_engine_reports_mate_in_1_and_the_text_agrees(self):
        score = self._score(self.REAL_MATE_IN_1)
        self.assertEqual(score.mate(), -1, "engine no longer sees the Fool's mate")
        cp, mate = normalize_score(score)
        self.assertEqual((cp, mate), (-MATE_SCORE_CP, -1))
        text = explanation_for(self.analyzer, cp, mate_after=mate)[0]
        self.assertIn("checkmate in 1", text.lower())
        self.assertNotIn("checkmate in 2", text.lower())

    def test_real_engine_reports_mate_in_2_and_the_text_agrees(self):
        score = self._score(self.REAL_MATE_IN_2)
        self.assertEqual(score.mate(), -2, "engine no longer sees the smothered mate")
        cp, mate = normalize_score(score)
        self.assertEqual((cp, mate), (-MATE_SCORE_CP, -2))
        text = explanation_for(self.analyzer, cp, mate_after=mate)[0]
        self.assertIn("checkmate in 2", text.lower())
        self.assertNotIn("checkmate in 1", text.lower())

    def test_real_engine_cp_score_is_never_a_checkmate(self):
        score = self._score("r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 w kq - 0 6")
        self.assertIsNone(score.mate())
        cp, mate = normalize_score(score)
        self.assertIsNone(mate)
        text = explanation_for(self.analyzer, cp, mate_after=mate, cpl_loss=abs(cp) or 1)[0]
        assert_no_mate_claim(self, text)


class TestIdempotentWritePath(ScratchDbTestCase):
    """SYNC-15: re-analysing a game must converge on the same rows."""

    def setUp(self):
        self.analyzer = GameAnalyzer()

    def tearDown(self):
        self.analyzer.close()

    def test_repeated_analysis_converges_on_one_row(self):
        gid = FIXERK_PREFIX + "idem"
        rows = [_blunder_row(gid, 1), _blunder_row(gid, 3, played_move="g1f3")]
        for _ in range(3):
            self.analyzer.save_blunders_to_db(rows)
        stored = _rows(gid)
        self.assertEqual(len(stored), 2, stored)
        self.assertEqual(sorted(r["ply"] for r in stored), [1, 3])

    def test_resaving_updates_instead_of_appending(self):
        gid = FIXERK_PREFIX + "upd"
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1, cpl_loss=300)])
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1, cpl_loss=450,
                                                        explanation="new text")])
        stored = _rows(gid)
        self.assertEqual(len(stored), 1, stored)
        self.assertEqual(stored[0]["cpl_loss"], 450)
        self.assertEqual(stored[0]["explanation"], "new text")

    def test_preexisting_duplicate_rows_are_collapsed(self):
        # The state under test is "a database written BEFORE the UNIQUE index
        # existed, which may already hold several rows for one natural key".
        # Since FIXER-N the *migrated* schema refuses to create that state (that
        # is the whole point of `ux_blunders_natural_key`), so it is built the
        # way a real pre-index database has it: drop the index, insert, put the
        # index back.  The boot-path equivalent is asserted in
        # tests/test_schema_integrity.py::TestLegacyDatabaseMigration.
        gid = FIXERK_PREFIX + "dup"
        conn = get_db()
        try:
            conn.execute("DROP INDEX ux_blunders_natural_key")
            conn.commit()
        finally:
            conn.close()
        conn = get_db()
        try:
            first = _insert_raw_blunder(conn, gid, 1, "d2d4", 300)
            second = _insert_raw_blunder(conn, gid, 1, "d2d4", 300)
            # The second copy is the one the user actually practised.
            conn.execute("INSERT INTO drill_reviews (blunder_id, attempts, successes) "
                         "VALUES (?, 7, 3)", (second,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(_count_blunders(gid), 2)

        summary = self.analyzer.save_blunders_to_db([_blunder_row(gid, 1, cpl_loss=300)])

        self.assertEqual(_count_blunders(gid), 1)
        self.assertEqual(summary["duplicates_removed"], 1)
        # The practised copy survived: no spaced-repetition progress is lost.
        conn = get_db()
        try:
            drill = conn.execute("SELECT blunder_id, attempts, successes FROM drill_reviews "
                                 "WHERE blunder_id IN (?, ?)", (first, second)).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(drill), 1, drill)
        self.assertEqual(drill[0]["attempts"], 7)
        self.assertEqual(drill[0]["blunder_id"], second)
        # Leave the scratch schema exactly as init_db() produced it.
        conn = get_db()
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_blunders_natural_key "
                         "ON blunders (game_id, ply, played_move)")
            conn.commit()
        finally:
            conn.close()

    def test_a_second_blunder_at_a_different_ply_is_not_a_duplicate(self):
        gid = FIXERK_PREFIX + "plies"
        self.analyzer.save_blunders_to_db([
            _blunder_row(gid, 1, played_move="d2d4"),
            _blunder_row(gid, 1, played_move="c2c4"),
        ])
        self.assertEqual(_count_blunders(gid), 2)

    def test_concurrent_saves_of_one_game_converge_on_one_row(self):
        gid = FIXERK_PREFIX + "race"
        rows = [_blunder_row(gid, 1)]
        errors = []

        def worker():
            try:
                self.analyzer.save_blunders_to_db(rows)
            except Exception as exc:  # pragma: no cover - diagnostic
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(_count_blunders(gid), 1)

    def test_analyze_and_store_is_atomic_and_idempotent(self):
        gid = FIXERK_PREFIX + "aas"
        game = {"id": gid, "user_color": "white", "moves": "e4 e5 Nf3", "opening": {}}
        state = {"n": 0}

        def fn(board):
            state["n"] += 1
            score = chess.engine.Cp(200) if state["n"] % 2 else chess.engine.Cp(-400)
            return score, pv_of(board, ["e5", "Nf6", "d4"])

        self.analyzer._engine = ScriptedEngine(fn)
        self.analyzer._pool = None
        for _ in range(2):
            blunders, summary = self.analyzer.analyze_and_store(game)
        self.assertEqual(len(blunders), 1)
        self.assertEqual(_count_blunders(gid), 1)
        self.assertEqual(summary["inserted"] + summary["updated"], 1)


class TestPerGameAtomicity(ScratchDbTestCase):
    """A failed write must not leave a half-written blunder set behind."""

    def setUp(self):
        self.analyzer = GameAnalyzer()

    def tearDown(self):
        self.analyzer.close()

    def test_a_failure_mid_batch_rolls_the_whole_batch_back(self):
        gid = FIXERK_PREFIX + "atomic"
        good = _blunder_row(gid, 1)
        broken = _blunder_row(gid, 3, played_move="g1f3")
        del broken["played_san"]          # malformed -> KeyError mid-batch
        with self.assertRaises(KeyError):
            self.analyzer.save_blunders_to_db([good, broken])
        self.assertEqual(_count_blunders(gid), 0)
        # ...and the game can be written again afterwards.
        self.analyzer.save_blunders_to_db([good])
        self.assertEqual(_count_blunders(gid), 1)

    def test_the_connection_is_closed_even_when_the_batch_fails(self):
        import backend.analyzer as analyzer_mod

        opened = []

        class RecordingConn:
            def __init__(self, conn):
                self._conn = conn
                self.closed = False

            def cursor(self):
                return self._conn.cursor()

            def commit(self):
                return self._conn.commit()

            def rollback(self):
                return self._conn.rollback()

            def close(self):
                self.closed = True
                return self._conn.close()

        def fake_get_db():
            # Short busy timeout: the pre-fix code never closes a connection on
            # its error path, so it keeps the WAL write lock and every later
            # write in this process fails.  With the 30 s production default this
            # regression test would take 30 s to fail instead of failing fast.
            raw = sqlite3.connect(db_mod.get_db_path(), timeout=0.5)
            raw.row_factory = sqlite3.Row
            raw.execute("PRAGMA journal_mode=WAL")
            conn = RecordingConn(raw)
            opened.append(conn)
            return conn

        analyzer_mod.get_db = fake_get_db
        try:
            gid = FIXERK_PREFIX + "close"
            broken = _blunder_row(gid, 1)
            del broken["cpl_loss"]
            with self.assertRaises(KeyError):
                self.analyzer.save_blunders_to_db([broken])
            self.analyzer.save_blunders_to_db([_blunder_row(gid, 1)])
        finally:
            analyzer_mod.get_db = get_db
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(c.closed for c in opened),
                        "a write path leaked a SQLite connection")


class TestMateColumnsAreForwardCompatible(ScratchDbTestCase):
    """The mate distance is persisted as soon as the schema has the columns."""

    def setUp(self):
        self.analyzer = GameAnalyzer()

    def tearDown(self):
        self.analyzer.close()

    def test_mate_columns_are_written_when_present_and_skipped_when_absent(self):
        gid = FIXERK_PREFIX + "cols"
        row = _blunder_row(gid, 1, eval_after=-MATE_SCORE_CP, eval_after_mate=-2,
                           eval_before_mate=None)
        # Current schema: no mate columns -> the write must still succeed.
        conn = get_db()
        try:
            present = {row[1] for row in conn.execute("PRAGMA table_info(blunders)")}
        finally:
            conn.close()
        self.assertNotIn("eval_after_mate", present)
        self.analyzer.save_blunders_to_db([row])
        stored = _rows(gid)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["eval_after"], -MATE_SCORE_CP)

        # Simulate FIXER-P0's migration and prove the analyzer fills them in.
        conn = get_db()
        try:
            conn.execute("ALTER TABLE blunders ADD COLUMN eval_before_mate INTEGER DEFAULT NULL")
            conn.execute("ALTER TABLE blunders ADD COLUMN eval_after_mate INTEGER DEFAULT NULL")
            conn.commit()
        finally:
            conn.close()

        self.analyzer.save_blunders_to_db([row])
        conn = get_db()
        try:
            got = conn.execute("SELECT eval_before_mate, eval_after_mate, eval_after "
                               "FROM blunders WHERE game_id = ?", (gid,)).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0]["eval_before_mate"])
        self.assertEqual(got[0]["eval_after_mate"], -2)
        self.assertEqual(got[0]["eval_after"], -MATE_SCORE_CP)


if __name__ == "__main__":
    unittest.main()
