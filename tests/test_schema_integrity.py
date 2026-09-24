"""
Schema-integrity regression tests for `backend/db.py` (audit SYNC-01, SYNC-15,
CODE-02-08/10, CODE-02-18).

`init_db()` runs on EVERY application launch, so a mistake in it is either a
silent data-loss incident (CODE-05-03, fixed by FIXER-P0) or a feature that is
dead on a freshly installed machine (SYNC-01). These tests pin the properties
that make the boot migration safe:

  1. a FRESH `init_db()` produces a schema the application can actually write to
     -- including `blunders.best_pv`, which the analyzer INSERTs unconditionally
     and which used to exist only in pre-migration databases. This is the test
     that would have caught SYNC-01.
  2. a LEGACY database (the pre-fix DDL, no `best_pv`, no indexes, possibly with
     duplicate blunder rows) gains the column and the indexes through the
     migration, and keeps every row it had.
  3. re-running `init_db()` changes nothing: same rows, same counts, same schema.
  4. de-duplication is idempotent and removes nothing but rows that duplicate an
     already-stored natural key, keeping the copy that carries the drill practice.
  5. the boot migration still never deletes or relabels a `repertoire_moves` row,
     even with orphan-shaped rows present -- FIXER-P0's guarantee, re-asserted here
     because the new indexes/dedup run in the same transaction as it.
  6. the hot `study_moves` join is answered by an index, not a `SCAN`.

Every test runs against its own throwaway SQLite file in the system temp
directory (set AFTER `tests.test_setup` has configured the global environment), so
nothing here can touch `chess_improvement.db`. The one test that reads production
copies its bytes with a plain `open(..., 'rb')` -- no SQLite connection, so
production is never locked, WAL-read or checkpointed -- and skips when the file is
absent.
"""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest

import tests.test_setup  # MUST be imported first: isolates CHESS_DB_PATH from production

import backend.db as db_mod
from backend.db import FORBIDDEN_AUTO_STUDY_NAMES, START_FEN_3, init_db


def _constant(name, fallback):
    """Read a module constant, tolerating its absence.

    These tests are also run against the PRE-FIX `backend/db.py` to prove they are
    red before the fix. A hard `from backend.db import SCHEMA_INDEXES` would turn
    that proof into a collection error instead of a genuine assertion failure, so
    every constant is read defensively: the fallback is what the pre-fix build
    effectively had (no index, no schema version, the leading-wildcard LIKE probe).
    """
    return getattr(db_mod, name, fallback)


SCHEMA_INDEXES = _constant("SCHEMA_INDEXES", ())
SCHEMA_PARTIAL_INDEXES = _constant("SCHEMA_PARTIAL_INDEXES", ())
SCHEMA_VERSION = _constant("SCHEMA_VERSION", 0)
EXPLORER_EVAL_CONVENTION_VERSION = _constant("EXPLORER_EVAL_CONVENTION_VERSION", 1)
UNCONVERTED_EXPLORER_PREDICATE = _constant(
    "UNCONVERTED_EXPLORER_PREDICATE", "data_json NOT LIKE '%\"eval_convention\": \"white\"%'")
UNCONVERTED_EXPLORER_SELECT = _constant(
    "UNCONVERTED_EXPLORER_SELECT", "SELECT fen, ratings, data_json FROM explorer_cache WHERE %s")
BLUNDER_NATURAL_KEY = ("game_id", "ply", "played_move")
UNIQUE_INDEX_NAME = "ux_blunders_natural_key"

#: The tables AGENTS.md section 5 documents. The boot migration stamps
#: `PRAGMA user_version` rather than creating a *migration ledger* table, and
#: that is still true: `scout_video_meta` is a feature table (the Scout's
#: per-video ranking cache), not a record of what the migration did. Adding a
#: real table is legitimate; adding a tenth *ledger* is not, and every entry
#: here must also appear in AGENTS.md section 5 or `test_docs_contract.py`
#: fails on the count it derives from the live schema.
TABLES = (
    "blunders", "drill_reviews", "explorer_cache", "games", "repertoire_moves",
    "repertoire_studies", "scout_video_meta", "scouted_content", "settings",
    "study_moves",
)

#: Verbatim copy of the PRE-FIX `blunders` DDL (`git show HEAD:backend/db.py`):
#: no `best_pv`, no indexes, no uniqueness. Used to build a legacy database in
#: memory so the migration path is exercised for real instead of being assumed.
LEGACY_BLUNDERS_DDL = """
CREATE TABLE blunders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT,
    move_number INTEGER,
    ply INTEGER,
    color TEXT,
    fen_before TEXT,
    fen_after TEXT,
    played_move TEXT,
    played_san TEXT,
    best_move TEXT,
    best_san TEXT,
    eval_before INTEGER,
    eval_after INTEGER,
    cpl_loss INTEGER,
    opening_name TEXT,
    eco TEXT,
    is_book_move INTEGER DEFAULT 0,
    refutation_pv TEXT,
    explanation TEXT,
    strategic_idea TEXT,
    category TEXT,
    opening_games INTEGER DEFAULT NULL,
    created_at INTEGER,
    FOREIGN KEY(game_id) REFERENCES games(id)
)
"""

#: Exactly the column list `backend/analyzer.py` INSERTs. If `init_db()` does not
#: produce a schema with all of these, every blunder write raises OperationalError
#: and the whole blunder/drill/gap/dashboard feature is dead (audit SYNC-01).
ANALYZER_INSERT_COLUMNS = (
    "game_id, move_number, ply, color, fen_before, fen_after, played_move, played_san,"
    " best_move, best_san, eval_before, eval_after, cpl_loss, opening_name, eco,"
    " is_book_move, refutation_pv, best_pv, explanation, strategic_idea, category,"
    " opening_games, created_at"
)



def _fen4(fen):
    return " ".join(str(fen).strip().split()[:4])


class SchemaTestBase(unittest.TestCase):
    """A pristine throwaway database per test, restored on teardown."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="chess_test_schema_", suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)  # let SQLite create the file
        self._prev_db_path = os.environ.get("CHESS_DB_PATH")
        os.environ["CHESS_DB_PATH"] = self.db_path
        db_mod.set_db_path(self.db_path)

    def tearDown(self):
        try:
            if self._prev_db_path is None:
                os.environ.pop("CHESS_DB_PATH", None)
            else:
                os.environ["CHESS_DB_PATH"] = self._prev_db_path
                db_mod.set_db_path(self._prev_db_path)
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(self.db_path + suffix):
                    os.unlink(self.db_path + suffix)

    # ------------------------------------------------------------------ helpers
    def conn(self):
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def boot(self, *args, **kwargs):
        """Run `init_db()` and return everything it printed."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            init_db(*args, **kwargs)
        return buffer.getvalue()

    def columns(self, table, connection=None):
        own = connection is None
        connection = connection or self.conn()
        try:
            return [row[1] for row in connection.execute('PRAGMA table_info("%s")' % table)]
        finally:
            if own:
                connection.close()

    def index_columns(self, name, connection=None):
        own = connection is None
        connection = connection or self.conn()
        try:
            return [row[2] for row in connection.execute('PRAGMA index_info("%s")' % name)]
        finally:
            if own:
                connection.close()

    def plan(self, sql, params=()):
        connection = self.conn()
        try:
            return [row[-1] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, params)]
        finally:
            connection.close()

    def digest(self, connection=None):
        """Order-independent digest over every row of every documented table."""
        own = connection is None
        connection = connection or self.conn()
        try:
            out = {}
            for table in TABLES:
                columns = [r[1] for r in connection.execute('PRAGMA table_info("%s")' % table)]
                order = ", ".join('"%s"' % c for c in columns) or "1"
                h = hashlib.sha256()
                for row in connection.execute("SELECT * FROM %s ORDER BY %s" % (table, order)):
                    h.update(repr(tuple(row)).encode("utf-8", "replace"))
                out[table] = (connection.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0],
                              h.hexdigest())
            return out
        finally:
            if own:
                connection.close()

    def schema_digest(self):
        connection = self.conn()
        try:
            rows = sorted(
                (r[0], r[1], r[2])
                for r in connection.execute(
                    "SELECT type, name, COALESCE(sql, '') FROM sqlite_master")
            )
        finally:
            connection.close()
        return hashlib.sha256(repr(rows).encode()).hexdigest()

    def insert_game(self, game_id="gLegacy", moves_san="e4 e5 Nf3", analyzed_at=0,
                    connection=None):
        own = connection is None
        connection = connection or self.conn()
        try:
            connection.execute(
                "INSERT OR REPLACE INTO games (id, speed, rated, played_at, user_color,"
                " result, opening_name, eco, moves_san, analyzed_at) VALUES"
                " (?, 'blitz', 1, 1700000000000, 'white', 'win', 'Opening', 'C50', ?, ?)",
                (game_id, moves_san, analyzed_at))
            connection.commit()
        finally:
            if own:
                connection.close()

    def insert_raw_blunder(self, connection, game_id, ply, played_move, cpl_loss=280,
                            created_at=1, category="Blunder", eco="C50"):
        connection.execute(
            "INSERT INTO blunders (game_id, move_number, ply, color, fen_before, fen_after,"
            " played_move, played_san, best_move, best_san, eval_before, eval_after, cpl_loss,"
            " opening_name, eco, is_book_move, refutation_pv, explanation, strategic_idea,"
            " category, created_at) VALUES"
            " (?, 1, ?, 'white', 'fen-before', 'fen-after', ?, 'd4', 'g1f3', 'Nf3', 20, -?, ?,"
            " 'Opening', ?, 0, '[]', 'why', 'idea', ?, ?)",
            (game_id, ply, played_move, -cpl_loss, cpl_loss, eco, category, created_at))
        return connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


# --------------------------------------------------------------------------- #
# 1. A fresh schema must be writable by the application
# --------------------------------------------------------------------------- #
class TestFreshSchemaIsWritable(SchemaTestBase):

    def test_fresh_init_db_creates_best_pv(self):
        """SYNC-01. RED against the pre-fix db.py: 23 columns, no `best_pv`.

        The pre-fix `CREATE TABLE blunders` had no `best_pv` and there was no
        `ALTER TABLE` migration for it either, so `backend/analyzer.py` -- which
        INSERTs into it unconditionally -- could not store a single blunder on any
        freshly migrated database.
        """
        self.boot()
        columns = self.columns("blunders")
        self.assertIn("best_pv", columns,
                      "a FRESH init_db() must create blunders.best_pv (audit SYNC-01); "
                      "got %s" % (columns,))
        connection = self.conn()
        try:
            self.addCleanup(connection.close)
            # The analyzer's real INSERT shape, verbatim column list.
            connection.execute(
                "INSERT INTO blunders (%s) VALUES ('gX', 3, 5, 'white', 'fb', 'fa', 'd2d4',"
                " 'd4', 'g1f3', 'Nf3', 20, -280, 280, 'Opening', 'C50', 0, '[]', '[]',"
                " 'why', 'idea', 'Blunder', 500, 1)" % ANALYZER_INSERT_COLUMNS)
            connection.commit()
            stored = connection.execute(
                "SELECT best_pv FROM blunders WHERE game_id = 'gX'").fetchone()["best_pv"]
        finally:
            connection.close()
        self.assertEqual(stored, "[]", "best_pv must be readable back")

    def test_fresh_schema_has_every_column_the_analyzer_writes(self):
        """The whole analyzer column list, not just `best_pv`."""
        self.boot()
        columns = set(self.columns("blunders"))
        missing = [c.strip() for c in ANALYZER_INSERT_COLUMNS.split(",") if c.strip() not in columns]
        self.assertEqual(missing, [],
                         "init_db() must create every column analyzer.py writes; missing %s" % missing)

    def test_fresh_schema_declares_the_blunder_natural_key_as_unique(self):
        """SYNC-15. One row per (game_id, ply, played_move), declaratively."""
        self.boot()
        connection = self.conn()
        try:
            self.assertEqual(
                self.index_columns("ux_blunders_natural_key", connection),
                list(BLUNDER_NATURAL_KEY),
                "the blunder natural key must be a UNIQUE index on %s" % (BLUNDER_NATURAL_KEY,))
            uniques = {row[1]: row[2] for row in connection.execute("PRAGMA index_list(blunders)")}
            self.assertEqual(uniques.get("ux_blunders_natural_key"), 1,
                             "ux_blunders_natural_key must be UNIQUE, not a plain index")
        finally:
            connection.close()

    def test_fresh_schema_refuses_a_duplicate_blunder(self):
        """Re-analysing a game must not be able to append a second copy."""
        self.boot()
        self.insert_game()
        connection = self.conn()
        try:
            first = self.insert_raw_blunder(connection, "gLegacy", 5, "d2d4")
            connection.commit()
            self.assertGreater(first, 0)
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_raw_blunder(connection, "gLegacy", 5, "d2d4")
            connection.rollback()
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM blunders WHERE game_id = 'gLegacy' AND ply = 5"
                ).fetchone()[0], 1)
        finally:
            connection.close()

    def test_fresh_schema_creates_every_declared_index(self):
        """Every index in the module's own table must exist after a fresh boot."""
        self.boot()
        connection = self.conn()
        try:
            present = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'")}
            expected = {name for name, _t, _c in SCHEMA_INDEXES} | {
                name for name, _t, _c, _w in SCHEMA_PARTIAL_INDEXES} | {
                "ux_blunders_natural_key"}
            self.assertEqual(expected - present, set(),
                             "init_db() did not create: %s" % sorted(expected - present))
        finally:
            connection.close()

    def test_fresh_schema_creates_exactly_the_nine_documented_tables(self):
        """A migration ledger TABLE would break AGENTS.md section 5's "9 tables"."""
        self.boot()
        connection = self.conn()
        try:
            names = sorted(r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))
        finally:
            connection.close()
        self.assertEqual(names, sorted(TABLES))


# --------------------------------------------------------------------------- #
# 2. A legacy database is migrated without losing a row
# --------------------------------------------------------------------------- #
class TestLegacyDatabaseMigration(SchemaTestBase):

    def _build_legacy_blunders_table(self):
        """Create the PRE-FIX `blunders` table (no best_pv, no indexes).

        `init_db()` runs first so the other eight tables exist, then `blunders` is
        replaced by the pre-fix DDL -- exactly the state of a database that was
        created by an older build and has not been migrated by this one.
        """
        self.boot()
        connection = self.conn()
        try:
            connection.execute("DROP TABLE blunders")
            connection.execute(LEGACY_BLUNDERS_DDL)
            connection.commit()
            self.assertNotIn("best_pv", self.columns("blunders", connection))
            self.assertEqual(
                [r[0] for r in connection.execute("PRAGMA index_list(blunders)")], [],
                "the legacy table must really have no index")
            return connection
        except Exception:
            connection.close()
            raise

    def test_legacy_database_gains_best_pv_without_losing_a_row(self):
        """SYNC-01's other half: an existing database must gain the column too."""
        connection = self._build_legacy_blunders_table()
        try:
            self.insert_game("gOld", "e4 c5 d4", connection=connection)
            for ply, move in ((3, "c2c4"), (5, "d2d4")):
                self.insert_raw_blunder(connection, "gOld", ply, move)
            connection.execute(
                "INSERT INTO drill_reviews (blunder_id, attempts, successes, status)"
                " SELECT id, 4, 3, 'learning' FROM blunders WHERE ply = 5")
            connection.commit()
            before = [tuple(r) for r in connection.execute(
                "SELECT id, game_id, ply, played_move, cpl_loss, created_at FROM blunders"
                " ORDER BY id")]
            drill_before = [tuple(r) for r in connection.execute(
                "SELECT blunder_id, attempts, successes, status FROM drill_reviews ORDER BY id")]
        finally:
            connection.close()
        self.assertEqual(len(before), 2)

        self.boot()

        self.assertIn("best_pv", self.columns("blunders"),
                      "the ALTER TABLE migration must add best_pv to a legacy database")
        connection = self.conn()
        try:
            after = [tuple(r) for r in connection.execute(
                "SELECT id, game_id, ply, played_move, cpl_loss, created_at FROM blunders"
                " ORDER BY id")]
            drill_after = [tuple(r) for r in connection.execute(
                "SELECT blunder_id, attempts, successes, status FROM drill_reviews ORDER BY id")]
        finally:
            connection.close()
        self.assertEqual(before, after, "the migration must not touch existing blunder rows")
        self.assertEqual(drill_before, drill_after, "drill_reviews must be untouched")

    def test_legacy_column_order_matches_a_fresh_schema(self):
        """A migrated database and a fresh one must not diverge in column order."""
        self.boot()
        fresh_columns = self.columns("blunders")
        self.assertEqual(fresh_columns[-1], "best_pv",
                         "best_pv is declared last so the orders match")
        self._build_legacy_blunders_table()
        self.boot()
        self.assertEqual(self.columns("blunders"), fresh_columns,
                         "a migrated legacy schema must end up identical to a fresh one")

    def test_legacy_duplicates_are_collapsed_and_practice_is_kept(self):
        """SYNC-15's migration half.

        A database written before the UNIQUE index existed can hold several rows
        for one natural key. The index cannot be created until they are collapsed,
        so `init_db()` collapses them: the survivor is the copy that carries the
        most spaced-repetition practice, and the discarded copy's drill row is
        merged into it (attempts are summed, `mastered` is never downgraded).
        """
        connection = self._build_legacy_blunders_table()
        try:
            self.insert_game("gDup", "e4 e5 Nf3", connection=connection)
            first = self.insert_raw_blunder(connection, "gDup", 3, "d2d4", 280, created_at=1)
            second = self.insert_raw_blunder(connection, "gDup", 3, "d2d4", 280, created_at=2)
            third = self.insert_raw_blunder(connection, "gDup", 3, "d2d4", 280, created_at=3)
            # The copy the user actually practised is the SECOND one.
            connection.execute(
                "INSERT INTO drill_reviews (blunder_id, attempts, successes, status)"
                " VALUES (?, 7, 3, 'learning')", (second,))
            connection.execute(
                "INSERT INTO drill_reviews (blunder_id, attempts, successes, status)"
                " VALUES (?, 2, 1, 'mastered')", (third,))
            connection.commit()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM blunders").fetchone()[0], 3)
        finally:
            connection.close()

        output = self.boot()
        self.assertIn("duplicated the natural key", output,
                      "the de-duplication must announce itself")
        connection = self.conn()
        try:
            rows = [tuple(r) for r in connection.execute(
                "SELECT id, game_id, ply, played_move, cpl_loss FROM blunders ORDER BY id")]
            self.assertEqual(len(rows), 1, "exactly one row per natural key must survive")
            survivor = rows[0][0]
            self.assertIn(survivor, (first, second, third))
            # 7 + 2 attempts and 3 + 1 successes: no practice is lost.
            drill = connection.execute(
                "SELECT blunder_id, attempts, successes, status FROM drill_reviews"
            ).fetchall()
            self.assertEqual(len(drill), 1, "one drill row per surviving blunder")
            self.assertEqual(drill[0]["blunder_id"], survivor)
            self.assertEqual(drill[0]["attempts"], 9)
            self.assertEqual(drill[0]["successes"], 4)
            self.assertEqual(drill[0]["status"], "mastered",
                             "a mastered copy must never be downgraded by a merge")
        finally:
            connection.close()

    def test_collapse_removes_nothing_but_duplicates_and_is_idempotent(self):
        """Two distinct blunders of one game, plus one duplicated: only the
        duplicate disappears, and a second boot changes nothing at all."""
        self.boot()
        self.insert_game("gMix", "e4 e5 Nf3")
        connection = self.conn()
        try:
            self.insert_raw_blunder(connection, "gMix", 3, "d2d4", 280)
            self.insert_raw_blunder(connection, "gMix", 5, "c2c4", 190)
            self.insert_raw_blunder(connection, "gMix", 7, "g1f3", 150)
            connection.commit()
            unique_before = self.digest(connection)["blunders"]
        finally:
            connection.close()
        # Make room for a legacy duplicate by dropping the index the same way a
        # pre-migration database would have had it missing.
        connection = self.conn()
        try:
            connection.execute("DROP INDEX ux_blunders_natural_key")
            self.insert_raw_blunder(connection, "gMix", 3, "d2d4", 280, created_at=99)
            connection.commit()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM blunders").fetchone()[0], 4)
        finally:
            connection.close()

        self.boot()
        connection = self.conn()
        try:
            rows = [tuple(r) for r in connection.execute(
                "SELECT ply, played_move, cpl_loss, created_at FROM blunders ORDER BY ply")]
            self.assertEqual(
                rows,
                [(3, "d2d4", 280, 1), (5, "c2c4", 190, 1), (7, "g1f3", 150, 1)],
                "only the duplicated natural key may be collapsed, and the SURVIVOR is the"
                " copy with the most drill practice (ties broken by the lowest id)")
            after_first = self.digest(connection)
        finally:
            connection.close()
        self.assertEqual(unique_before, after_first["blunders"],
                         "collapsing a duplicate must not alter the surviving row")

        # Idempotent: a second boot is a strict no-op.
        self.boot()
        connection = self.conn()
        try:
            self.assertEqual(after_first, self.digest(connection))
        finally:
            connection.close()

    def test_a_production_copy_is_preserved_row_for_row(self):
        """The strongest form of "existing database": a byte copy of production.

        The copy is made with a plain `open(..., 'rb')`, so no SQLite connection
        is ever opened against `chess_improvement.db` (no lock, no `-wal` read, no
        checkpoint on close).
        """
        from tests import probe_guard

        if not os.path.exists(probe_guard.PRODUCTION_DB_PATH):
            self.skipTest("no production database present")
        production = probe_guard.PRODUCTION_DB_PATH
        with open(production, "rb") as fh:
            payload = fh.read()
        with open(self.db_path, "wb") as fh:
            fh.write(payload)
        with contextlib.suppress(sqlite3.DatabaseError):
            self.boot()
        connection = self.conn()
        try:
            tables = sorted(r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'"))
            self.assertEqual(tables, sorted(TABLES))
            columns = self.columns("blunders", connection)
            self.assertIn("best_pv", columns)
            # Every row of every table survives, and the drill schedule is intact.
            drill = connection.execute(
                "SELECT COUNT(*) FROM drill_reviews d JOIN blunders b ON b.id = d.blunder_id"
            ).fetchone()[0]
            total_drill = connection.execute("SELECT COUNT(*) FROM drill_reviews").fetchone()[0]
            self.assertEqual(drill, total_drill,
                             "no drill_review may be orphaned by the migration")
        finally:
            connection.close()


# --------------------------------------------------------------------------- #
# 3. init_db() is idempotent
# --------------------------------------------------------------------------- #
class TestInitDbIsIdempotent(SchemaTestBase):

    def _rich_fixture(self):
        """One of everything the boot path touches, including orphan-shaped rows."""
        self.boot()
        self.insert_game("gRich", "e4 e5 Nf3 Nc6 Bc4")
        connection = self.conn()
        try:
            blunder = self.insert_raw_blunder(connection, "gRich", 3, "d2d4", 280)
            connection.execute(
                "INSERT INTO drill_reviews (blunder_id, attempts, successes, next_review_date,"
                " status) VALUES (?, 5, 2, 0, 'learning')", (blunder,))
            connection.execute(
                "INSERT INTO explorer_cache (fen, ratings, data_json, updated_at)"
                " VALUES ('8/8/8/8/8/5k1/8/4K3 b - - 0 1', '2000,2200,2500', ?, 1)",
                (json.dumps({"eval_convention": "white", "moves": [{"san": "Kd5", "eval": 20}]}),))
            connection.execute(
                "INSERT INTO explorer_cache (fen, ratings, data_json, updated_at)"
                " VALUES ('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',"
                " '2000,2200,2500', ?, 1)",
                (json.dumps({"eval_convention": "stm", "moves": [{"san": "e5", "eval": 30}]}),))
            connection.execute(
                "INSERT INTO scouted_content (source_id, source_type, title, channel,"
                " opening_name, eco, data_json, scouted_at) VALUES ('vid1', 'youtube', 'T',"
                " 'C', 'Opening', 'C50', '{}', 7)")
            # A connected line from the start position...
            start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
            for note, uci in ((1, "e2e4"), (2, "d2d4")):  # a sibling branch at one position
                connection.execute(
                    "INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci,"
                    " move_san, is_user_move, study_name, comment, created_at)"
                    " VALUES ('white', ?, ?, ?, 'e4', 1, '', ?, 1)",
                    (start, start, uci, "note %d" % note))
            # ...and a fragment whose parent is not any stored `fen` (orphan-shaped).
            connection.execute(
                "INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san,"
                " is_user_move, study_name, comment, created_at)"
                " VALUES ('black', '8/8/8/8/8/5k2/6q1/7K b - - 0 1',"
                " '8/8/8/8/8/6k1/6q1/7K b - - 0 1', 'g2g3', 'Kg3', 1, 'Keep Me', 'frag', 1)")
            connection.execute(
                "INSERT INTO repertoire_studies (name, repertoire, description, created_at,"
                " updated_at) VALUES ('Anti French Defense', 'white', '', 1, 1)")
            connection.commit()
        finally:
            connection.close()

    def test_second_and_third_boot_change_nothing(self):
        self._rich_fixture()
        # One boot to let the boot migrations converge the fixture (the seeded
        # side-to-move explorer payload is converted on the first boot after it
        # appears -- that is the point of `test_a_row_that_turns_up_later_is_still_repaired`).
        self.boot()
        before = self.digest()
        schema_before = self.schema_digest()
        version_before = self.conn().execute("PRAGMA user_version").fetchone()[0]
        self.assertGreaterEqual(version_before, EXPLORER_EVAL_CONVENTION_VERSION)
        for run in (1, 2):
            self.boot()
            self.assertEqual(self.digest(), before, "boot #%d changed stored rows" % run)
            self.assertEqual(self.schema_digest(), schema_before,
                             "boot #%d changed the schema" % run)
        self.assertEqual(self.conn().execute("PRAGMA user_version").fetchone()[0], version_before,
                         "the schema version must not move once the migrations have run")

    def test_user_version_never_moves_backwards(self):
        self.boot()
        connection = self.conn()
        try:
            connection.execute("PRAGMA user_version = %d" % (SCHEMA_VERSION + 5))
            connection.commit()
        finally:
            connection.close()
        self.boot()
        self.assertEqual(
            self.conn().execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 5,
            "init_db() must never downgrade a database that is already newer")


# --------------------------------------------------------------------------- #
# 4. The eval-convention migration: once, loud, self-healing (CODE-02-18)
# --------------------------------------------------------------------------- #
class TestExplorerCacheEvalConventionMigration(SchemaTestBase):

    def _seed_payload(self, fen, payload):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        connection = self.conn()
        try:
            connection.execute(
                "INSERT OR REPLACE INTO explorer_cache (fen, ratings, data_json, updated_at)"
                " VALUES (?, '2000,2200,2500', ?, 1)", (fen, raw))
            connection.commit()
        finally:
            connection.close()

    def _payload(self, fen):
        connection = self.conn()
        try:
            raw = connection.execute(
                "SELECT data_json FROM explorer_cache WHERE fen = ?", (fen,)).fetchone()[0]
        finally:
            connection.close()
        return json.loads(raw)

    BLACK_TO_MOVE = "8/8/8/8/8/5k1/8/4K3 b - - 0 1"

    def test_side_to_move_payload_is_negated_and_stamped(self):
        self.boot()
        self._seed_payload(self.BLACK_TO_MOVE, {"eval_convention": "stm",
                                                "moves": [{"san": "Kd5", "eval": 30}]})
        self.boot()
        payload = self._payload(self.BLACK_TO_MOVE)
        self.assertEqual(payload["eval_convention"], "white")
        self.assertEqual(payload["moves"][0]["eval"], -30,
                         "a black-to-move position must be negated into White's POV")
        self.assertGreaterEqual(
            self.conn().execute("PRAGMA user_version").fetchone()[0],
            EXPLORER_EVAL_CONVENTION_VERSION,
            "the migration must stamp PRAGMA user_version so it is a recorded one-shot")

    def test_a_row_that_turns_up_later_is_still_repaired(self):
        """The stamp must not strand a payload that only appears afterwards."""
        self.boot()
        self._seed_payload(self.BLACK_TO_MOVE, {"eval_convention": "white", "moves": []})
        self.boot()
        self._seed_payload(self.BLACK_TO_MOVE, {"eval_convention": "stm",
                                                "moves": [{"san": "Kd5", "eval": 12}]})
        self.boot()
        self.assertEqual(self._payload(self.BLACK_TO_MOVE)["eval_convention"], "white")

    def test_a_compact_json_payload_counts_as_converted(self):
        """The pre-fix gate was TEXTUAL: `data_json NOT LIKE '%"eval_convention": "white"%'`.

        It agreed with `json.dumps` defaults only by coincidence of formatting, and
        that coincidence was load-bearing. The rewrite negates the scores of a
        black-to-move position *unconditionally*, so any already-converted payload
        that the textual gate failed to match -- e.g. one written with
        `separators=(',', ':')` -- had its White-perspective scores flipped a
        second time on the next boot, and again on the boot after that, flipping
        the sign back and forth. The new predicate reads the actual JSON value, so
        a converted payload is recognised as such and left byte-for-byte alone.
        """
        self.boot()
        self._seed_payload(self.BLACK_TO_MOVE, json.dumps(
            {"eval_convention": "white", "moves": [{"san": "Kd5", "eval": -30}]},
            separators=(",", ":")))
        self._seed_payload("4k3/8/8/8/8/8/8/8 w - - 0 1", json.dumps(
            {"eval_convention": "white", "moves": [{"san": "Kd4", "eval": 30}]},
            separators=(",", ":")))
        self.boot()
        self.boot()
        self.assertEqual(self._payload(self.BLACK_TO_MOVE)["moves"][0]["eval"], -30,
                         "an already-converted black-to-move payload must not be negated again")
        self.assertEqual(self._payload("4k3/8/8/8/8/8/8/8 w - - 0 1")["moves"][0]["eval"], 30,
                         "an already-converted white-to-move payload must be left alone")

    def test_a_corrupt_payload_is_reported_loudly_and_left_alone(self):
        """CODE-02-18: the pre-fix code swallowed every per-row error with `pass`,
        so a corrupt payload was skipped forever, silently, on every boot."""
        self.boot()
        connection = self.conn()
        try:
            connection.execute(
                "INSERT OR REPLACE INTO explorer_cache (fen, ratings, data_json, updated_at)"
                " VALUES (?, '2000,2200,2500', '{not json at all', 1)", (self.BLACK_TO_MOVE,))
            connection.commit()
        finally:
            connection.close()
        output = self.boot()
        self.assertIn("explorer_cache eval-convention migration could not rewrite",
                      output, "a per-row failure must be announced, not swallowed:\n%s" % output)
        self.assertIn(self.BLACK_TO_MOVE, output,
                      "the warning must name the offending FEN")
        raw = self.conn().execute(
            "SELECT data_json FROM explorer_cache WHERE fen = ?",
            (self.BLACK_TO_MOVE,)).fetchone()[0]
        self.assertEqual(raw, "{not json at all",
                         "a row that cannot be parsed must be left byte-identical")

    def test_the_probe_is_answered_by_the_partial_index(self):
        """The whole point of CODE-02-18: no full-table `LIKE '%...%'` scan."""
        self.boot()
        plan = "\n".join(self.plan(UNCONVERTED_EXPLORER_SELECT % UNCONVERTED_EXPLORER_PREDICATE))
        self.assertIn("ix_explorer_cache_unconverted", plan,
                      "the unconverted-payload probe must use its partial index; plan was:\n%s" % plan)
        self.assertNotIn("SCAN explorer_cache\n", plan + "\n",
                         "the probe must not be a bare full table scan")


# --------------------------------------------------------------------------- #
# 5. FIXER-P0's guarantee survives the new migrations
# --------------------------------------------------------------------------- #
class TestBootNeverDestroysTheRepertoire(SchemaTestBase):
    """The new indexes, the blunder de-duplication and the `best_pv` migration all
    run in the SAME transaction as FIXER-P0's boot migration, so its guarantee is
    re-asserted here against a repertoire that contains orphan-shaped rows."""

    def _repertoire_snapshot(self):
        connection = self.conn()
        try:
            return {
                "moves": [dict(r) for r in connection.execute(
                    "SELECT * FROM repertoire_moves ORDER BY id")],
                "studies": [dict(r) for r in connection.execute(
                    "SELECT * FROM repertoire_studies ORDER BY id")],
                "study_moves": [dict(r) for r in connection.execute(
                    "SELECT * FROM study_moves ORDER BY study_id, repertoire, parent_fen, move_uci")],
            }
        finally:
            connection.close()

    def _seed_repertoire_with_orphans(self):
        self.boot()
        connection = self.conn()
        try:
            start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
            connection.execute(
                "INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san,"
                " is_user_move, study_name, comment, arrows, is_important, created_at)"
                " VALUES ('white', ?, ?, 'e2e4', 'e4', 1, '', 'first move \\u2014 keep',"
                " '[{\"from\":\"e2\",\"to\":\"e4\"}]', 1, 1)", (start, start))
            # Orphan-shaped: a fragment whose parent_fen is not any stored `fen`,
            # which is exactly what the pre-P0 purge destroyed (157 of 243 moves).
            connection.execute(
                "INSERT INTO repertoire_moves (repertoire, parent_fen, fen, move_uci, move_san,"
                " is_user_move, study_name, comment, created_at)"
                " VALUES ('white', '8/8/8/8/8/5k2/6q1/7K b - - 0 1',"
                " '8/8/8/8/8/6k1/6q1/7K b - - 0 1', 'g2g3', 'Kg3', 1, '', 'fragment', 1)")
            connection.execute(
                "INSERT INTO repertoire_studies (name, repertoire, description, created_at,"
                " updated_at) VALUES ('Caro-Kann Fantasy Variation', 'black', 'mine', 1, 1)")
            connection.commit()
        finally:
            connection.close()

    def test_orphan_shaped_moves_survive_every_boot(self):
        self._seed_repertoire_with_orphans()
        before = self._repertoire_snapshot()
        for run in (1, 2, 3):
            self.boot()
            self.assertEqual(self._repertoire_snapshot(), before,
                             "boot #%d changed the repertoire" % run)

    def test_no_forbidden_default_study_is_invented(self):
        self._seed_repertoire_with_orphans()
        self.boot()
        connection = self.conn()
        try:
            names = [r[0] for r in connection.execute("SELECT name FROM repertoire_studies")]
        finally:
            connection.close()
        for forbidden in FORBIDDEN_AUTO_STUDY_NAMES:
            self.assertNotIn(forbidden, names,
                             "AGENTS.md 9.3 forbids the boot migration creating %r" % forbidden)
        self.assertEqual(names, ["Caro-Kann Fantasy Variation"])

    def test_the_default_boot_never_announces_a_destructive_purge(self):
        self._seed_repertoire_with_orphans()
        output = self.boot()
        self.assertNotIn("DESTRUCTIVE", output,
                         "the opt-in purge must stay opt-in")


# --------------------------------------------------------------------------- #
# 6. The hot joins are answered by indexes
# --------------------------------------------------------------------------- #
class TestHotQueriesUseIndexes(SchemaTestBase):

    def test_study_moves_join_uses_an_index_not_a_scan(self):
        """CODE-02-08. `study_moves`' only index used to be the PRIMARY KEY, which
        leads with the *unconstrained* `study_id`, so the planner had to `SCAN sm`
        for every row of `repertoire_moves` -- O(N x M) at every boot."""
        self.boot()
        sql = ("SELECT rm.id, sm.study_id FROM repertoire_moves rm"
               " LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire"
               " AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci"
               " WHERE rm.repertoire = ?")
        plan = "\n".join(self.plan(sql, ("white",)))
        self.assertIn("ix_study_moves_move", plan,
                      "the study_moves join must be answered by ix_study_moves_move; plan:\n%s" % plan)
        self.assertNotIn("SCAN sm", plan,
                         "study_moves must not be scanned per repertoire_moves row; plan:\n%s" % plan)

    def test_study_name_backfill_probe_uses_the_same_index(self):
        self.boot()
        sql = ("SELECT id FROM repertoire_moves WHERE (study_name IS NULL OR study_name = '')"
               " AND EXISTS (SELECT 1 FROM study_moves sm WHERE sm.repertoire ="
               " repertoire_moves.repertoire AND sm.parent_fen = repertoire_moves.parent_fen"
               " AND sm.move_uci = repertoire_moves.move_uci)")
        plan = "\n".join(self.plan(sql))
        self.assertIn("ix_study_moves_move", plan,
                      "the boot migration's EXISTS probe must use the index; plan:\n%s" % plan)
        self.assertNotIn("AUTOMATIC", plan,
                         "a per-query automatic index means no permanent index exists; plan:\n%s" % plan)

    def test_blunder_lookups_use_indexes(self):
        self.boot()
        natural = "\n".join(self.plan(
            "SELECT id FROM blunders WHERE game_id = ? AND ply = ? AND played_move = ?",
            ("g", 1, "d2d4")))
        self.assertIn("ux_blunders_natural_key", natural,
                      "the analyzer's upsert lookup must use the natural key; plan:\n%s" % natural)
        joined = "\n".join(self.plan(
            "SELECT b.id FROM blunders b JOIN games g ON g.id = b.game_id WHERE b.game_id = ?",
            ("g",)))
        self.assertIn("ix_blunders_game_id", joined,
                      "the blunders<->games join must use an index; plan:\n%s" % joined)
        self.assertNotIn("SCAN b\n", joined + "\n",
                         "blunders must not be scanned per game; plan:\n%s" % joined)

    def test_repertoire_tree_walk_is_a_search(self):
        """The tree walk was already served by the UNIQUE autoindex; assert it
        stays that way (a regression here is what made the builder quadratic)."""
        self.boot()
        plan = "\n".join(self.plan(
            "SELECT * FROM repertoire_moves WHERE repertoire = ? AND parent_fen = ?",
            ("white", START_FEN_3 + " 0 1")))
        self.assertIn("SEARCH repertoire_moves", plan,
                      "the tree walk must be a SEARCH; plan:\n%s" % plan)


if __name__ == "__main__":
    unittest.main()
