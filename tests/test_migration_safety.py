"""
Regression tests for the boot-migration data-destruction incident (audit CODE-05-03).

`init_db()` runs on EVERY application launch (`start.py` and the FastAPI `startup`
event). Before the fix, its migration step 3 walked `parent_fen -> fen` links
forward from the starting position and DELETEd every `repertoire_moves` row it
could not reach. A move whose `parent_fen` is not the `fen` of another stored
move -- i.e. a legitimate side-line / sub-line fragment that starts
mid-variation, or a move stored before its parent -- was therefore silently
destroyed on the next app launch: 157 of the user's 243 prepared moves, 155 of
them `is_user_move = 1`.

The migration additionally invented the "Italian Game" / "Sicilian Dragon" /
"King's Indian Defense" studies and relabelled every one of the user's moves
with one of those generic names, which AGENTS.md section 9.3 forbids.

These tests run against their own throwaway SQLite file (set AFTER
`tests.test_setup` has configured the global test environment) and therefore
never touch `chess_improvement.db`.
"""
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest

import chess

import tests.test_setup  # MUST be imported first: isolates CHESS_DB_PATH from production

from backend.db import FORBIDDEN_AUTO_STUDY_NAMES, find_unreachable_repertoire_moves, init_db


START_FEN = chess.STARTING_FEN
START_FEN_3 = " ".join(START_FEN.split()[:3])

# Real opening lines, played from the start. Inserted in order, they form a
# connected tree (every parent_fen is the previous move's fen).
WHITE_LINES = [
    "e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6 d3 d6 O-O O-O Nbd2 a6 Re1 Be6 Bb3",
    "e4 e5 Nf3 Nc6 Bc4 Nf6 d3 d6 O-O Be7 Re1 O-O Nc3 Nb8 Bg5",
    "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6 Bg5 e6 f4 Be7 Qf3 Qc7 O-O-O",
    "e4 e6 d4 d5 Nc3 Bb4 e5 c5 a3 Bxc3 bxc3 Ne7 Qg4 Qc7",
    "e4 c6 d4 d5 Nc3 Nf6 Nge2 e5 g4 Bb4",
    "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O h3",
    "c4 e5 Nc3 Nf6 Nf3 Nc6 g3 d5 cxd5 Nxd5 Bg2 Nb6 O-O Be7",
    "Nf3 d5 c4 dxc4 g3 Nc6 d4 e5 Bg2 Nb4",
    "d4 d5 c4 e6 Nc3 Nf6 Bg5 Be7 e3 O-O Nf3 h6",
    "d4 d5 c4 c6 Nc3 dxc4 a4 Bf5 e3 e6 Bxc4 Bb4",
]

# Black repertoire trees: White's moves are the opponent's replies (is_user_move=0).
BLACK_LINES = [
    "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6 Bg5 e6 f4 Be7 Qf3 Qc7",
    "e4 c5 Nf3 Nc6 d4 cxd4 Nxd4 g6 Nc3 Bg7 Be3 Nf6",
    "e4 e6 d4 d5 Nc3 Bb4 e5 c5 a3 Bxc3 bxc3",
    "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7",
    "e4 d5 exd5 Qxd5 Nc3 Qa5",
    "c4 c5 Nf3 d6 Nc3 Nc6 d4 cxd4 Nxd4",
    "e4 c6 d4 d5 Nc3 Nf6 Nge2 e5",
    "d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Nf3 O-O",
    "d4 f5 g3 Nf6 Bg2 e6 Nf3",
    "e4 Nf6 e5 Nd5 d4 d6 Nf3",
    "f4 d5 e3 Nf6 Nf3 c6",
    "d4 d5 c4 dxc4 Nf3 Nf6 e3 e6",
    "c4 Nf6 Nc3 e6 Nf3 d5 cxd5 Nxd5",
    "e4 e5 Nf3 Nf6 d4 exd4 e5 Ne4 Nxd4 Nc6",
    "d4 d5 c4 e5 dxe5 f6",
    "e4 c5 Nf3 e6 d4 cxd4 Nxd4 a6 Be3",
    "d4 Nc6 c4 e5 Nf3",
]

# Fragment source lines. Only their TAIL is ever stored, so the first stored
# move's parent_fen is a position that is neither the start position nor the
# `fen` of any other stored move -- exactly the "sub-line imported mid-variation"
# shape that the old purge destroyed. They all open with a first move that no
# connected line uses, so their tail positions never collide with the tree.
WHITE_FRAGMENT_LINES = [
    "f4 d5 e3 Nf6 Nf3 c5",
    "b4 e5 Bb2 f6 e3",
    "Nc3 Nf6 Nf3 d5 d4",
    "g3 d5 Bg2 Nf6",
    "h3 h6 g4 g5",
    "b3 d5 Bb2 Nf6",
    "a3 e5 Nc3 Nc6",
    "a3 e5 d4 exd4",
    "Na3 d5 c4 e6",
    "Nh3 d5 g4 e5",
    "b4 d5 Nf3 e5",
    "g3 Nf6 Bg2 d5",
    "a3 d5 b4 Nf6",
    "f4 Nc6 e3 e5",
    "b3 Nf6 Bb2 g6",
    "Na3 c5 Nf3 d6",
    "h3 Nf6 g4 e6",
    "g3 c5 Bg2 Nc6",
    "a3 e6 b4 d5",
    "Nh3 Nf6 g4 c5",
    "b4 c5 Bb2 Nc6",
    "f4 g5 e3 Bg7",
    "a3 d6 b4 c5",
    "h3 c5 g4 Nc6",
    "f4 c5 e3 Nc6",
    "b3 c5 Bb2 Nc6",
    "a3 Nf6 b4 c5",
    "g3 c5 Nf3 Nc6",
]

# Fragment sources for the Black repertoire (positions absent from BLACK_LINES).
BLACK_FRAGMENT_LINES = [
    "e4 g6 d4 Bg7 Nc3 d6",
    "e4 Nc6 Nf3 e5",
    "d4 e6 c4 f5",
    "d4 Nf6 Nf3 e6",
    "e4 c5 Nc3 d6",
    "e4 d6 d4 Nf6 Nc3 g6",
    "d4 d6 c4 Nf6 Nf3 c5",
    "e4 e5 Nf3 f5 d4",
    "d4 f6 c4 e6 Nf3",
    "e4 Nf6 Nf3 b6 d4",
    "d4 Nc6 c4 d5 e3",
    "e4 c6 d4 Nf6 Nc3 e5",
    "d4 b6 c4 e5 Nf3",
    "e4 d5 Nf3 Nf6 e5",
    "d4 f5 c4 e6 Nf3",
    "e4 Nc6 d4 e6 Nf3",
]

MOVE_COLUMNS = (
    "repertoire, parent_fen, fen, move_uci, move_san, is_user_move, popularity_pct, "
    "win_rate, eval_score, opening_name, eco, is_important, comment, arrows, study_name, "
    "times_practiced, times_correct, last_practiced, next_review, interval_days, created_at"
)


def _fen4(fen):
    """Same normalisation the app writes into the DB (repertoire.normalize_fen)."""
    return " ".join(fen.split()[:4])


def _fen3(fen):
    return " ".join(fen.split()[:3])


def _walk(line):
    """Return [(parent_fen4, fen4, uci, san, is_white_move, mover)] for a SAN line."""
    board = chess.Board()
    steps = []
    for san in line.split():
        move = board.parse_san(san)
        parent = _fen4(board.fen())
        san_text = board.san(move)
        white_to_move = board.turn == chess.WHITE
        board.push(move)
        steps.append((parent, _fen4(board.fen()), move.uci(), san_text, white_to_move, san))
    return steps


class MigrationSafetyTestBase(unittest.TestCase):
    """Own throwaway DB per test class, restored to the global test DB on teardown."""

    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(prefix="chess_migration_safety_", suffix=".db")
        os.close(fd)
        cls._prev_db_path = os.environ.get("CHESS_DB_PATH")
        os.environ["CHESS_DB_PATH"] = cls.db_path
        init_db()  # create a clean, empty schema
        conn = cls._conn()
        try:
            # Reset autoincrement bookkeeping so ids are comparable across the
            # before/after snapshots without depending on insert history.
            conn.execute("DELETE FROM sqlite_sequence")
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        try:
            if cls._prev_db_path is None:
                os.environ.pop("CHESS_DB_PATH", None)
            else:
                os.environ["CHESS_DB_PATH"] = cls._prev_db_path
        finally:
            for suffix in ("", "-wal", "-shm"):
                path = cls.db_path + suffix
                if os.path.exists(path):
                    os.unlink(path)

    @classmethod
    def _conn(cls):
        conn = sqlite3.connect(cls.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        conn.row_factory = sqlite3.Row
        return conn

    def setUp(self):
        # Every test starts from an empty repertoire but a migrated schema.
        conn = self._conn()
        conn.execute("DELETE FROM repertoire_moves")
        conn.execute("DELETE FROM repertoire_studies")
        conn.execute("DELETE FROM study_moves")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------ helpers
    def _stored_fens(self, repertoire):
        conn = self._conn()
        try:
            return {
                _fen3(r["fen"])
                for r in conn.execute("SELECT fen FROM repertoire_moves WHERE repertoire = ?", (repertoire,))
            }
        finally:
            conn.close()

    def _is_isolated_fragment(self, repertoire, parent_fen):
        """True when the old migration's BFS would classify this move as an orphan."""
        parent3 = _fen3(parent_fen)
        return parent3 != START_FEN_3 and parent3 not in self._stored_fens(repertoire)

    def _insert_move(self, repertoire, parent_fen, fen, uci, san, is_user_move,
                     study_name="", comment="", arrows="", opening_name="Sicilian Defense"):
        conn = self._conn()
        cur = conn.execute(
            f"INSERT OR IGNORE INTO repertoire_moves ({MOVE_COLUMNS}) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (repertoire, parent_fen, fen, uci, san, 1 if is_user_move else 0,
             12.5, 51.0, 17, opening_name, "B90", 0, comment, arrows, study_name,
             0, 0, 0, 0, 1.0, 1700000000),
        )
        inserted = cur.rowcount
        conn.commit()
        conn.close()
        return inserted

    def _insert_line(self, repertoire, line, study_name="", comment="", arrows=""):
        """Insert a connected line in order (every parent_fen is a stored fen)."""
        count = 0
        conn = self._conn()
        try:
            for parent, fen, uci, san, white_moved, _ in _walk(line):
                is_user_move = white_moved if repertoire == "white" else (not white_moved)
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO repertoire_moves ({MOVE_COLUMNS}) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (repertoire, parent, fen, uci, san, 1 if is_user_move else 0,
                     12.5, 51.0, 17, "Sicilian Defense", "B90", 0, comment, arrows, study_name,
                     0, 0, 0, 0, 1.0, 1700000000),
                )
                if cur.rowcount:
                    count += 1
            conn.commit()
        finally:
            conn.close()
        return count

    def _insert_fragments(self, repertoire, lines, study_name="", comment="", arrows="",
                          tail=2, repeat=1):
        fens_set = self._stored_fens(repertoire)
        inserted = []
        conn = self._conn()
        try:
            for _ in range(repeat):
                for line in lines:
                    steps = _walk(line)
                    for parent, fen, uci, san, white_moved, _ in steps[-tail:]:
                        parent3 = _fen3(parent)
                        if parent3 == START_FEN_3 or parent3 in fens_set:
                            continue
                        is_user_move = white_moved if repertoire == "white" else (not white_moved)
                        cur = conn.execute(
                            f"INSERT OR IGNORE INTO repertoire_moves ({MOVE_COLUMNS}) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (repertoire, parent, fen, uci, san, 1 if is_user_move else 0,
                             12.5, 51.0, 17, "Sicilian Defense", "B90", 0, comment, arrows, study_name,
                             0, 0, 0, 0, 1.0, 1700000000),
                        )
                        if cur.rowcount:
                            inserted.append((parent, uci))
                            fens_set.add(_fen3(fen))
            conn.commit()
        finally:
            conn.close()
        return inserted

    def _create_study(self, name, repertoire, description=""):
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO repertoire_studies (name, repertoire, description, created_at, updated_at) "
            "VALUES (?,?,?,0,0)",
            (name, repertoire, description),
        )
        conn.commit()
        study_id = cur.lastrowid
        conn.close()
        return study_id

    def _link_study_move(self, study_id, repertoire, parent_fen, uci):
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO study_moves (study_id, repertoire, parent_fen, move_uci) VALUES (?,?,?,?)",
            (study_id, repertoire, parent_fen, uci),
        )
        conn.commit()
        conn.close()

    def _snapshot(self):
        conn = self._conn()
        try:
            snap = {}
            for table, order in (
                ("repertoire_moves", "id"),
                ("repertoire_studies", "id"),
                ("study_moves", "study_id, repertoire, parent_fen, move_uci"),
            ):
                snap[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
            snap["tables"] = sorted(
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )
            return snap
        finally:
            conn.close()

    def _move(self, repertoire, uci):
        conn = self._conn()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM repertoire_moves WHERE repertoire = ? AND move_uci = ? ORDER BY id",
                (repertoire, uci))]
            self.assertTrue(rows, f"expected a stored {repertoire} move {uci}")
            return rows
        finally:
            conn.close()

    def _move_at(self, repertoire, parent_fen, uci):
        """The single row for this exact position+move (fragments can share a move_uci)."""
        conn = self._conn()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM repertoire_moves WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?",
                (repertoire, parent_fen, uci))]
            self.assertEqual(len(rows), 1, f"expected exactly one stored {repertoire} move {uci}")
            return rows[0]
        finally:
            conn.close()

    def _assert_preserved(self, before, after, label):
        self.assertEqual(
            len(before["repertoire_moves"]), len(after["repertoire_moves"]),
            f"{label}: repertoire_moves row count changed "
            f"{len(before['repertoire_moves'])} -> {len(after['repertoire_moves'])} "
            f"(DELETED {len(before['repertoire_moves']) - len(after['repertoire_moves'])} rows)")
        self.assertEqual(before["repertoire_moves"], after["repertoire_moves"],
                         f"{label}: repertoire_moves rows were modified")
        self.assertEqual(before["repertoire_studies"], after["repertoire_studies"],
                         f"{label}: repertoire_studies changed")
        self.assertEqual(before["study_moves"], after["study_moves"],
                         f"{label}: study_moves changed")
        self.assertEqual(before["tables"], after["tables"], f"{label}: schema changed")


class TestBootMigrationPreservesRepertoire(MigrationSafetyTestBase):

    def test_connected_line_and_isolated_fragment_survive_boot(self):
        """The core incident: one connected line + side-line fragments, one boot."""
        self._insert_line("white", "e4 e5 Nf3 Nc6 Bc4", study_name="")
        self._insert_line("black", "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6", study_name="")

        white_frags = self._insert_fragments(
            "white", WHITE_FRAGMENT_LINES,
            study_name="Anti French Defense",
            comment="fragment imported from a video \u2014 keep me",
            arrows='[{"from":"e2","to":"e4","color":"#22c55e"}]',
        )
        black_frags = self._insert_fragments(
            "black", BLACK_FRAGMENT_LINES,
            study_name="Caro-Kann Fantasy Variation",
            comment="black fragment",
            arrows='[{"from":"d7","to":"d5","color":"#38bdf8"}]',
        )
        self.assertGreaterEqual(len(white_frags), 3, "test fixture must build isolated fragments")
        self.assertGreaterEqual(len(black_frags), 3, "test fixture must build isolated fragments")

        study_id = self._create_study("Anti French Defense", "white")
        for parent, uci in white_frags:
            self._link_study_move(study_id, "white", parent, uci)

        before = self._snapshot()
        init_db()  # <-- the app boot
        after = self._snapshot()

        self._assert_preserved(before, after, "single boot")

        # Field-level spot checks on the fragment that carries user annotations.
        frag = self._move_at("white", white_frags[0][0], white_frags[0][1])
        before_frag = next(r for r in before["repertoire_moves"] if r["id"] == frag["id"])
        self.assertEqual(before_frag, frag)
        self.assertEqual(frag["study_name"], "Anti French Defense")
        self.assertIn("keep me", frag["comment"])
        self.assertIn("22c55e", frag["arrows"])
        self.assertIn(frag["is_user_move"], (0, 1))
        self.assertTrue(frag["move_uci"] and frag["move_san"])

    def test_fragments_keep_their_user_move_flag_and_annotations(self):
        self._insert_line("white", "d4 d5 c4 e6")
        frags = self._insert_fragments("white", ["f4 d5 e3 Nf6 Nf3 c5", "b4 e5 Bb2 f6 e3"],
                                       comment="side line \u00b5 note", arrows='[{"from":"f2","to":"f4","color":"#ef4444"}]')
        self.assertTrue(frags)
        expected = {}
        for parent, uci in frags:
            row = self._move_at("white", parent, uci)
            expected[row["id"]] = row

        init_db()

        for move_id, row in expected.items():
            now = self._move_at("white", row["parent_fen"], row["move_uci"])
            self.assertEqual(now["id"], move_id)
            for column in ("repertoire", "parent_fen", "fen", "move_uci", "move_san",
                           "is_user_move", "comment", "arrows", "study_name", "opening_name",
                           "eco", "is_important", "popularity_pct", "win_rate", "eval_score"):
                self.assertEqual(row[column], now[column], f"move {move_id} column {column} changed")

    def test_no_forbidden_default_studies_are_injected(self):
        self._insert_line("white", "e4 e5 Nf3 Nc6 Bc4")
        self._insert_line("black", "e4 c5 Nf3 d6 d4 cxd4 Nxd4")
        self._insert_fragments("white", WHITE_FRAGMENT_LINES)
        self._insert_fragments("black", BLACK_FRAGMENT_LINES)

        before = self._snapshot()
        init_db()
        after = self._snapshot()

        created = [s["name"] for s in after["repertoire_studies"]
                   if s not in before["repertoire_studies"]]
        self.assertEqual(created, [], "boot migration must not invent studies")
        self.assertEqual(after["study_moves"], [], "boot migration must not invent study membership")
        names = [r["name"] for r in after["repertoire_studies"]]
        for forbidden in FORBIDDEN_AUTO_STUDY_NAMES:
            self.assertNotIn(forbidden, names,
                             f"AGENTS.md 9.3 forbids auto-seeding {forbidden!r}")
        self.assertEqual(after["repertoire_moves"], before["repertoire_moves"])

    def test_init_db_is_idempotent(self):
        """Running init_db() repeatedly must be a strict no-op after the first boot."""
        self._insert_line("white", "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6")
        self._insert_line("black", "d4 Nf6 c4 g6 Nc3 Bg7")
        # Pre-label the fragments so the (additive) study_name backfill in step 2 is
        # already satisfied: the very first boot must then also change nothing.
        white_frags = self._insert_fragments("white", WHITE_FRAGMENT_LINES,
                                             study_name="Anti French Defense")
        black_frags = self._insert_fragments("black", BLACK_FRAGMENT_LINES,
                                             study_name="Caro-Kann Fantasy Variation")
        self.assertTrue(white_frags and black_frags)
        study_id = self._create_study("Anti French Defense", "white")
        for parent, uci in white_frags:
            self._link_study_move(study_id, "white", parent, uci)

        baseline = self._snapshot()  # BEFORE any boot
        for run in range(1, 4):
            init_db()
            current = self._snapshot()
            self._assert_preserved(baseline, current, f"boot #{run}")

    def test_large_repertoire_with_fragments_is_fully_preserved(self):
        """Mirrors the incident: 100+ moves per colour including isolated fragments."""
        for line in WHITE_LINES:
            self._insert_line("white", line)
        for line in BLACK_LINES:
            self._insert_line("black", line)
        white_frags = self._insert_fragments("white", WHITE_FRAGMENT_LINES, comment="w frag")
        black_frags = self._insert_fragments("black", BLACK_FRAGMENT_LINES, comment="b frag")

        conn = self._conn()
        counts = {
            "white": conn.execute(
                "SELECT COUNT(*) FROM repertoire_moves WHERE repertoire='white'").fetchone()[0],
            "black": conn.execute(
                "SELECT COUNT(*) FROM repertoire_moves WHERE repertoire='black'").fetchone()[0],
            "user": conn.execute(
                "SELECT COUNT(*) FROM repertoire_moves WHERE is_user_move=1").fetchone()[0],
        }
        conn.close()
        self.assertGreater(counts["white"], 100, f"fixture too small: {counts}")
        self.assertGreater(counts["black"], 100, f"fixture too small: {counts}")
        self.assertGreater(counts["user"], 50, f"fixture too small: {counts}")
        self.assertGreaterEqual(len(white_frags), 10, f"fixture needs isolated fragments: {counts}")
        self.assertGreaterEqual(len(black_frags), 10, f"fixture needs isolated fragments: {counts}")

        before = self._snapshot()
        init_db()
        after = self._snapshot()

        self._assert_preserved(before, after, "large repertoire boot")
        conn = self._conn()
        try:
            after_counts = {
                "white": conn.execute(
                    "SELECT COUNT(*) FROM repertoire_moves WHERE repertoire='white'").fetchone()[0],
                "black": conn.execute(
                    "SELECT COUNT(*) FROM repertoire_moves WHERE repertoire='black'").fetchone()[0],
                "user": conn.execute(
                    "SELECT COUNT(*) FROM repertoire_moves WHERE is_user_move=1").fetchone()[0],
            }
        finally:
            conn.close()
        self.assertEqual(before and after_counts, counts,
                         "row counts after boot must equal the counts before boot")

    def test_unreachable_moves_are_reported_but_never_deleted(self):
        self._insert_line("white", "e4 e5 Nf3 Nc6 Bc4")
        frags = self._insert_fragments("white", WHITE_FRAGMENT_LINES)
        self.assertTrue(frags)

        conn = self._conn()
        try:
            before_ids = {r["id"] for r in conn.execute("SELECT id FROM repertoire_moves")}
        finally:
            conn.close()

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            init_db()
        output = buffer.getvalue()

        conn = self._conn()
        try:
            after_ids = {r["id"] for r in conn.execute("SELECT id FROM repertoire_moves")}
        finally:
            conn.close()
        self.assertEqual(before_ids, after_ids, "boot must not delete unreachable moves")

        conn = self._conn()
        try:
            unreachable = find_unreachable_repertoire_moves(conn)
        finally:
            conn.close()
        flagged = {mid for ids in unreachable.values() for mid in ids}
        fragment_ids = {self._move("white", uci)[0]["id"] for _, uci in frags}
        self.assertTrue(fragment_ids & flagged,
                        "the read-only diagnostic should still flag fragments for human review")
        self.assertNotIn("DESTRUCTIVE", output,
                         "the default boot path must not announce a destructive purge")


class TestOptInPurgeAndFailSafe(MigrationSafetyTestBase):

    def test_purge_requires_explicit_opt_in(self):
        self._insert_line("white", "e4 e5 Nf3 Nc6 Bc4")
        self._insert_fragments("white", WHITE_FRAGMENT_LINES)

        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM repertoire_moves").fetchone()[0]
        finally:
            conn.close()

        init_db()  # default: no deletion
        conn = self._conn()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM repertoire_moves").fetchone()[0], total)
        finally:
            conn.close()

        # Explicit opt-in still works, and it is loud about what it is doing.
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            init_db(purge_orphans=True)
        self.assertIn("DESTRUCTIVE", buffer.getvalue())
        self.assertIn("BACK UP THE DATABASE", buffer.getvalue())

        conn = self._conn()
        try:
            remaining = conn.execute("SELECT COUNT(*) FROM repertoire_moves").fetchone()[0]
        finally:
            conn.close()
        self.assertLess(remaining, total, "opt-in purge is expected to drop unreachable fragments")
        # The connected line from the start position must survive even the opt-in purge.
        self._assertTrueRow("white", "e2e4")

    def test_env_var_enables_the_purge(self):
        self._insert_line("white", "e4 e5 Nf3 Nc6 Bc4")
        self._insert_fragments("white", WHITE_FRAGMENT_LINES)
        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM repertoire_moves").fetchone()[0]
        finally:
            conn.close()

        # Baseline boot with the flag OFF: this must not touch anything, which is
        # what makes the later opt-in deletion attributable to the env var alone.
        init_db()
        conn = self._conn()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM repertoire_moves").fetchone()[0], total,
                "a boot with CHESS_PURGE_ORPHANS unset must not delete anything")
        finally:
            conn.close()

        os.environ["CHESS_PURGE_ORPHANS"] = "1"
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                init_db()
        finally:
            del os.environ["CHESS_PURGE_ORPHANS"]
        self.assertIn("DESTRUCTIVE", buffer.getvalue())

        conn = self._conn()
        try:
            remaining = conn.execute("SELECT COUNT(*) FROM repertoire_moves").fetchone()[0]
        finally:
            conn.close()
        self.assertLess(remaining, total, "CHESS_PURGE_ORPHANS=1 must enable the lossy purge")
        self._assertTrueRow("white", "e2e4")

        # Flag is off again once the env var is gone.
        init_db()
        conn = self._conn()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM repertoire_moves").fetchone()[0], remaining)
        finally:
            conn.close()

    def test_failed_migration_rolls_back_instead_of_half_applying(self):
        """A crash mid-migration must not leave the DB partially migrated."""
        import backend.db as db_mod

        # (a) A study-linked move that is missing from repertoire_moves: a healthy
        #     migration re-inserts it (step 1). 1...e5 stored on its own, i.e. a
        #     move whose parent (after 1.b4) is not itself a stored move.
        board = chess.Board()
        board.push_san("b4")
        missing_parent = _fen4(board.fen())
        move = board.parse_san("e5")

        # (b) A stored move with an empty study_name that belongs to a study:
        #     a healthy migration backfills it (step 2).
        self._insert_line("white", "e4 e5")
        conn = self._conn()
        stored = conn.execute(
            "SELECT id, parent_fen, move_uci FROM repertoire_moves "
            "WHERE repertoire='white' AND move_uci='e2e4'").fetchone()
        conn.close()
        self.assertIsNone(stored["study_name"] if "study_name" in stored.keys() else None)

        study_id = self._create_study("Anti French Defense", "white")
        self._link_study_move(study_id, "white", missing_parent, move.uci())
        self._link_study_move(study_id, "white", stored["parent_fen"], stored["move_uci"])

        before = self._snapshot()

        original = db_mod.find_unreachable_repertoire_moves
        db_mod.find_unreachable_repertoire_moves = lambda conn: (_ for _ in ()).throw(
            RuntimeError("boom: simulated migration failure"))
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                init_db()
        finally:
            db_mod.find_unreachable_repertoire_moves = original

        self.assertIn("boom: simulated migration failure", buffer.getvalue())
        after = self._snapshot()
        # Nothing from steps 1 and 2 may have survived the failure.
        self.assertEqual(before["repertoire_moves"], after["repertoire_moves"],
                         "a failed migration must roll back, not partially apply")
        self.assertEqual(before["study_moves"], after["study_moves"])

        # Once the failure is gone the same migration completes normally.
        init_db()
        conn = self._conn()
        try:
            restored = conn.execute(
                "SELECT COUNT(*) FROM repertoire_moves WHERE parent_fen = ? AND move_uci = ?",
                (missing_parent, move.uci())).fetchone()[0]
            backfilled = conn.execute(
                "SELECT study_name FROM repertoire_moves WHERE id = ?", (stored["id"],)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(restored, 1, "study-linked move should be restored by a healthy migration")
        self.assertEqual(backfilled, "Anti French Defense",
                         "study_name backfill is additive and must fill the empty value")

    def _assertTrueRow(self, repertoire, uci):
        conn = self._conn()
        try:
            found = conn.execute(
                "SELECT COUNT(*) FROM repertoire_moves WHERE repertoire=? AND move_uci=?",
                (repertoire, uci)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(found, 1, f"expected the connected move {uci} to survive")


if __name__ == "__main__":
    unittest.main()
