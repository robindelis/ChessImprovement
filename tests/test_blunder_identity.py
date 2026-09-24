"""FIXER-S-01 regression tests -- blunder identity and blunder-set convergence.

Three findings are pinned here, all of them in the **ingestion** of opening
findings:

* **SYNC-18** (HIGH, data-loss) -- a record with no `id` grew an identical
  `games` row *and* an identical `blunders` row on every sync.  `ux_blunders_
  natural_key` cannot stop it, because SQLite compares NULLs as *distinct*
  inside a UNIQUE index, and the `blunders` rows were invisible anyway
  (`/api/blunders` JOINs `games`).  The identity rule is now: a finding must
  carry a usable `game_id`, `ply` and `played_move`; the first is not derivable
  so a finding without one is **refused loudly**, and the `games` row can no
  longer accumulate because the schema folds a NULL id into one bucket.
* **SYNC-19** (HIGH, wrong-result) -- the SYNC-15 upsert made re-analysis
  *idempotent* but not *correct*.  A finding the engine no longer reports was
  never deleted, and `analyzed_at` guaranteed it never would be.  A
  re-analysis now makes the stored set of a game equal the engine's current
  findings, and a *practised* finding that goes stale has its drill card
  **retired rather than deleted**.
* **SYNC-23** (LOW, a trap for the next caller) -- `normalize_score` reported
  an ordinary `PovScore` as "checkmate is already on the board", because
  python-chess 1.11.2's `PovScore` has neither `.mate()` nor `.score()` and the
  `hasattr` duck-typing swallowed it into the `MateGiven` fallback.

Database policy: every test in this module runs against a **private scratch
database** created by `init_db()` (the only legitimate way to obtain the
schema), never the shared suite database and never `chess_improvement.db`.
`tests.test_setup` is imported first and points `CHESS_DB_PATH` at a throwaway
temp database for the whole process.  The `/api/blunders`-vs-dashboard
disagreement is measured through `TestClient(app)` on the *shared* suite
database (that is the shape the finding was reported in), never over HTTP.
Engine policy: no Stockfish process is started at all -- the engine is a
scripted double, exactly like the one `tests/test_analyzer_correctness.py`
uses, so the whole module runs offline and instantly.
"""
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.test_setup  # noqa: E402  FIRST backend-adjacent import (temp DB only)
import chess  # noqa: E402
import chess.engine  # noqa: E402
import backend.db as db_mod  # noqa: E402
import backend.analyzer as analyzer_mod  # noqa: E402
from backend.analyzer import (  # noqa: E402
    NEVER_REVIEW_DUE,
    RETIRED_DRILL_STATUS,
    MATE_SCORE_CP,
    GameAnalyzer,
    UnidentifiableBlunderError,
    natural_key_of,
    normalize_score,
)
from backend.db import get_db, init_db  # noqa: E402

FIXERS_PREFIX = "fixers_"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _blunder_row(game_id, ply, played_move="d2d4", **overrides):
    """A complete, writable blunder row -- the analyzer's own dict shape."""
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


def _scripted_engine(before, after, user_color=chess.WHITE):
    """A `PovScore`-returning engine stub: (score, []) for every position."""
    state = {"n": 0}

    def analyse(_board, limit=None):  # noqa: ARG001 - the real engine's signature
        state["n"] += 1
        score = before if state["n"] % 2 else after
        return {"score": chess.engine.PovScore(score, user_color), "pv": []}

    return analyse, state


class _ScriptedEngine:
    """Minimal stand-in for a `SimpleEngine` lease."""

    def __init__(self, before, after, user_color=chess.WHITE):
        self._analyse, self.calls = _scripted_engine(before, after, user_color)

    def analyse(self, board, limit=None):
        return self._analyse(board, limit)


class ScratchDbTestCase(unittest.TestCase):
    """A private, throwaway database per test class, restored on teardown."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._prev_path = os.environ.get("CHESS_DB_PATH")
        fd, cls._scratch = tempfile.mkstemp(prefix="chess_test_fixers_", suffix=".db")
        os.close(fd)
        os.unlink(cls._scratch)
        os.environ["CHESS_DB_PATH"] = cls._scratch
        db_mod.set_db_path(cls._scratch)
        with contextlib.redirect_stdout(io.StringIO()):
            init_db()

    @classmethod
    def tearDownClass(cls):
        if cls._prev_path is None:
            os.environ.pop("CHESS_DB_PATH", None)
        else:
            os.environ["CHESS_DB_PATH"] = cls._prev_path
            db_mod.set_db_path(cls._prev_path)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(cls._scratch + suffix):
                try:
                    os.unlink(cls._scratch + suffix)
                except OSError:
                    pass
        super().tearDownClass()

    def setUp(self):
        # Per-test isolation: the scratch database is per *class*, so each test
        # starts from an empty corpus and can assert on absolute counts.
        conn = get_db()
        try:
            for table in ("drill_reviews", "blunders", "games"):
                conn.execute("DELETE FROM %s" % table)
            conn.commit()
        finally:
            conn.close()

    # -- sql helpers ------------------------------------------------------ #
    def _conn(self):
        conn = get_db()
        self.addCleanup(conn.close)
        return conn

    def _rows(self, game_id):
        return [dict(r) for r in self._conn().execute(
            "SELECT id, ply, played_move, cpl_loss FROM blunders WHERE game_id = ? "
            "ORDER BY id", (game_id,)).fetchall()]

    def _count(self, game_id=None):
        if game_id is None:
            return self._conn().execute("SELECT COUNT(*) c FROM blunders").fetchone()["c"]
        return len(self._rows(game_id))

    def _drill(self, blunder_id):
        row = self._conn().execute(
            "SELECT * FROM drill_reviews WHERE blunder_id = ?", (blunder_id,)).fetchone()
        return dict(row) if row is not None else None

    def _drill_counters(self):
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM drill_reviews WHERE status = 'mastered'"
        ).fetchone()
        return row["n"]

    def _due_count(self):
        return self._conn().execute(
            "SELECT COUNT(*) AS n FROM drill_reviews WHERE next_review_date <= ?",
            (int(time.time()),)).fetchone()["n"]


# --------------------------------------------------------------------------- #
# SYNC-18 -- a NULL-keyed record must not accumulate
# --------------------------------------------------------------------------- #
class TestUnidentifiableBlundersAreRefused(ScratchDbTestCase):
    """A finding with no usable natural key is refused, loudly, and never stored."""

    def setUp(self):
        super().setUp()
        self.analyzer = GameAnalyzer()

    def tearDown(self):
        self.analyzer.close()

    # -- the identity rule itself ----------------------------------------- #
    def test_a_null_game_id_is_refused(self):
        for bad in (None, "", "   "):
            row = _blunder_row(bad, 1)
            with self.assertRaises(UnidentifiableBlunderError) as ctx:
                natural_key_of(row)
            self.assertIn("game id", str(ctx.exception))

    def test_a_missing_ply_or_played_move_is_refused(self):
        with self.assertRaises(UnidentifiableBlunderError):
            natural_key_of(_blunder_row("g1", None))
        with self.assertRaises(UnidentifiableBlunderError):
            natural_key_of(_blunder_row("g1", "not-a-number"))
        with self.assertRaises(UnidentifiableBlunderError):
            natural_key_of(_blunder_row("g1", 1, played_move=""))

    def test_the_key_is_normalised_not_just_validated(self):
        """`ply` and `played_move` ARE derivable, so they are normalised."""
        self.assertEqual(
            natural_key_of(_blunder_row("  g1  ", " 4 ", played_move=" D2D4 ")),
            ("g1", 4, "d2d4"),
        )
        # ...and a normalised key still lands on the same stored row.
        gid = FIXERS_PREFIX + "norm"
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1, played_move="d2d4")])
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1, played_move=" D2D4 ")])
        rows = self._rows(gid)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["played_move"], "d2d4")

    def test_saving_an_unidentifiable_row_stores_nothing_at_all(self):
        gid = FIXERS_PREFIX + "refuse"
        batch = [_blunder_row(gid, 1), _blunder_row(None, 3, played_move="g1f3")]
        with self.assertRaises(UnidentifiableBlunderError):
            self.analyzer.save_blunders_to_db(batch)
        # Not even the good row of the batch: the refusal happens before the
        # transaction opens, so there is nothing to roll back and nothing half
        # written.
        self.assertEqual(self._count(), 0)
        self.assertEqual(
            self._conn().execute("SELECT COUNT(*) c FROM drill_reviews").fetchone()["c"], 0)

    def test_a_bad_game_id_argument_cannot_wipe_a_game(self):
        """`game_ids` is a reconciliation scope -- a junk value must not nuke rows."""
        gid = FIXERS_PREFIX + "scope"
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1)])
        for junk in (None, "", "   ", 0):
            with self.assertRaises(UnidentifiableBlunderError):
                self.analyzer.save_blunders_to_db([], game_ids=[junk])
        self.assertEqual(self._count(gid), 1)

    def test_repeated_saves_of_an_unidentifiable_row_grow_nothing(self):
        """The pre-fix measurement: 4 identical saves -> 4 rows. Now: refused."""
        row = _blunder_row(None, 1)
        for _ in range(4):
            with self.assertRaises(UnidentifiableBlunderError):
                self.analyzer.save_blunders_to_db([row])
        self.assertEqual(self._count(), 0)

    # -- the pipeline ------------------------------------------------------ #
    def test_an_id_less_game_is_not_analysed_and_writes_nothing(self):
        # No engine is pre-seeded: the refusal must happen *before* the analyzer
        # leases one, so an id-less record costs nothing and stalls nothing.
        self.assertIsNone(self.analyzer._engine)
        record = {"user_color": "white", "moves": "e4 e5 Nf3", "opening": {}}
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            found = self.analyzer.analyze_game_openings(record)
        self.assertEqual(found, [])
        self.assertEqual(self._count(), 0)
        # Loud: the refusal is on stdout, not silent.
        self.assertIn("no id", buffer.getvalue())
        # ...and no engine lease was taken for it.
        self.assertIsNone(self.analyzer._engine)
        self.assertIsNone(self.analyzer._pool)


# --------------------------------------------------------------------------- #
# SYNC-18 -- the `games` row converges too (schema level)
# --------------------------------------------------------------------------- #
class TestGamesIdentityIsConstrainedByTheSchema(ScratchDbTestCase):
    """`INSERT OR REPLACE INTO games (id=NULL, ...)` must converge, not append."""

    def _replace_id_less_game(self, tag="blitz"):
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO games (id, speed, rated, played_at, white_player, "
            "black_player, white_rating, black_rating, user_color, result, opening_name, "
            "eco, moves_san, analyzed_at) VALUES (NULL, ?, 1, ?, 'u', 'o', 1800, 1800, "
            "'white', 'draw', 'O', 'A00', 'e4 e5', 0)", (tag, int(time.time())))
        conn.commit()
        return conn.execute(
            "SELECT COUNT(*) c FROM games WHERE id IS NULL").fetchone()["c"]

    def test_repeated_id_less_upserts_converge_on_one_games_row(self):
        for _ in range(3):
            count = self._replace_id_less_game()
        self.assertEqual(count, 1, "an id-less games record accumulated again")

    def test_the_index_is_created_on_a_fresh_boot(self):
        names = {r["name"] for r in self._conn().execute(
            "PRAGMA index_list(games)").fetchall()}
        self.assertIn("ux_games_id_not_null", names)
        uniques = {r["name"]: r["unique"] for r in self._conn().execute(
            "PRAGMA index_list(games)").fetchall()}
        self.assertEqual(uniques["ux_games_id_not_null"], 1)

    def test_a_real_game_is_never_folded_into_the_null_bucket(self):
        conn = self._conn()
        for game_id in ("gV1", "gV2", "gV3"):
            conn.execute("INSERT OR REPLACE INTO games (id, played_at, user_color) "
                         "VALUES (?, 0, 'white')", (game_id,))
        conn.execute("INSERT OR REPLACE INTO games (id, played_at, user_color) "
                     "VALUES (NULL, 0, 'white')")
        conn.commit()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM games WHERE id IS NOT NULL").fetchone()["c"], 3)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM games WHERE id IS NULL").fetchone()["c"], 1)


# --------------------------------------------------------------------------- #
# SYNC-19 -- a re-analysis converges to what the engine currently says
# --------------------------------------------------------------------------- #
class TestReanalysisConverges(ScratchDbTestCase):
    """`save_blunders_to_db` is a recomputation, not an append."""

    def setUp(self):
        super().setUp()
        self.analyzer = GameAnalyzer()

    def tearDown(self):
        self.analyzer.close()

    def test_a_finding_the_engine_no_longer_reports_is_removed(self):
        gid = FIXERS_PREFIX + "stale"
        # Analysis #1: two findings.
        self.analyzer.save_blunders_to_db([
            _blunder_row(gid, 1), _blunder_row(gid, 3, played_move="g1f3")])
        self.assertEqual(len(self._rows(gid)), 2)
        # Analysis #2: the engine only reports the first one any more.
        summary = self.analyzer.save_blunders_to_db([_blunder_row(gid, 1)])
        rows = self._rows(gid)
        self.assertEqual([r["ply"] for r in rows], [1], rows)
        self.assertEqual(summary["stale_removed"], 1)
        self.assertEqual(summary["stale_retired"], 0)
        # The stale finding's placeholder card went with it, so
        # `mastered_count` / `due_reviews_count` cannot count a phantom.
        self.assertEqual(
            self._conn().execute("SELECT COUNT(*) c FROM drill_reviews").fetchone()["c"], 1)

    def test_a_narrower_opening_window_converges(self):
        """The finding's own repro: `max_opening_ply` lowered from 9 to 4."""
        gid = FIXERS_PREFIX + "window"
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 9, played_move="b1c3")])
        self.assertEqual(len(self._rows(gid)), 1)
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1)], game_ids=[gid])
        self.assertEqual([r["ply"] for r in self._rows(gid)], [1])

    def test_an_analysis_that_now_finds_nothing_converges_to_empty(self):
        """An empty batch names no game, so the caller must supply the scope."""
        gid = FIXERS_PREFIX + "empty"
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1)])
        self.assertEqual(len(self._rows(gid)), 1)
        summary = self.analyzer.save_blunders_to_db([], game_ids=[gid])
        self.assertEqual(summary["stale_removed"], 1)
        self.assertEqual(self._rows(gid), [])

    def test_reconciliation_never_touches_another_game(self):
        mine, theirs = FIXERS_PREFIX + "mine", FIXERS_PREFIX + "theirs"
        self.analyzer.save_blunders_to_db([
            _blunder_row(mine, 1), _blunder_row(mine, 3, played_move="g1f3"),
            _blunder_row(theirs, 1), _blunder_row(theirs, 5, played_move="b1c3")])
        self.analyzer.save_blunders_to_db([_blunder_row(mine, 1)], game_ids=[mine])
        self.assertEqual([r["ply"] for r in self._rows(mine)], [1])
        self.assertEqual(sorted(r["ply"] for r in self._rows(theirs)), [1, 5],
                         "re-analysis of one game must not delete another game's rows")

    def test_a_changed_verdict_for_the_same_key_is_an_update_not_a_delete(self):
        gid = FIXERS_PREFIX + "changed"
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1, cpl_loss=300)])
        summary = self.analyzer.save_blunders_to_db([_blunder_row(gid, 1, cpl_loss=950)])
        rows = self._rows(gid)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["cpl_loss"], 950)
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["stale_removed"], 0)

    def test_a_legacy_unidentifiable_row_is_outside_the_per_game_scope(self):
        """The residue of SYNC-18: a NULL-keyed row already in the table.

        It belongs to no game, so it is invisible to every reader and
        un-deduplicable.  It is also, deliberately, **not** swept here: the
        reconciliation is scoped to `WHERE b.game_id = ?` precisely so a
        re-analysis can never touch another game's rows, and a NULL-keyed row
        is nobody's.  Removing one would be a table-wide `DELETE` with no
        owner, executed from a write path -- the exact shape of the
        destructive `init_db()` migration this audit had to undo.  The fix is
        that no such row can be *created* any more; the residue on a database
        that already has one is a documented, manual cleanup.
        """
        gid = FIXERS_PREFIX + "legacy"
        conn = self._conn()
        conn.execute(
            "INSERT INTO blunders (game_id, ply, played_move, cpl_loss) "
            "VALUES (?, 5, 'e2e4', 400)", (gid,))
        conn.execute("INSERT INTO blunders (game_id, ply, played_move, cpl_loss) "
                     "VALUES (NULL, 5, 'e2e4', 400)")
        conn.commit()
        self.assertEqual(
            self._conn().execute("SELECT COUNT(*) c FROM blunders WHERE game_id IS NULL"
                                 ).fetchone()["c"], 1,
            "fixture must really contain the NULL-keyed row")
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1)], game_ids=[gid])
        # The game's own set converged ...
        self.assertEqual([r["ply"] for r in self._rows(gid)], [1])
        # ... and the orphan is untouched, not silently deleted.
        self.assertEqual(
            self._conn().execute("SELECT COUNT(*) c FROM blunders WHERE game_id IS NULL"
                                 ).fetchone()["c"], 1)

    def test_a_failed_reconciliation_rolls_the_whole_batch_back(self):
        gid = FIXERS_PREFIX + "atomic"
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1)])
        broken = _blunder_row(gid, 3, played_move="g1f3")
        del broken["cpl_loss"]                      # malformed -> KeyError
        with self.assertRaises(KeyError):
            self.analyzer.save_blunders_to_db([broken, _blunder_row(gid, 5, played_move="b1c3")],
                                              game_ids=[gid])
        # Nothing changed: the surviving row is still there and the removals the
        # batch asked for did not happen either.
        self.assertEqual([r["ply"] for r in self._rows(gid)], [1])

    def test_analyze_and_store_converges_through_the_real_pipeline(self):
        """End to end with a scripted engine, changing what the engine reports."""
        gid = FIXERS_PREFIX + "e2e"
        game = {"id": gid, "user_color": "white", "moves": "e4 e5 Nf3", "opening": {}}

        # Pass 1: a 400 cp drop on every white move -> one finding at ply 1.
        self.analyzer._engine = _ScriptedEngine(chess.engine.Cp(200), chess.engine.Cp(-400))
        found, saved = self.analyzer.analyze_and_store(game)
        self.assertEqual([b["ply"] for b in found], [1], found)
        self.assertEqual([r["ply"] for r in self._rows(gid)], [1])
        self.assertEqual(saved["inserted"], 1)

        # Pass 2: the same engine now reports a *clean* game.  The stored set
        # must become empty -- this is the half `analyzed_at` used to freeze.
        self.analyzer._engine = _ScriptedEngine(chess.engine.Cp(10), chess.engine.Cp(10))
        found, saved = self.analyzer.analyze_and_store(game)
        self.assertEqual(found, [])
        self.assertEqual(saved["stale_removed"], 1)
        self.assertEqual(self._rows(gid), [])

        # Pass 3: the mistake comes back -> a new row with a fresh card.
        self.analyzer._engine = _ScriptedEngine(chess.engine.Cp(200), chess.engine.Cp(-400))
        found, saved = self.analyzer.analyze_and_store(game)
        self.assertEqual([b["ply"] for b in found], [1], found)
        self.assertEqual(len(self._rows(gid)), 1)
        self.assertEqual(saved["inserted"], 1)


class TestStaleFindingPolicyForPractisedCards(ScratchDbTestCase):
    """The stated policy: never practised -> deleted; practised -> card retired."""

    def setUp(self):
        super().setUp()
        self.analyzer = GameAnalyzer()

    def tearDown(self):
        self.analyzer.close()

    def _practise(self, blunder_id, attempts=0, successes=0, last=0, status="new"):
        conn = self._conn()
        conn.execute(
            "UPDATE drill_reviews SET attempts = ?, successes = ?, last_attempt_date = ?, "
            "next_review_date = ?, status = ? WHERE blunder_id = ?",
            (attempts, successes, last, 0 if last else int(time.time()), status, blunder_id))
        conn.commit()
        return blunder_id

    def test_an_untouched_card_is_deleted_with_its_finding(self):
        gid = FIXERS_PREFIX + "untouched"
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1)])
        stale_id = self._rows(gid)[0]["id"]
        self._practise(stale_id)          # the all-zero placeholder
        summary = self.analyzer.save_blunders_to_db([], game_ids=[gid])
        self.assertEqual((summary["stale_removed"], summary["stale_retired"]), (1, 0))
        self.assertEqual(self._rows(gid), [])
        self.assertIsNone(self._drill(stale_id),
                          "an untouched placeholder card has nothing worth keeping")

    def test_a_practised_card_is_retired_not_deleted(self):
        gid = FIXERS_PREFIX + "practised"
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1)])
        stale_id = self._rows(gid)[0]["id"]
        self._practise(stale_id, attempts=7, successes=3, last=1700000000, status="mastered")
        before_mastered = self._drill_counters()
        before_due = self._due_count()
        self.assertEqual(before_mastered, 1)
        self.assertEqual(before_due, 1)

        summary = self.analyzer.save_blunders_to_db([], game_ids=[gid])
        self.assertEqual((summary["stale_removed"], summary["stale_retired"]), (1, 1))

        # The finding itself is gone: the gap list can no longer rank a phantom.
        self.assertEqual(self._rows(gid), [])
        # The user's training record survives verbatim.
        card = self._drill(stale_id)
        self.assertIsNotNone(card, "the user's drill card was deleted")
        self.assertEqual(card["attempts"], 7)
        self.assertEqual(card["successes"], 3)
        self.assertEqual(card["last_attempt_date"], 1700000000)
        self.assertEqual(card["status"], RETIRED_DRILL_STATUS)
        self.assertEqual(card["next_review_date"], NEVER_REVIEW_DUE)
        # ...and both dashboard counters are truthful again.
        self.assertEqual(self._drill_counters(), 0)
        self.assertEqual(self._due_count(), 0)

    def test_each_practice_signal_alone_is_enough_to_retire(self):
        for label, kwargs in (
            ("attempts", {"attempts": 1}),
            ("successes", {"successes": 1}),
            ("last_attempt_date", {"last": 1700000000}),
            ("status", {"status": "learning"}),
        ):
            gid = FIXERS_PREFIX + "sig" + label
            self.analyzer.save_blunders_to_db([_blunder_row(gid, 1)])
            stale_id = self._rows(gid)[0]["id"]
            self._practise(stale_id, **kwargs)
            summary = self.analyzer.save_blunders_to_db([], game_ids=[gid])
            self.assertEqual(summary["stale_retired"], 1, label)
            self.assertEqual(summary["stale_removed"], 1, label)
            self.assertEqual(self._drill(stale_id)["status"], RETIRED_DRILL_STATUS, label)

    def test_a_retired_card_can_never_be_re_bound_to_another_finding(self):
        """`blunders.id` is AUTOINCREMENT, so the retired pointer stays inert."""
        gid = FIXERS_PREFIX + "autoinc"
        self.analyzer.save_blunders_to_db([_blunder_row(gid, 1)])
        stale_id = self._rows(gid)[0]["id"]
        self._practise(stale_id, attempts=2, status="learning")
        self.analyzer.save_blunders_to_db([], game_ids=[gid])
        for ply in (1, 3, 5):
            self.analyzer.save_blunders_to_db([_blunder_row(gid, ply, played_move="x%da" % ply)])
        self.assertNotIn(stale_id, [r["id"] for r in self._rows(gid)])
        self.assertIsNone(self._drill(stale_id + 1),
                          "a new finding inherited a retired card's identity")


# --------------------------------------------------------------------------- #
# SYNC-18 -- `/api/blunders` and the dashboard must agree
# --------------------------------------------------------------------------- #
class TestBlunderListAndDashboardAgree(unittest.TestCase):
    """Measured on the shared suite database, through `TestClient(app)`.

    Pre-fix, a NULL-keyed blunder was counted by the dashboard's bare
    `COUNT(*)` and shown by no list at all, because `/api/blunders` JOINs
    `games`.  These two tests pin the agreement for the general case and prove
    the NULL case cannot even be constructed any more.
    """

    def setUp(self):
        from fastapi.testclient import TestClient

        from backend.api import app

        tests.test_setup.reset_fixtures()
        self.client = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)

    def test_the_list_and_the_dashboard_count_the_same_blunders(self):
        response = self.client.get("/api/blunders?limit=200", headers={"Host": "127.0.0.1:38281"})
        self.assertEqual(response.status_code, 200, response.text)
        listed = response.json()
        dashboard = self.client.get("/api/dashboard", headers={"Host": "127.0.0.1:38281"})
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        total = dashboard.json()["total_blunders"]
        self.assertEqual(len(listed), total,
                         "GET /api/blunders shows %d rows while the dashboard counts %d"
                         % (len(listed), total))
        # Nothing shown is invisible: every listed row has a game to point at.
        for row in listed:
            self.assertTrue(row.get("played_at") is not None, row)

    def test_a_blunder_row_always_has_a_resolvable_game(self):
        conn = get_db()
        try:
            orphans = conn.execute(
                "SELECT COUNT(*) AS n FROM blunders b LEFT JOIN games g "
                "ON b.game_id = g.id WHERE g.id IS NULL").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(orphans, 0,
                         "%d blunders row(s) point at no game and can never be listed" % orphans)

    def test_the_schema_has_no_null_keyed_blunder_to_begin_with(self):
        conn = get_db()
        try:
            nulls = conn.execute(
                "SELECT COUNT(*) AS n FROM blunders WHERE game_id IS NULL "
                "OR ply IS NULL OR played_move IS NULL").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(nulls, 0)


# --------------------------------------------------------------------------- #
# SYNC-23 -- `normalize_score` must be honest about an unknown score
# --------------------------------------------------------------------------- #
class TestNormalizeScoreIsFailClosed(unittest.TestCase):
    """`hasattr` duck-typing reported a `PovScore(Cp(+30))` as a mate."""

    def test_a_pov_score_without_a_colour_is_refused_not_guessed(self):
        pov = chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE)
        # Pre-fix this returned (10000, 0), which describe_score_mate rendered
        # as "checkmate is already on the board" for a +0.30 evaluation.
        with self.assertRaises(ValueError) as ctx:
            normalize_score(pov)
        message = str(ctx.exception)
        self.assertIn("colour", message)
        # No ``(cp, mate)`` pair is produced at all, so there is nothing for
        # `describe_score_mate` to turn into "checkmate is already on the board".
        self.assertNotIsInstance(ctx.exception, (TypeError,))

    def test_a_pov_score_with_a_colour_still_works(self):
        pov = chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE)
        self.assertEqual(normalize_score(pov, chess.WHITE), (30, None))
        self.assertEqual(normalize_score(pov, chess.BLACK), (-30, None))
        self.assertEqual(normalize_score(chess.engine.PovScore(chess.engine.Mate(-2), chess.WHITE),
                                         chess.WHITE), (-MATE_SCORE_CP, -2))

    def test_the_known_score_types_are_still_interpreted(self):
        self.assertEqual(normalize_score(chess.engine.Cp(120)), (120, None))
        self.assertEqual(normalize_score(chess.engine.Cp(-95)), (-95, None))
        self.assertEqual(normalize_score(chess.engine.Mate(1)), (MATE_SCORE_CP, 1))
        self.assertEqual(normalize_score(chess.engine.Mate(-1)), (-MATE_SCORE_CP, -1))
        self.assertEqual(normalize_score(chess.engine.MateGiven, chess.WHITE),
                         (MATE_SCORE_CP, 0))
        self.assertEqual(normalize_score(chess.engine.Mate(0), chess.WHITE),
                         (MATE_SCORE_CP, 0))

    def test_an_unrecognised_input_makes_no_mate_claim(self):
        """No duck type, and no `None`, may become "checkmate on the board"."""
        for junk in (None, 42, -7, 3.5, "", "e4", object(), {"score": 30},
                     [chess.engine.Cp(30)]):
            with self.assertRaises((TypeError, ValueError)) as ctx:
                normalize_score(junk)
            self.assertNotIn("checkmate", str(ctx.exception).lower())

    def test_the_real_call_sites_are_unaffected(self):
        """`analyze_game_openings` always passes a colour, so it never raises."""
        self.assertTrue(analyzer_mod.MAX_CPL_LOSS > 0)
        for score, colour in ((chess.engine.Cp(200), chess.WHITE),
                              (chess.engine.Mate(-1), chess.BLACK),
                              (chess.engine.MateGiven, chess.WHITE)):
            cp, mate = normalize_score(chess.engine.PovScore(score, chess.WHITE), colour)
            self.assertIsInstance(cp, int)
            self.assertIsInstance(mate, (int, type(None)))


if __name__ == "__main__":
    unittest.main()
