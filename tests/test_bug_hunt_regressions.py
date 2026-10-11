import tests.test_setup  # noqa: F401 - MUST be first import to isolate SQLite DB

import asyncio
import json
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from backend import api, db, evaluator, repertoire, scout


ROOT = Path(__file__).resolve().parents[1]


class TestBugHuntBackendRegressions(unittest.TestCase):
    def setUp(self):
        db.init_db()
        conn = db.get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM repertoire_moves")
            cursor.execute("DELETE FROM repertoire_studies")
            cursor.execute("DELETE FROM games")
            cursor.execute("DELETE FROM scouted_content")
            conn.commit()
        finally:
            conn.close()
        self.client = TestClient(api.app)

    def test_repertoire_move_replace_existing_via_api(self):
        start_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
        res1 = self.client.post(
            "/api/repertoire/move",
            json={
                "repertoire": "white",
                "parent_fen": start_fen,
                "move_uci": "e2e4",
            },
        )
        self.assertEqual(res1.status_code, 200)
        self.assertIn(res1.json()["status"], ("ok", "saved", "success"))

        # Conflicting user move without replace_existing returns conflict
        res_conflict = self.client.post(
            "/api/repertoire/move",
            json={
                "repertoire": "white",
                "parent_fen": start_fen,
                "move_uci": "d2d4",
            },
        )
        self.assertEqual(res_conflict.status_code, 200)
        self.assertEqual(res_conflict.json()["status"], "conflict")

        # Retrying with replace_existing=True replaces e4 with d4
        res_replace = self.client.post(
            "/api/repertoire/move",
            json={
                "repertoire": "white",
                "parent_fen": start_fen,
                "move_uci": "d2d4",
                "replace_existing": True,
            },
        )
        self.assertEqual(res_replace.status_code, 200)
        self.assertIn(res_replace.json()["status"], ("ok", "replaced", "success"))

        rm = repertoire.RepertoireManager.get_instance()
        tree = rm.get_repertoire_tree("white")["moves_by_parent"]
        norm_start = repertoire.normalize_fen(start_fen)
        self.assertEqual([m["move_san"] for m in tree.get(norm_start, [])], ["d4"])

    def test_import_game_opening_replace_existing_via_api(self):
        rm = repertoire.RepertoireManager.get_instance()
        conn = db.get_db()
        try:
            rm._upsert_game(
                conn.cursor(),
                {
                    "id": "g_conflict_1",
                    "speed": "blitz",
                    "rated": 1,
                    "played_at": 1700000000000,
                    "white_player": "RobiDeli",
                    "black_player": "Opp",
                    "white_rating": 1800,
                    "black_rating": 1800,
                    "user_color": "white",
                    "result": "white",
                    "opening_name": "Queen's Gambit",
                    "eco": "D06",
                    "moves_san": "d4 d5 c4 e6",
                },
            )
            conn.commit()
        finally:
            conn.close()

        start_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
        rm.add_repertoire_move(
            repertoire="white",
            parent_fen=start_fen,
            move_uci="e2e4",
        )

        # First call without replace_existing -> conflict
        r1 = self.client.post(
            "/api/repertoire/import_game_opening",
            json={"game_id": "g_conflict_1", "repertoire": "white", "max_ply": 4},
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["status"], "conflict")

        # Second call with replace_existing=True -> success
        r2 = self.client.post(
            "/api/repertoire/import_game_opening",
            json={
                "game_id": "g_conflict_1",
                "repertoire": "white",
                "max_ply": 4,
                "replace_existing": True,
            },
        )
        self.assertEqual(r2.status_code, 200)
        self.assertIn(r2.json()["status"], ("ok", "success"))
        self.assertGreaterEqual(r2.json()["imported_moves"], 1)

    def test_scout_favorite_on_unextracted_shelf_item(self):
        res1 = self.client.post(
            "/api/scout/favorite",
            json={
                "source_id": "eco_guide_b90",
                "source_type": "eco_guide",
                "title": "Sicilian Najdorf Guide",
                "channel": "Theory Shelf",
                "opening_name": "Sicilian Defense",
            },
        )
        self.assertEqual(res1.status_code, 200)
        self.assertTrue(res1.json()["is_favorite"])

        # Toggling again unbookmarks it
        res2 = self.client.post(
            "/api/scout/favorite",
            json={"source_id": "eco_guide_b90"},
        )
        self.assertEqual(res2.status_code, 200)
        self.assertFalse(res2.json()["is_favorite"])

    def test_scout_import_line_does_not_self_clobber_or_leave_orphans(self):
        # Two conflicting White lines from start position in a single import batch:
        # Line 1: 1. e4 e5 2. Nf3
        # Line 2: 1. d4 d5 2. c4
        fen_start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        fen_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
        fen_e5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
        fen_nf3 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
        fen_d4 = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1"
        fen_d5 = "rnbqkbnr/ppp1pppp/8/3p4/3P4/8/PPP1PPPP/RNBQKBNR w KQkq - 0 2"
        fen_c4 = "rnbqkbnr/ppp1pppp/8/3p4/2PP4/8/PP2PPPP/RNBQKBNR b KQkq - 0 2"

        moves = [
            {"fen_before": fen_start, "uci": "e2e4", "san": "e4", "fen_after": fen_e4},
            {"fen_before": fen_e4, "uci": "e7e5", "san": "e5", "fen_after": fen_e5},
            {"fen_before": fen_e5, "uci": "g1f3", "san": "Nf3", "fen_after": fen_nf3},
            {"fen_before": fen_start, "uci": "d2d4", "san": "d4", "fen_after": fen_d4},
            {"fen_before": fen_d4, "uci": "d7d5", "san": "d5", "fen_after": fen_d5},
            {"fen_before": fen_d5, "uci": "c2c4", "san": "c4", "fen_after": fen_c4},
        ]
        cs = scout.ContentScout.get_instance()
        result = cs.import_line_to_repertoire("white", moves, study_name="Test Study")
        self.assertEqual(result["imported_count"], 3)
        rm = repertoire.RepertoireManager.get_instance()
        tree = rm.get_repertoire_tree("white")["moves_by_parent"]
        # 1. e4 should be preserved (not overwritten by 1. d4), and 1...d5 / 2. c4 should not be orphaned in DB
        self.assertEqual([m["move_san"] for m in tree.get(repertoire.normalize_fen(fen_start), [])], ["e4"])
        self.assertNotIn(repertoire.normalize_fen(fen_d4), tree)
        self.assertNotIn(repertoire.normalize_fen(fen_d5), tree)

    def test_evaluator_stream_eval_replays_cached_pv(self):
        fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
        ev = evaluator.AsyncEngineEvaluator.get_instance()
        expected_pv = [
            {"uci": "e7e5", "san": "e5"},
            {"uci": "g1f3", "san": "Nf3"},
            {"uci": "b8c6", "san": "Nc6"},
        ]
        ev._set_cached_eval(
            fen,
            {
                "depth": 18,
                "score_cp": 25,
                "mate": None,
                "best_move": "e7e5",
                "best_san": "e5",
                "pv": expected_pv,
            },
        )

        async def _consume_first():
            gen = ev.stream_eval(fen, max_depth=16)
            first = await gen.__anext__()
            await gen.aclose()
            return first

        chunk = asyncio.run(_consume_first())
        data_line = next(line for line in chunk.splitlines() if line.startswith("data: "))
        payload = json.loads(data_line[len("data: "):].strip())
        self.assertTrue(payload.get("cached"))
        self.assertEqual(payload.get("pv"), expected_pv)


class TestFrontendBugHuntAndLayoutInvariants(unittest.TestCase):
    def test_board_js_viewport_fit_and_single_listener(self):
        src = (ROOT / "frontend" / "board.js").read_text(encoding="utf-8")
        self.assertIn("computeIdealBoardSize", src)
        self.assertIn("Math.max(300, Math.min(620, Math.round(optimal)))", src)
        # Ensure we don't attach duplicate addEventListener on .btn-board-smaller/.btn-board-larger/.btn-board-fit
        self.assertNotIn("document.querySelectorAll('.btn-board-smaller').forEach", src)

    def test_repertoire_builder_fixes_present(self):
        src = (ROOT / "frontend" / "repertoire_builder.js").read_text(encoding="utf-8")
        self.assertIn("getMovesForParent(parentFen)", src)
        self.assertIn("isUserTurnAtFen(fen)", src)
        self.assertIn("_reconstructHistoryToFen(targetFen", src)
        self.assertIn("_seedLineTreeFromStudy(studyName)", src)
        self.assertIn("replace_existing: true", src)
        self.assertIn("data-uci", src)

    def test_repertoire_trainer_fixes_present(self):
        src = (ROOT / "frontend" / "repertoire_trainer.js").read_text(encoding="utf-8")
        self.assertIn("_normalizeGameOutcome(rawResult, userColor)", src)
        self.assertIn("drillsSection.style.display = isDeviations ? 'none' : 'flex'", src)
        self.assertIn("deviationsView.style.display = isDeviations ? 'flex' : 'none'", src)
        self.assertIn("noteBox.style.minHeight = '54px'", src)

    def test_scout_and_app_fixes_present(self):
        scout_src = (ROOT / "frontend" / "scout.js").read_text(encoding="utf-8")
        self.assertIn("window.openingScout = window.videoScout", scout_src)
        self.assertIn("builder.addressGap({ parent_fen: fen, opponent_uci: uci })", scout_src)
        app_src = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("inputDefaultGap", app_src)
        self.assertIn("gap_threshold", app_src)


if __name__ == "__main__":
    unittest.main()
