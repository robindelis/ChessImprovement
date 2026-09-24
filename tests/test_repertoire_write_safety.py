"""
Regression tests for the FIXER-A audit patch on `backend/repertoire.py`.

Each test asserts a real post-condition of the fix and fails against the pre-fix module
(run them against the pre-fix snapshot with
`audit/scripts/FIXA-01_prefix_runner.py tests.test_repertoire_write_safety`).

Findings covered:
  CODE-1  re-adding an existing repertoire move must be an idempotent UPSERT that keeps
          comment / arrows / is_important / study / spaced-repetition state
  CODE-2  seed_default_repertoires(force=True) must never prune a user-prepared line and must
          return a real result object on every path
  CODE-3  the trainer's prepared-move lookup must be deterministic (exact parent wins)
  CODE-4  get_repertoire_tree must score a black repertoire with the BLACK player's stats
  CODE-5  the 2000+ explorer must be fetched once per opponent position
  CODE-6  get_game_gaps (a GET) must not write, and must not persist a 0/sentinel on failure
  CODE-17 delete_study(delete_moves=True) must only remove the moves exclusive to that study
"""

import json
import unittest

import tests.test_setup  # FIRST backend-adjacent import (isolated temp DB)

import chess
import backend.repertoire as rep_mod
from backend.db import get_db, init_db
from backend.repertoire import RepertoireManager, normalize_fen

START = normalize_fen(chess.STARTING_FEN)
# Two legal positions that share the same 3-token FEN prefix but differ in the en-passant token
# (the ambiguity CODE-3 is about). Both are reachable in a real game.
FEN_EP = "rnbqkbnr/ppp2ppp/4p3/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 3"   # 1.e4 e6 2.e5 d5
FEN_NOEP = "rnbqkbnr/ppp2ppp/4p3/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq - 0 3"  # same pieces, no ep


def _rows(sql, params=()):
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


class RepertoireWriteSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.rm = RepertoireManager.get_instance()
        # Snapshot the repertoire tables so this class is invisible to the rest of the suite
        # (unittest shares one temp DB across every module in a run).
        conn = get_db()
        try:
            cls._snapshot = {}
            for table in ("repertoire_studies", "repertoire_moves", "study_moves"):
                cls._snapshot[table] = [
                    tuple(r) for r in conn.execute("SELECT * FROM %s" % table).fetchall()
                ]
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        conn = get_db()
        try:
            for table in ("study_moves", "repertoire_moves", "repertoire_studies"):
                conn.execute("DELETE FROM %s" % table)
            for table in ("repertoire_studies", "repertoire_moves", "study_moves"):
                for row in cls._snapshot[table]:
                    conn.execute(
                        "INSERT OR REPLACE INTO %s VALUES (%s)" % (table, ",".join("?" * len(row))),
                        row,
                    )
            conn.commit()
        finally:
            conn.close()

    def setUp(self):
        conn = get_db()
        try:
            for table in ("study_moves", "repertoire_moves", "repertoire_studies"):
                conn.execute("DELETE FROM %s" % table)
            conn.commit()
        finally:
            conn.close()
        # No test may reach the network. (Capture the real bound methods BEFORE stubbing.)
        self._orig_explorer_count = self.rm.get_explorer_game_count
        self._orig_gaps = self.rm.get_game_gaps
        self._orig_stats = self.rm.get_user_position_stats
        self._orig_cand = self.rm.get_candidate_moves_2000
        self.rm.get_explorer_game_count = lambda fen, ratings="2000,2200,2500": 0
        self.rm.get_game_gaps = lambda *a, **kw: {"color": "white", "gaps": [], "patched_gaps": [],
                                                  "unpatched_gaps": 0, "unpatched_count": 0,
                                                  "patched_count": 0, "total_gaps": 0,
                                                  "min_games": 30, "total_games_analyzed": 0,
                                                  "total_games_color": 0}
        self.rm.get_user_position_stats = lambda color="white": {}

    def tearDown(self):
        # drop the instance-level stubs again so the class methods are visible
        for name in ("get_explorer_game_count", "get_game_gaps", "get_user_position_stats",
                     "get_candidate_moves_2000"):
            self.rm.__dict__.pop(name, None)

    # ------------------------------------------------------------------ CODE-1
    def test_readding_existing_move_preserves_notes_arrows_importance_and_srs(self):
        rm = self.rm
        self.assertEqual(
            rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)["status"],
            "success",
        )
        meta = rm.update_move_metadata(
            repertoire="white",
            parent_fen=chess.STARTING_FEN,
            move_uci="e2e4",
            is_important=True,
            comment="my precious note",
            arrows=json.dumps([{"from": "e2", "to": "e4", "color": "#22c55e"}]),
            study_name="Anti French",
        )
        self.assertEqual(meta["status"], "success")

        # An opponent reply so the move has a subtree worth protecting.
        b = chess.Board()
        b.push_uci("e2e4")
        self.assertEqual(
            rm.add_repertoire_move("white", b.fen(), "e7e5", is_user_move=False)["status"], "success"
        )

        # Simulate a fully mastered move (2/2, interval 3 days).
        conn = get_db()
        conn.execute(
            """
            UPDATE repertoire_moves
            SET times_practiced = 2, times_correct = 2, last_practiced = 111, next_review = 222,
                interval_days = 3.0
            WHERE parent_fen = ? AND move_uci = 'e2e4'
            """,
            (START,),
        )
        conn.commit()
        before = _rows(
            "SELECT * FROM repertoire_moves WHERE parent_fen = ? AND move_uci = 'e2e4'", (START,)
        )[0]

        # Re-add the very same move with no metadata arguments - exactly what a second
        # import_game_opening() / scout whole-line import does.
        again = rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
        self.assertEqual(again["status"], "success")

        after = _rows(
            "SELECT * FROM repertoire_moves WHERE parent_fen = ? AND move_uci = 'e2e4'", (START,)
        )
        self.assertEqual(len(after), 1, "re-adding a move must not create a duplicate row")
        row = after[0]

        # The row itself (and therefore any in-flight trainer session keyed on its id) survives.
        self.assertEqual(row["id"], before["id"])
        self.assertEqual(row["created_at"], before["created_at"])
        # User-authored metadata survives.
        self.assertEqual(row["comment"], "my precious note")
        self.assertIn("e2", row["arrows"])
        self.assertEqual(row["is_important"], 1)
        self.assertEqual(row["study_name"], "Anti French")
        # Spaced-repetition state survives.
        self.assertEqual(row["times_practiced"], 2)
        self.assertEqual(row["times_correct"], 2)
        self.assertEqual(row["last_practiced"], 111)
        self.assertEqual(row["next_review"], 222)
        self.assertEqual(row["interval_days"], 3.0)
        # ...and the response reports the preserved values, not empty ones.
        self.assertEqual(again["comment"], "my precious note")
        self.assertIn("e2", again["arrows"])
        self.assertTrue(again["is_important"])
        self.assertEqual(again["study_name"], "Anti French")

        progress = rm.get_trainer_progress("white")
        self.assertEqual(progress["mastered_count"], 1)

        # The subtree is intact and the one-user-move-per-position invariant still holds.
        self.assertEqual(len(_rows("SELECT * FROM repertoire_moves WHERE repertoire='white'")), 2)
        # AUDIT FIXER-R2a / SCOUT-02: the replacement is now *explicit*. `add_repertoire_move`
        # defaults to `replace_existing=False` (report a conflict, change nothing), so this
        # deliberate "make this my move here" action states its consent. The assertion is
        # unchanged: after an explicit replacement the position still holds exactly ONE user
        # move, and it is the new one. (Pre-fix this call relied on the destructive default.)
        replaced = rm.add_repertoire_move("white", chess.STARTING_FEN, "d2d4", is_user_move=True,
                                          replace_existing=True)
        self.assertEqual(replaced["status"], "success")
        user_moves = _rows(
            "SELECT move_uci FROM repertoire_moves WHERE repertoire='white' AND parent_fen = ?"
            " AND is_user_move = 1",
            (START,),
        )
        self.assertEqual([m["move_uci"] for m in user_moves], ["d2d4"])

    # ------------------------------------------------------------------ CODE-2
    def test_force_seed_never_deletes_a_user_prepared_line_and_returns_a_result(self):
        rm = self.rm
        # RobiDeli's Anti-French: 1.e4 e6 2.d4 d5 3.e5 c5 4.f3
        board = chess.Board()
        for uci, user in [("e2e4", True), ("e7e6", False), ("d2d4", True), ("d7d5", False),
                          ("e4e5", True), ("c7c5", False), ("g1f3", True)]:
            self.assertEqual(
                rm.add_repertoire_move("white", board.fen(), uci, is_user_move=user)["status"],
                "success",
            )
            board.push_uci(uci)
        rm.update_move_metadata("white", chess.STARTING_FEN, "e2e4",
                                comment="anti-french prep", is_important=True)
        conn = get_db()
        conn.execute(
            """
            UPDATE repertoire_moves SET times_practiced = 4, times_correct = 4, interval_days = 6.0
            WHERE move_uci = 'e4e5' AND is_user_move = 1
            """
        )
        conn.commit()
        before = _rows("SELECT * FROM repertoire_moves WHERE repertoire='white' ORDER BY id")

        result = rm.seed_default_repertoires(force=True)

        # 1. the endpoint must never answer `null`
        self.assertTrue(result, "seed_default_repertoires must return a truthy result object")
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("status"), "success")
        self.assertIn("moves_added", result)

        # 2. not one of the user's prepared rows is gone, and none lost its metadata
        after = _rows("SELECT * FROM repertoire_moves WHERE repertoire='white' ORDER BY id")
        kept = {(r["parent_fen"], r["move_uci"]): r for r in after}
        for row in before:
            key = (row["parent_fen"], row["move_uci"])
            self.assertIn(key, kept, "force-seed deleted user move %s" % (row["move_uci"],))
            self.assertEqual(kept[key]["id"], row["id"])
            self.assertEqual(kept[key]["comment"], row["comment"])
            self.assertEqual(kept[key]["is_important"], row["is_important"])
            self.assertEqual(kept[key]["times_practiced"], row["times_practiced"])
            self.assertEqual(kept[key]["times_correct"], row["times_correct"])
            self.assertEqual(kept[key]["interval_days"], row["interval_days"])
        self.assertEqual(kept[(START, "e2e4")]["comment"], "anti-french prep")
        self.assertEqual(kept[(START, "e2e4")]["is_important"], 1)

        # 3. the user's 3.e5 is still THE user move for that position (no generic 3.Nc3 replaced it)
        after_e5 = chess.Board()
        for uci in ("e2e4", "e7e6", "d2d4", "d7d5"):
            after_e5.push_uci(uci)
        prepared = _rows(
            "SELECT move_uci FROM repertoire_moves WHERE parent_fen = ? AND is_user_move = 1",
            (normalize_fen(after_e5.fen()),),
        )
        self.assertEqual([p["move_uci"] for p in prepared], ["e4e5"])

        # 4. the protective guard still works and is a result object too
        skipped = rm.seed_default_repertoires(force=False)
        self.assertIsInstance(skipped, dict)
        self.assertEqual(skipped.get("status"), "skipped")

    # ------------------------------------------------------------------ CODE-3
    def test_ambiguous_prepared_move_lookup_prefers_the_exact_parent(self):
        rm = self.rm
        self.assertEqual(
            rm.add_repertoire_move("white", FEN_NOEP, "b1c3", is_user_move=True)["status"], "success"
        )
        # AUDIT FIXER-T / TRAINER-01 (SETUP ONLY, no assertion changed): the en-passant twin can
        # no longer be produced through the public write path, because a position is identified
        # by its first three FEN tokens and a second prepared move at the same logical position is
        # now refused (`already_prepared_at_transposition`, nothing written). This test exists to
        # prove the *grader* is still deterministic for databases that already hold such a pair,
        # so the twin is written directly - which is exactly that legacy state.
        conn = get_db()
        try:
            board = chess.Board(FEN_EP)
            board.push_uci("g1f3")
            conn.execute(
                """INSERT INTO repertoire_moves
                   (repertoire, parent_fen, fen, move_uci, move_san, is_user_move, created_at)
                   VALUES ('white', ?, ?, 'g1f3', 'Nf3', 1, 0)""",
                (normalize_fen(FEN_EP), normalize_fen(board.fen())),
            )
            conn.commit()
        finally:
            conn.close()
        wrong_row = _rows(
            "SELECT * FROM repertoire_moves WHERE parent_fen = ?", (normalize_fen(FEN_NOEP),)
        )[0]
        right_row = _rows(
            "SELECT * FROM repertoire_moves WHERE parent_fen = ?", (normalize_fen(FEN_EP),)
        )[0]
        self.assertNotEqual(wrong_row["id"], right_row["id"])

        # The player plays the move prepared for THIS position -> must be accepted.
        ok = rm.evaluate_trainer_move("white", FEN_EP, "g1f3")
        self.assertTrue(ok.get("correct"), "exact-parent prepared move was not honoured: %r" % ok)
        self.assertEqual(ok.get("prepared_id"), right_row["id"])
        after_ok = _rows("SELECT * FROM repertoire_moves WHERE id = ?", (right_row["id"],))[0]
        self.assertEqual(after_ok["times_practiced"], 1)
        self.assertEqual(after_ok["times_correct"], 1)
        other = _rows("SELECT * FROM repertoire_moves WHERE id = ?", (wrong_row["id"],))[0]
        self.assertEqual(other["times_practiced"], 0, "the other position's move was mutated")
        self.assertEqual(other["times_correct"], 0)

        # A mistake in this position must only touch THIS position's row.
        bad = rm.evaluate_trainer_move("white", FEN_EP, "b1c3")
        self.assertFalse(bad.get("correct"))
        self.assertEqual(bad.get("prepared_id"), right_row["id"])
        after_bad = _rows("SELECT * FROM repertoire_moves WHERE id = ?", (right_row["id"],))[0]
        other2 = _rows("SELECT * FROM repertoire_moves WHERE id = ?", (wrong_row["id"],))[0]
        self.assertEqual(after_bad["times_practiced"], 2)
        self.assertEqual(other2["times_practiced"], 0,
                         "the not-match branch incremented a move of a different position")

    # ------------------------------------------------------------------ CODE-4
    def test_black_repertoire_tree_uses_the_black_players_personal_stats(self):
        rm = self.rm
        # a white and a black game that both reach the same position, with opposite results
        moves = "e4 e5 Nf3 Nc6 Bc4 Nf6"
        conn = get_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO games (id, speed, rated, played_at, user_color, result, moves_san)"
                " VALUES ('fixa-w', 'rapid', 1, 1, 'white', 'white', ?)", (moves,)
            )
            conn.execute(
                "INSERT OR REPLACE INTO games (id, speed, rated, played_at, user_color, result, moves_san)"
                " VALUES ('fixa-b', 'rapid', 1, 1, 'black', 'black', ?)", (moves,)
            )
            conn.commit()
        finally:
            conn.close()
        rm._user_stats_cache = {}
        rm._user_stats_time = 0

        seen_candidates = []
        seen_stats = []
        orig_stats = self._orig_stats
        orig_cand = self._orig_cand

        def spy_stats(color="white"):
            seen_stats.append(color)
            return orig_stats(color)

        def spy_cand(fen, ratings="2000,2200,2500", color="white"):
            seen_candidates.append(color)
            rm.get_user_position_stats(color)
            return {
                "fen": fen,
                "total_games": 10,
                "moves": [{"san": "Nf3", "uci": "g1f3", "games": 5, "popularity_pct": 50.0, "win_rate": 50.0}],
            }

        rm.get_user_position_stats = spy_stats
        rm.get_candidate_moves_2000 = spy_cand
        try:
            # black repertoire: 1...c5
            after_e4 = chess.Board()
            after_e4.push_uci("e2e4")
            rm.add_repertoire_move("black", after_e4.fen(), "c7c5", is_user_move=True)
            tree = rm.get_repertoire_tree("black")
            black_stats = orig_stats("black")
            white_stats = orig_stats("white")
        finally:
            rm.get_user_position_stats = orig_stats
            rm.get_candidate_moves_2000 = orig_cand
            conn = get_db()
            try:
                conn.execute("DELETE FROM games WHERE id IN ('fixa-w', 'fixa-b')")
                conn.commit()
            finally:
                conn.close()
            rm._user_stats_cache = {}
            rm._user_stats_time = 0

        # the two colours really are distinguishable in this fixture, so the argument matters
        self.assertTrue(black_stats and white_stats)
        self.assertNotEqual(black_stats, white_stats)

        self.assertEqual(tree["repertoire"], "black")
        self.assertTrue(seen_candidates, "the black tree never queried the explorer")
        self.assertEqual(
            sorted(set(seen_candidates)), ["black"],
            "a black repertoire was scored with color=%r" % sorted(set(seen_candidates)),
        )
        self.assertTrue(seen_stats, "the personal stats index was never built")
        self.assertEqual(
            sorted(set(seen_stats)), ["black"],
            "the personal stats were built for color=%r" % sorted(set(seen_stats)),
        )

    # ------------------------------------------------------------------ CODE-5
    def test_explorer_is_fetched_once_per_opponent_position(self):
        rm = self.rm
        # 1.e4 e5 2.Nf3 Nc6  -> opponent positions after 1.e4 and after 2.Nf3
        board = chess.Board()
        for uci, user in [("e2e4", True), ("e7e5", False), ("g1f3", True), ("b8c6", False)]:
            rm.add_repertoire_move("white", board.fen(), uci, is_user_move=user)
            board.push_uci(uci)

        calls = []

        def fake_cand(fen, ratings="2000,2200,2500", color="white"):
            calls.append((fen, color))
            return {
                "fen": fen,
                "total_games": 100,
                "moves": [
                    {"san": "Nf6", "uci": "g8f6", "games": 50, "popularity_pct": 30.0,
                     "win_rate": 50.0},
                    {"san": "d6", "uci": "d7d6", "games": 20, "popularity_pct": 10.0,
                     "win_rate": 50.0},
                ],
            }

        orig = self._orig_cand
        rm.get_candidate_moves_2000 = fake_cand
        try:
            tree = rm.get_repertoire_tree("white")
        finally:
            rm.get_candidate_moves_2000 = orig

        fens = [f for f, _ in calls]
        self.assertEqual(
            len(fens), len(set(fens)),
            "the explorer was fetched more than once for the same position: %r" % (calls,),
        )
        expected = set()
        for row in _rows("SELECT fen FROM repertoire_moves WHERE repertoire='white'"):
            try:
                turn = chess.Board(row["fen"]).turn
            except Exception:
                continue
            if turn == chess.BLACK:
                expected.add(row["fen"])
        self.assertTrue(expected, "fixture did not produce any opponent position")
        self.assertEqual(set(fens), expected)
        self.assertEqual(len(fens), len(expected))
        # the gap list is still produced from the single lookup
        self.assertTrue(any(g["opponent_uci"] == "g8f6" for g in tree["gaps"]))

    # ------------------------------------------------------------------ CODE-6
    def test_get_game_gaps_performs_no_writes_and_persists_no_sentinel(self):
        rm = self.rm
        rm.get_game_gaps = self._orig_gaps  # the real implementation, not the setUp() stub
        rm.get_candidate_moves_2000 = self._orig_cand
        conn = get_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO games (id, speed, rated, played_at, user_color, result, moves_san)"
                " VALUES ('fixa-gapgame', 'rapid', 1, 1, 'white', 'black', 'e4 e5')"
            )
            for ply, (fen, best) in enumerate(
                [(FEN_EP, "g1f3"), (FEN_NOEP, "e2e4")], start=1
            ):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO blunders
                        (id, game_id, move_number, ply, color, fen_before, fen_after, played_move,
                         played_san, best_move, best_san, eval_before, eval_after, cpl_loss,
                         opening_name, eco, opening_games, created_at)
                    VALUES (?, 'fixa-gapgame', ?, ?, 'white', ?, ?, 'g8f6', 'Nf6', ?, 'Nf3', 20, -180,
                            200, 'Test Opening', 'A00', NULL, 0)
                    """,
                    (900000 + ply, ply, ply, fen, fen, best),
                )
            conn.commit()
        finally:
            conn.close()

        lookups = []
        # the explorer is down: every probe fails and answers 0, exactly like a timeout
        rm.get_explorer_game_count = lambda fen, ratings="2000,2200,2500": (
            lookups.append(fen) or 0
        )

        statements = []
        real_get_db = rep_mod.get_db

        def traced_get_db():
            c = real_get_db()

            def _trace(stmt):
                head = stmt.lstrip().split(None, 1)[0].upper() if stmt.strip() else ""
                if head in ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"):
                    statements.append(" ".join(stmt.split())[:80])

            c.set_trace_callback(_trace)
            return c

        rep_mod.get_db = traced_get_db
        try:
            result = rm.get_game_gaps("white", min_games=30)
        finally:
            rep_mod.get_db = real_get_db

        try:
            stored = _rows(
                "SELECT opening_games FROM blunders WHERE id IN (900001, 900002) ORDER BY id"
            )
        finally:
            conn = get_db()
            try:
                conn.execute("DELETE FROM blunders WHERE id IN (900001, 900002)")
                conn.execute("DELETE FROM games WHERE id = 'fixa-gapgame'")
                conn.commit()
            finally:
                conn.close()

        self.assertEqual(statements, [], "get_game_gaps performed writes from a GET")
        self.assertEqual([s["opening_games"] for s in stored], [None, None],
                         "a failed explorer probe was persisted instead of retried")
        self.assertTrue(lookups, "the blunder rows were never resolved at all")
        self.assertNotIn(900001, [g["id"] for g in result["gaps"]])
        self.assertNotIn(900002, [g["id"] for g in result["gaps"]])

    # ------------------------------------------------------------------ CODE-17
    def test_delete_study_keeps_moves_shared_with_another_study(self):
        rm = self.rm
        rm.create_study("Shared A", "white")
        rm.create_study("Shared B", "white")
        # first linked to A, then to B: the move belongs to both studies
        rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True,
                               study_name="Shared A")
        b = chess.Board()
        b.push_uci("e2e4")
        rm.add_repertoire_move("white", b.fen(), "e7e5", is_user_move=False, study_name="Shared A")
        rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True,
                               study_name="Shared B")
        study_b = rm.get_study_by_name("Shared B", "white")

        res = rm.delete_study(study_b["id"], delete_moves=True)
        self.assertEqual(res["status"], "success")

        surviving = _rows("SELECT * FROM repertoire_moves ORDER BY id")
        self.assertEqual(
            sorted(r["move_uci"] for r in surviving), ["e2e4", "e7e5"],
            "a move shared with study 'Shared A' was destroyed with study 'Shared B'",
        )
        e4 = [r for r in surviving if r["move_uci"] == "e2e4"][0]
        self.assertEqual(e4["study_name"], "Shared A",
                         "the surviving move still points at the deleted study")
        self.assertIsNone(rm.get_study_by_name("Shared B", "white"))
        self.assertIsNotNone(rm.get_study_by_name("Shared A", "white"))
        self.assertEqual(
            _rows("SELECT * FROM study_moves WHERE study_id = ?", (study_b["id"],)), []
        )

    def test_delete_study_prunes_the_children_of_the_moves_it_owns(self):
        rm = self.rm
        rm.create_study("Solo", "white")
        board = chess.Board()
        board.push_uci("e2e4")
        rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True,
                               study_name="Solo")
        rm.add_repertoire_move("white", board.fen(), "e7e5", is_user_move=False, study_name="Solo")
        board.push_uci("e7e5")
        rm.add_repertoire_move("white", board.fen(), "g1f3", is_user_move=True, study_name="Solo")
        self.assertEqual(len(_rows("SELECT * FROM repertoire_moves")), 3)

        study = rm.get_study_by_name("Solo", "white")
        res = rm.delete_study(study["id"], delete_moves=True)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["deleted_moves"], 3)
        self.assertEqual(
            _rows("SELECT * FROM repertoire_moves"), [],
            "delete_study left orphaned children in the tree",
        )
        self.assertEqual(_rows("SELECT * FROM study_moves"), [])


if __name__ == "__main__":
    unittest.main()
