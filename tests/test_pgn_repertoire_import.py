"""
Unit and integration tests for PGN repertoire import (Chessbook, Lichess studies, PGN trees).
Tests tree traversal, recursive annotation variations (RAVs), color auto-detection,
study organization, conflict resolution, and REST API boundaries.
"""
import os
import unittest
from fastapi.testclient import TestClient

import tests.test_setup  # noqa: F401 - redirects CHESS_DB_PATH at an isolated test DB
from backend.db import init_db, get_db
from backend.repertoire import RepertoireManager
from backend.api import app


SAMPLE_WHITE_PGN = """
[Event "White Repertoire"]
[Site "chessbook.com"]
[Round "N/A"]
[White "RobiDeli"]
[Black "N/A"]
[Result "*"]

1. e4 e5 (1... c5 2. Nf3 d6 (2... Nc6 3. Bb5 {Moscow variation}) 3. d4 cxd4 4. Nxd4) (1... e6 2. d4 d5 3. e5) 2. Nf3 Nc6 3. Bc4 Bc5 4. c3 Nf6 5. d4 exd4 6. cxd4 Bb4+ 7. Bd2 *
"""

SAMPLE_BLACK_PGN = """
[Event "Black Repertoire"]
[Site "chessbook.com"]
[Round "N/A"]
[White "N/A"]
[Black "RobiDeli"]
[Result "*"]

1. e4 (1. d4 Nf6 2. c4 e6 3. Nc3 Bb4) c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6 *
"""

SAMPLE_MULTI_CHAPTER_PGN = """
[Event "Italian Game"]
[Site "chessbook.com"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. c3 Nf6 *

[Event "Sicilian Defense"]
[Site "chessbook.com"]

1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6 *
"""


class TestPgnRepertoireImport(unittest.TestCase):
    def setUp(self):
        init_db()
        self.rm = RepertoireManager.get_instance()
        self.client = TestClient(app)

    def test_preview_white_repertoire(self):
        """Preview correctly auto-detects White and extracts move counts."""
        res = self.rm.preview_pgn_repertoire(SAMPLE_WHITE_PGN)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["detected_color"], "white")
        self.assertEqual(res["game_count"], 1)
        self.assertGreater(res["total_moves"], 10)
        self.assertGreater(res["user_moves"], 5)
        self.assertGreater(res["opponent_moves"], 5)
        self.assertGreater(res["max_depth"], 5)

    def test_preview_black_repertoire(self):
        """Preview correctly auto-detects Black and extracts move counts."""
        res = self.rm.preview_pgn_repertoire(SAMPLE_BLACK_PGN)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["detected_color"], "black")
        self.assertEqual(res["game_count"], 1)
        self.assertGreater(res["total_moves"], 5)
        self.assertGreater(res["user_moves"], 2)

    def test_preview_multi_chapter_pgn(self):
        """Preview parses multiple chapters with titles and move counts."""
        res = self.rm.preview_pgn_repertoire(SAMPLE_MULTI_CHAPTER_PGN)
        self.assertEqual(res["game_count"], 2)
        titles = [c["title"] for c in res["chapters"]]
        self.assertIn("Italian Game", titles)
        self.assertIn("Sicilian Defense", titles)

    def test_preview_empty_or_invalid_raises(self):
        """Empty or invalid PGN text raises ValueError."""
        with self.assertRaises(ValueError):
            self.rm.preview_pgn_repertoire("")
        with self.assertRaises(ValueError):
            self.rm.preview_pgn_repertoire("not a pgn at all")

    def test_import_white_repertoire_tree(self):
        """Imports White variation tree and checks database state."""
        res = self.rm.import_pgn_repertoire(
            pgn_text=SAMPLE_WHITE_PGN,
            repertoire="white",
            study_name="Italian & Sicilian",
            replace_conflicts=True,
            clean_repertoire=True,
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["repertoire"], "white")
        self.assertGreater(res["imported_moves"], 10)

        # Check DB moves
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT count(*) as total, sum(is_user_move) as user_moves FROM repertoire_moves WHERE repertoire = 'white'")
        row = c.fetchone()
        self.assertGreater(row["total"], 10)
        self.assertGreater(row["user_moves"], 5)

        # 1. e4 must be prepared
        c.execute("SELECT move_uci, is_user_move FROM repertoire_moves WHERE parent_fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -' AND repertoire = 'white'")
        start_move = c.fetchone()
        self.assertIsNotNone(start_move)
        self.assertEqual(start_move["move_uci"], "e2e4")
        self.assertEqual(start_move["is_user_move"], 1)

        # 1... e5, 1... c5, 1... e6 must be opponent replies
        c.execute("SELECT move_uci, is_user_move FROM repertoire_moves WHERE parent_fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -' AND repertoire = 'white'")
        replies = [dict(r) for r in c.fetchall()]
        self.assertEqual(len(replies), 3)
        for rep in replies:
            self.assertEqual(rep["is_user_move"], 0)

    def test_import_split_chapters_creates_studies(self):
        """Multi-chapter PGN with split_chapters_as_studies creates named studies."""
        res = self.rm.import_pgn_repertoire(
            pgn_text=SAMPLE_MULTI_CHAPTER_PGN,
            repertoire="white",
            split_chapters_as_studies=True,
            clean_repertoire=True,
        )
        self.assertEqual(res["status"], "success")
        studies = self.rm.get_studies("white")
        study_names = {s["name"] for s in studies}
        self.assertIn("Italian Game", study_names)
        self.assertIn("Sicilian Defense", study_names)

    def test_conflict_handling_replace_vs_keep(self):
        """Tests that replace_conflicts=True overwrites while replace_conflicts=False preserves."""
        # 1. Seed initial repertoire with 1. d4
        self.rm.clear_repertoire_database("white")
        self.rm.add_repertoire_move(
            repertoire="white",
            parent_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -",
            move_uci="d2d4",
            is_user_move=True,
        )

        # 2. Import PGN with replace_conflicts=False -> 1. d4 should survive
        self.rm.import_pgn_repertoire(
            pgn_text=SAMPLE_WHITE_PGN,
            repertoire="white",
            replace_conflicts=False,
            clean_repertoire=False,
        )
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT move_uci FROM repertoire_moves WHERE parent_fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -' AND is_user_move = 1")
        row = c.fetchone()
        self.assertEqual(row["move_uci"], "d2d4")

        # 3. Import PGN with replace_conflicts=True -> 1. e4 should overwrite 1. d4
        self.rm.import_pgn_repertoire(
            pgn_text=SAMPLE_WHITE_PGN,
            repertoire="white",
            replace_conflicts=True,
            clean_repertoire=False,
        )
        c.execute("SELECT move_uci FROM repertoire_moves WHERE parent_fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -' AND is_user_move = 1")
        row = c.fetchone()
        self.assertEqual(row["move_uci"], "e2e4")

    def test_clean_repertoire_mode(self):
        """clean_repertoire=True wipes pre-existing moves before importing."""
        # Seed 1. d4
        self.rm.add_repertoire_move(
            repertoire="white",
            parent_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -",
            move_uci="d2d4",
            is_user_move=True,
        )
        # Import with clean_repertoire=True
        res = self.rm.import_pgn_repertoire(
            pgn_text=SAMPLE_WHITE_PGN,
            repertoire="white",
            clean_repertoire=True,
        )
        self.assertEqual(res["status"], "success")
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT move_uci FROM repertoire_moves WHERE parent_fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -' AND is_user_move = 1")
        row = c.fetchone()
        self.assertEqual(row["move_uci"], "e2e4")

    def test_api_endpoints_preview_and_import(self):
        """Tests the REST API routes POST /api/repertoire/preview_pgn and /api/repertoire/import_pgn."""
        # 1. Preview API
        prev_res = self.client.post("/api/repertoire/preview_pgn", json={"pgn_text": SAMPLE_WHITE_PGN})
        self.assertEqual(prev_res.status_code, 200)
        prev_data = prev_res.json()
        self.assertEqual(prev_data["detected_color"], "white")
        self.assertGreater(prev_data["total_moves"], 10)

        # 2. Bad preview payload
        bad_res = self.client.post("/api/repertoire/preview_pgn", json={"pgn_text": ""})
        self.assertEqual(bad_res.status_code, 400)

        # 3. Import API
        imp_res = self.client.post("/api/repertoire/import_pgn", json={
            "pgn_text": SAMPLE_BLACK_PGN,
            "repertoire": "black",
            "study_name": "Sicilian Najdorf",
            "replace_conflicts": True,
            "clean_repertoire": True
        })
        self.assertEqual(imp_res.status_code, 200)
        imp_data = imp_res.json()
        self.assertEqual(imp_data["status"], "success")
        self.assertEqual(imp_data["repertoire"], "black")
        self.assertGreater(imp_data["imported_moves"], 5)


if __name__ == "__main__":
    unittest.main()
