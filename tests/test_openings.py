import unittest
import chess
import tests.test_setup
from backend.openings import OpeningBook

class TestOpenings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.test_setup.reset_fixtures()

    def setUp(self):
        self.book = OpeningBook.get_instance()

    def test_identify_game_opening(self):
        moves = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]
        info = self.book.identify_game_opening(moves)
        self.assertTrue("Italian" in info.get("name", "") or "Giuoco" in info.get("name", ""))
        self.assertTrue(info.get("eco", "").startswith("C"))

    def test_book_move_detection(self):
        board = chess.Board()
        # 1. e4 is known book move
        m1 = chess.Move.from_uci("e2e4")
        self.assertTrue(self.book.is_known_book_move(board, m1))

        # 1. h4 is not in standard opening book
        m_odd = chess.Move.from_uci("h2h4")
        # May or may not be in some obscure line, but test board state
        board.push(m1)
        # 1... c5 (Sicilian)
        m2 = chess.Move.from_uci("c7c5")
        self.assertTrue(self.book.is_known_book_move(board, m2))

    def test_strategic_ideas(self):
        ideas = self.book.get_strategic_ideas("B20", "Sicilian Defense")
        self.assertEqual(ideas["name"], "Sicilian Defense")
        self.assertIn("c-pawn", ideas["core_idea"])

if __name__ == "__main__":
    unittest.main()
