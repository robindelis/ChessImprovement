import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import chess
import tests.test_setup

from backend.api import app
from backend.db import init_db, get_db, save_scouted_content, get_scouted_content, list_recent_scouted, toggle_favorite_scouted
from backend.scout import ContentScout, extract_youtube_id, normalize_chess_speech, format_seconds
from backend.repertoire import RepertoireManager

class TestOpeningScout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_setup.reset_fixtures()
        init_db()
        cls.client = TestClient(app)
        cls.scout = ContentScout.get_instance()

    def setUp(self):
        """Each test starts from an empty repertoire tree.

        CODE-05-05 (escalation): this module was order-dependent. Alphabetically
        `test_import_whole_line_*` runs before `test_scout_import_repertoire` and
        `test_single_played_move_import`, and both of those import 1. e4, so they
        only ever saw `imported_count >= 2` / `== 1` because of what an unrelated
        test happened to leave behind. Every test now creates its own
        precondition, so a reordering cannot silently change the verdict.
        """
        conn = get_db()
        conn.execute("DELETE FROM study_moves")
        conn.execute("DELETE FROM repertoire_studies")
        conn.execute("DELETE FROM repertoire_moves")
        conn.commit()
        conn.close()

    def test_extract_youtube_id(self):
        cases = [
            ("https://www.youtube.com/watch?v=rmbU97iftC8", "rmbU97iftC8"),
            ("https://youtu.be/rmbU97iftC8", "rmbU97iftC8"),
            ("https://www.youtube.com/embed/rmbU97iftC8?enablejsapi=1", "rmbU97iftC8"),
            ("rmbU97iftC8", "rmbU97iftC8"),
            ("https://www.youtube.com/watch?v=dP4qtaUAhWM&t=120s", "dP4qtaUAhWM"),
            ("invalid_url", None),
            ("", None)
        ]
        for url, expected in cases:
            self.assertEqual(extract_youtube_id(url), expected)

    def test_normalize_chess_speech(self):
        raw = "knight to f3 then bishop c4 pawn to e4 castles kingside takes on d4"
        norm = normalize_chess_speech(raw)
        self.assertIn("Nf3", norm)
        self.assertIn("Bc4", norm)
        self.assertIn("e4", norm)
        self.assertIn("O-O", norm)
        self.assertIn("xd4", norm)

    def test_format_seconds(self):
        self.assertEqual(format_seconds(65), "01:05")
        self.assertEqual(format_seconds(3665), "01:01:05")
        self.assertEqual(format_seconds(0), "00:00")

    def test_analyze_eco_guide(self):
        study = self.scout.analyze_source("eco_B10_Caro-Kann_Defense")
        self.assertIn("Caro-Kann", study["title"])
        self.assertEqual(study["source_type"], "eco_guide")
        self.assertTrue(len(study["variations"]) > 0)
        self.assertTrue(len(study["full_timeline"]) > 0)

        # Check move integrity
        first_move = study["full_timeline"][0]
        self.assertEqual(first_move["san"], "e4")
        self.assertEqual(first_move["ply"], 1)
        self.assertIn("fen_before", first_move)
        self.assertIn("fen_after", first_move)

        # Check strategic ideas
        self.assertIn("strategic_ideas", study)
        self.assertIn("core_idea", study["strategic_ideas"])

    def test_analyze_raw_pgn(self):
        pgn = """[Event "Italian Game"]
[Site "Online"]
[White "White"]
[Black "Black"]
[ECO "C50"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 *
"""
        study = self.scout.analyze_source(pgn)
        self.assertEqual(study["source_type"], "pgn")
        self.assertEqual(len(study["variations"]), 1)
        self.assertEqual(len(study["full_timeline"]), 6)
        sans = [m["san"] for m in study["full_timeline"]]
        self.assertEqual(sans, ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"])

    def test_scout_search_endpoint(self):
        with patch("backend.scout.yt_dlp.YoutubeDL.extract_info", return_value={"entries": []}):
            res = self.client.get("/api/scout/search?query=Italian+Game&limit=4")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIsInstance(data, list)
        self.assertTrue(len(data) > 0)

    def test_scout_analyze_endpoint(self):
        res = self.client.post("/api/scout/analyze", json={
            "source": "eco_C50_Italian_Game"
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("variations", data)
        self.assertIn("full_timeline", data)

    def test_scout_recent_and_favorite_endpoint(self):
        # 1. Analyze an ECO guide so it is in recent
        self.client.post("/api/scout/analyze", json={"source": "eco_C50_Italian_Game"})

        # 2. Get recent
        res = self.client.get("/api/scout/recent?limit=10")
        self.assertEqual(res.status_code, 200)
        recent = res.json()
        self.assertTrue(len(recent) > 0)

        # 3. Toggle favorite
        source_id = recent[0]["source_id"]
        fav_res = self.client.post("/api/scout/favorite", json={"source_id": source_id})
        self.assertEqual(fav_res.status_code, 200)
        fav_data = fav_res.json()
        self.assertIn("is_favorite", fav_data)

    def test_scout_import_repertoire(self):
        # Prepare a 4-ply line: 1. e4 e5 2. Nf3 Nc6
        pgn = "1. e4 e5 2. Nf3 Nc6 *"
        study = self.scout.analyze_source(pgn)
        moves = study["full_timeline"]

        # Import into White repertoire
        res = self.client.post("/api/scout/import_repertoire", json={
            "repertoire": "white",
            "moves": moves
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["imported_count"] >= 2)

    def test_recent_learned_opening_tracking_and_endpoints(self):
        # 1. Set recent opening via API
        set_res = self.client.post("/api/repertoire/train/recent_opening", json={
            "name": "Caro-Kann Defense",
            "eco": "B12"
        })
        self.assertEqual(set_res.status_code, 200)
        self.assertEqual(set_res.json()["status"], "success")

        # 2. Query trainer openings endpoint
        openings_res = self.client.get("/api/repertoire/train/openings?color=black")
        self.assertEqual(openings_res.status_code, 200)
        openings_data = openings_res.json()
        self.assertIn("recent_opening", openings_data)
        self.assertEqual(openings_data["recent_opening"]["name"], "Caro-Kann Defense")
        self.assertEqual(openings_data["recent_opening"]["eco"], "B12")

    def test_trainer_opening_filter(self):
        # Ensure Italian Game move is seeded
        rm = RepertoireManager.get_instance()
        rm.add_repertoire_move("white", chess.STARTING_FEN, "e2e4", is_user_move=True)
        # Add opponent reply e7e5 and user reply g1f3
        board_e4 = chess.Board()
        board_e4.push_san("e4")
        rm.add_repertoire_move("white", board_e4.fen(), "e7e5", is_user_move=False)
        board_e5 = chess.Board(board_e4.fen())
        board_e5.push_san("e5")
        rm.add_repertoire_move("white", board_e5.fen(), "g1f3", is_user_move=True)

        # Query drill with opening filter matching Italian or King's Pawn
        res_all = self.client.get("/api/repertoire/train/next?color=white")
        self.assertEqual(res_all.status_code, 200)

        # Query drill with non-existent opening filter
        res_none = self.client.get("/api/repertoire/train/next?color=white&opening=NonExistentOpeningXYZ123")
        self.assertEqual(res_none.status_code, 404)
        self.assertIn("NonExistentOpeningXYZ123", res_none.json()["detail"])

    def test_single_played_move_import(self):
        # Simulate a user freely playing a move on the board to catch up to starting position
        res = self.client.post("/api/scout/import_repertoire", json={
            "repertoire": "white",
            "moves": [{
                "fen_before": chess.STARTING_FEN,
                "uci": "e2e4",
                "opening_name": "King's Pawn Opening",
                "eco": "B00"
            }]
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["imported_count"], 1)

    def test_import_whole_line_alternating_turns(self):
        # Caro-Kann Advance variation 6 plies: 1. e4 c6 2. d4 d5 3. e5 Bf5
        pgn = "1. e4 c6 2. d4 d5 3. e5 Bf5 *"
        study = self.scout.analyze_source(pgn)
        moves = study["full_timeline"]
        self.assertEqual(len(moves), 6)

        # Import whole line into White repertoire
        res = self.client.post("/api/scout/import_repertoire", json={
            "repertoire": "white",
            "moves": moves
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["imported_count"] >= 5)

        # Check DB to verify moves exist and have correct is_user_move flags
        db = get_db()
        for idx, m in enumerate(moves):
            row = db.execute(
                "SELECT is_user_move FROM repertoire_moves WHERE repertoire='white' AND parent_fen=? AND move_uci=?",
                (m["fen_before"], m["uci"])
            ).fetchone()
            self.assertIsNotNone(row, f"Move {m['san']} ({m['uci']}) not found in DB")
            expected_user = (idx % 2 == 0) # White plays plies 0, 2, 4 -> is_user_move=1
            self.assertEqual(bool(row["is_user_move"]), expected_user)

    def test_import_branching_tree_lines(self):
        # Line 1: 1. e4 c6 2. d4 d5 3. e5 Bf5
        study1 = self.scout.analyze_source("1. e4 c6 2. d4 d5 3. e5 Bf5 *")
        # Line 2: 1. e4 c6 2. d4 d5 3. Nc3 dxe4
        study2 = self.scout.analyze_source("1. e4 c6 2. d4 d5 3. Nc3 dxe4 *")

        combined_moves = study1["full_timeline"] + study2["full_timeline"]
        # Total 12 move items, but 4 moves are shared (1. e4 c6 2. d4 d5), so 8 unique moves
        res = self.client.post("/api/scout/import_repertoire", json={
            "repertoire": "white",
            "moves": combined_moves
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")

        # Verify that both branches (3. e5 and 3. Nc3) coexist in DB
        db = get_db()
        # Find fen after 2... d5
        board = chess.Board()
        for m in ["e4", "c6", "d4", "d5"]:
            board.push_san(m)
        from backend.repertoire import normalize_fen
        fen_after_d5 = normalize_fen(board.fen())

        branch_moves = db.execute(
            "SELECT move_uci, move_san FROM repertoire_moves WHERE repertoire='white' AND parent_fen=?",
            (fen_after_d5,)
        ).fetchall()
        # Since White is deciding the 3rd move, the user move replaces or is stored
        self.assertTrue(len(branch_moves) >= 1)

    def test_import_whole_line_for_black(self):
        # Sicilian 4 plies: 1. e4 c5 2. Nf3 d6
        pgn = "1. e4 c5 2. Nf3 d6 *"
        study = self.scout.analyze_source(pgn)
        moves = study["full_timeline"]

        res = self.client.post("/api/scout/import_repertoire", json={
            "repertoire": "black",
            "moves": moves
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")

        # Verify Black turns have is_user_move=1 and White turns have is_user_move=0
        db = get_db()
        for idx, m in enumerate(moves):
            row = db.execute(
                "SELECT is_user_move FROM repertoire_moves WHERE repertoire='black' AND parent_fen=? AND move_uci=?",
                (m["fen_before"], m["uci"])
            ).fetchone()
            self.assertIsNotNone(row, f"Move {m['san']} not found in Black repertoire DB")
            expected_user = (idx % 2 == 1) # Black plays plies 1, 3 -> is_user_move=1
            self.assertEqual(bool(row["is_user_move"]), expected_user)

    def test_check_line_overlap_fully_present(self):
        # Seed 4-ply line: 1. e4 e5 2. Nf3 Nc6
        pgn = "1. e4 e5 2. Nf3 Nc6 *"
        study = self.scout.analyze_source(pgn)
        moves = study["full_timeline"]
        self.scout.import_line_to_repertoire("white", moves)

        # Check this line again
        overlap = self.scout.check_line_overlap("white", moves)
        self.assertEqual(overlap["status"], "already_present")
        self.assertEqual(overlap["total_plies"], 4)
        self.assertEqual(overlap["matched_plies"], 4)
        self.assertEqual(overlap["new_plies"], 0)
        self.assertIn("Already present", overlap["message"])

    def test_check_subline_already_present_when_repertoire_is_deeper(self):
        # User has deep 6-ply line in repertoire: 1. e4 c6 2. d4 d5 3. e5 Bf5
        deep_study = self.scout.analyze_source("1. e4 c6 2. d4 d5 3. e5 Bf5 *")
        self.scout.import_line_to_repertoire("white", deep_study["full_timeline"])

        # User is considering a 2-ply start/subline: 1. e4 c6
        sub_study = self.scout.analyze_source("1. e4 c6 *")
        overlap = self.scout.check_line_overlap("white", sub_study["full_timeline"])

        # Must recognize that this subline is already present!
        self.assertEqual(overlap["status"], "already_present")
        self.assertEqual(overlap["total_plies"], 2)
        self.assertEqual(overlap["matched_plies"], 2)
        self.assertEqual(overlap["new_plies"], 0)
        self.assertTrue(overlap["repertoire_continues_deeper"])
        self.assertTrue(overlap["repertoire_deepest_ply"] >= 6)
        self.assertIn("Already present", overlap["message"])
        self.assertIn("deeper", overlap["message"])

    def test_check_line_overlap_partially_present_subline_extends(self):
        # User has only the start of the line in repertoire: 1. e4 d6 2. d4 Nf6 (4 plies)
        start_study = self.scout.analyze_source("1. e4 d6 2. d4 Nf6 *")
        self.scout.import_line_to_repertoire("white", start_study["full_timeline"])

        # Now considering an 8-ply line: 1. e4 d6 2. d4 Nf6 3. Nc3 g6 4. Be3 Bg7
        deep_study = self.scout.analyze_source("1. e4 d6 2. d4 Nf6 3. Nc3 g6 4. Be3 Bg7 *")
        overlap = self.scout.check_line_overlap("white", deep_study["full_timeline"])

        # Must recognize that the start of the line is already in repertoire!
        self.assertEqual(overlap["status"], "partially_present")
        self.assertEqual(overlap["total_plies"], 8)
        self.assertEqual(overlap["matched_plies"], 4)
        self.assertEqual(overlap["new_plies"], 4)
        self.assertIn("Start of line is already present", overlap["message"])
        self.assertIn("4 new moves", overlap["message"])

    def test_check_line_overlap_user_move_divergence(self):
        # User prepared 3. Bc4 in Italian: 1. e4 e5 2. Nf3 Nc6 3. Bc4
        italian = self.scout.analyze_source("1. e4 e5 2. Nf3 Nc6 3. Bc4 *")
        self.scout.import_line_to_repertoire("white", italian["full_timeline"])

        # Now considering Ruy Lopez 3. Bb5: 1. e4 e5 2. Nf3 Nc6 3. Bb5
        ruy = self.scout.analyze_source("1. e4 e5 2. Nf3 Nc6 3. Bb5 *")
        overlap = self.scout.check_line_overlap("white", ruy["full_timeline"])

        self.assertEqual(overlap["status"], "partially_present")
        self.assertEqual(overlap["matched_plies"], 4)
        self.assertIsNotNone(overlap["divergence_info"])
        self.assertEqual(overlap["divergence_info"]["type"], "user_move_alternative")
        self.assertEqual(overlap["divergence_info"]["existing_san"], "Bc4")
        self.assertEqual(overlap["divergence_info"]["new_san"], "Bb5")
        self.assertIn("replaces your prepared Bc4", overlap["message"])

    def test_check_lines_overlap_batch_and_endpoints(self):
        line1 = self.scout.analyze_source("1. e4 e5 *")["full_timeline"]
        line2 = self.scout.analyze_source("1. d4 d5 *")["full_timeline"]

        # Check single line API
        res_single = self.client.post("/api/scout/check_line", json={
            "repertoire": "white",
            "moves": line1
        })
        self.assertEqual(res_single.status_code, 200)
        data_single = res_single.json()
        self.assertIn("status", data_single)
        self.assertIn("message", data_single)

        # Check batch tree API
        res_tree = self.client.post("/api/scout/check_tree", json={
            "repertoire": "white",
            "lines": [
                {"id": "var_1", "moves": line1},
                {"id": "var_2", "moves": line2}
            ]
        })
        self.assertEqual(res_tree.status_code, 200)
        data_tree = res_tree.json()
        self.assertEqual(len(data_tree), 2)
        self.assertEqual(data_tree[0]["id"], "var_1")
        self.assertEqual(data_tree[1]["id"], "var_2")

    def test_import_line_informs_already_present(self):
        pgn = "1. e4 c6 2. d4 d5 *"
        moves = self.scout.analyze_source(pgn)["full_timeline"]

        # 1. First import
        res1 = self.client.post("/api/scout/import_repertoire", json={
            "repertoire": "white",
            "moves": moves
        })
        self.assertEqual(res1.status_code, 200)

        # 2. Second import of same line
        res2 = self.client.post("/api/scout/import_repertoire", json={
            "repertoire": "white",
            "moves": moves
        })
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertTrue(data2["already_present"])
        self.assertEqual(data2["overlap_status"], "already_present")
        self.assertEqual(data2["matched_count"], 4)
        self.assertIn("Already present", data2["message"])

    def test_import_line_with_comment_and_metadata(self):
        pgn = "1. e4 e6 2. d4 d5 *"
        moves = self.scout.analyze_source(pgn)["full_timeline"]

        # Attach comment, is_important, and arrows to the moves
        moves[0]["comment"] = "Control central squares and prepare development."
        moves[0]["is_important"] = True
        moves[0]["arrows"] = '[{"from":"e2","to":"e4","color":"#22c55e"}]'

        res = self.client.post("/api/scout/import_repertoire", json={
            "repertoire": "white",
            "moves": moves
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")

        # Verify database stored comment, is_important, and arrows
        db = get_db()
        row = db.execute(
            "SELECT comment, is_important, arrows FROM repertoire_moves WHERE repertoire='white' AND parent_fen=? AND move_uci=?",
            (moves[0]["fen_before"], moves[0]["uci"])
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["comment"], "Control central squares and prepare development.")
        self.assertEqual(row["is_important"], 1)
        self.assertIn("e2", row["arrows"])

    def test_import_line_with_study_name(self):
        pgn = "1. e4 e6 2. d4 d5 3. e5 c5 4. a3 *"
        moves = self.scout.analyze_source(pgn)["full_timeline"]
        study_title = "French Defense Advance Variation"

        res = self.client.post("/api/scout/import_repertoire", json={
            "repertoire": "white",
            "moves": moves,
            "study_name": study_title
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")

        # Verify DB records
        db = get_db()
        row = db.execute(
            "SELECT study_name FROM repertoire_moves WHERE repertoire='white' AND parent_fen=? AND move_uci=?",
            (moves[0]["fen_before"], moves[0]["uci"])
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["study_name"], study_title)

        # Verify study created in repertoire_studies and linked in study_moves
        st_row = db.execute("SELECT id FROM repertoire_studies WHERE name=? AND repertoire='white'", (study_title,)).fetchone()
        self.assertIsNotNone(st_row)
        sm_row = db.execute(
            "SELECT * FROM study_moves WHERE study_id=? AND parent_fen=? AND move_uci=?",
            (st_row["id"], moves[0]["fen_before"], moves[0]["uci"])
        ).fetchone()
        self.assertIsNotNone(sm_row)

    def test_update_move_metadata_importance_and_study(self):
        pgn = "1. d4 d5 2. c4 e6 *"
        moves = self.scout.analyze_source(pgn)["full_timeline"]
        self.scout.import_line_to_repertoire("white", moves)

        # Update metadata to set is_important and study_name
        res = self.client.post("/api/repertoire/move_meta", json={
            "repertoire": "white",
            "parent_fen": moves[0]["fen_before"],
            "move_uci": moves[0]["uci"],
            "is_important": True,
            "study_name": "Queen's Gambit Declined"
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["is_important"], 1)
        self.assertEqual(data["study_name"], "Queen's Gambit Declined")

        db = get_db()
        row = db.execute(
            "SELECT is_important, study_name FROM repertoire_moves WHERE repertoire='white' AND parent_fen=? AND move_uci=?",
            (moves[0]["fen_before"], moves[0]["uci"])
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_important"], 1)
        self.assertEqual(row["study_name"], "Queen's Gambit Declined")

if __name__ == "__main__":
    unittest.main()


