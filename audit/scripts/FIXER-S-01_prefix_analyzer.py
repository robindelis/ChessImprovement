import os
import time
import json
import re
import threading
import chess
import chess.engine
from typing import Any, Dict, List, Optional, Sequence, Tuple
from backend.db import get_db, get_setting
from backend.evaluator import MATE_SCORE_CP, get_sync_engine_pool
from backend.openings import OpeningBook

STOCKFISH_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "stockfish", "stockfish-linux-x86-64-universal"
)

#: `MATE_SCORE_CP` (imported from `backend.evaluator`) is the saturated
#: centipawn stand-in written to `blunders.eval_before` / `eval_after` when the
#: engine returned a **mate** score.  Sharing the evaluator's constant keeps the
#: SSE eval bar and the blunder table on one sentinel, and a mate is never
#: stored as `10000 - mate` (which produced a different number for every mate
#: distance -- 9995, 9998, 9999, 9901, ...).

#: A single move cannot lose more than "one mate" worth of evaluation, so
#: `cpl_loss` is clamped to this.  Pre-fix a mate-in-5 -> mate-in-1 collapse
#: stored 19994 and permanently owned the top of `ORDER BY cpl_loss DESC`
#: (audit SYNC-06, "ranking pollution").
MAX_CPL_LOSS = MATE_SCORE_CP

#: Natural key of a blunder row (audit SYNC-15).  `blunders` has no unique index
#: -- that half of the fix belongs to `backend/db.py` (see the FIXER-P0 hand-off)
#: -- so the write path implements the upsert itself and stays correct either way.
BLUNDER_NATURAL_KEY: Tuple[str, str, str] = ("game_id", "ply", "played_move")

#: Written only when the schema actually has them (hand-off to FIXER-P0:
#: `ALTER TABLE blunders ADD COLUMN eval_before_mate INTEGER DEFAULT NULL`
#:  and the same for `eval_after_mate`).  Before that migration the columns are
#: skipped, so this module works unchanged against the current schema.
_OPTIONAL_MATE_COLUMNS: Tuple[str, str] = ("eval_before_mate", "eval_after_mate")

#: Column -> key in the blunder dict.  Single source of truth for both the
#: INSERT and the UPDATE so the two can never drift apart.  The keys in
#: ``_OPTIONAL_BLUNDER_KEYS`` are read with ``.get()`` exactly as the pre-fix
#: code did; every other key is required, so a malformed row fails loudly and
#: rolls the whole batch back.
_BLUNDER_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("game_id", "game_id"),
    ("move_number", "move_number"),
    ("ply", "ply"),
    ("color", "color"),
    ("fen_before", "fen_before"),
    ("fen_after", "fen_after"),
    ("played_move", "played_move"),
    ("played_san", "played_san"),
    ("best_move", "best_move"),
    ("best_san", "best_san"),
    ("eval_before", "eval_before"),
    ("eval_after", "eval_after"),
    ("cpl_loss", "cpl_loss"),
    ("opening_name", "opening_name"),
    ("eco", "eco"),
    ("is_book_move", "is_book_move"),
    ("refutation_pv", "refutation_pv"),
    ("best_pv", "best_pv"),
    ("explanation", "explanation"),
    ("strategic_idea", "strategic_idea"),
    ("category", "category"),
    ("opening_games", "opening_games"),
    ("created_at", "created_at"),
    ("eval_before_mate", "eval_before_mate"),
    ("eval_after_mate", "eval_after_mate"),
)

#: Serialises the write path so two analyses of the same game running in
#: different threads cannot both miss the existing row and both insert it.
_SAVE_LOCK = threading.RLock()

#: Blunder-dict keys that may legitimately be absent from a caller's row.
_OPTIONAL_BLUNDER_KEYS = frozenset({"best_pv", "opening_games"})


def normalize_score(score: Any, color: Optional[chess.Color] = None) -> Tuple[int, Optional[int]]:
    """Split an engine score into ``(centipawns, mate_distance)``.

    ``mate_distance`` comes from the score's own ``mate`` field and is the
    **only** signal that a score is a mate -- never a magnitude test on
    centipawns (audit SYNC-06: a plain ``-95.00`` evaluation was announced to
    the user as "Checkmate in 1", and every mate distance collapsed to
    ``10000 - mate``, i.e. 9995 / 9999 / 9998 / 9901 ... for different mates).

    Units and sign, verified against the real Stockfish 19 binary in
    ``audit/scripts/FIXER-K-01_materepro.py`` (FENs are in that file):

    * the magnitude is the number of **moves** until the mate -- the same value
      the engine prints in its ``info string``, *not* a ply count.  A mate that
      lands on the opponent's very next move is ``1`` (Fool's mate, black to
      move); one that lands on the opponent's second move is ``2``.
    * negative means the score's own colour is **getting mated**
      (``chess.engine.Score.mate()``'s documented sign), positive means that
      colour is delivering the mate.

    A mate is saturated to ``+/-MATE_SCORE_CP`` rather than to
    ``+/-MATE_SCORE_CP - mate``, so the stored value is one stable sentinel.
    ``mate == 0`` is the terminal case python-chess cannot disambiguate (it
    folds "checkmated" and "MateGiven" together); the positive sentinel is used
    for it because an already-finished position can never reach this code path
    from ``analyze_game_openings`` (the game is still running there).
    """
    if color is not None and hasattr(score, "pov"):
        score = score.pov(color)
    mate = score.mate() if hasattr(score, "mate") else None
    if mate is None:
        raw = score.score() if hasattr(score, "score") else None
        if raw is None:
            # `MateGiven` -- python-chess folds "already won" into mate 0.
            return MATE_SCORE_CP, 0
        return int(raw), None
    return (MATE_SCORE_CP if mate >= 0 else -MATE_SCORE_CP), int(mate)


def mate_distance_moves(mate: Optional[int]) -> int:
    """A mate distance in moves for display (``0`` means "mated right now")."""
    if mate is None:
        return 0
    return max(1, abs(int(mate)))


def describe_score_mate(mate: Optional[int]) -> str:
    """Mate-aware phrase for an engine score, or ``""`` when it is not a mate.

    ``mate`` follows :func:`normalize_score` (negative = the scoring side is
    getting mated, magnitude = moves to mate).  A ``None`` mate distance means
    "ordinary centipawn evaluation" and yields ``""`` -- there is no magnitude
    anywhere in this function that could turn ``-9500`` into a mate.
    """
    if mate is None:
        return ""
    moves = mate_distance_moves(mate)
    if mate < 0:
        return f"allows checkmate in {moves}"
    if mate == 0:
        return "checkmate is already on the board"
    return f"reaches a forced mate in {moves} for you"


def centipawn_loss(cp_before: int, cp_after: int) -> int:
    """Centipawn loss of a single move, clamped to ``[0, MAX_CPL_LOSS]``."""
    return max(0, min(int(cp_before) - int(cp_after), MAX_CPL_LOSS))


class GameAnalyzer:
    """Opening-blunder analyzer.

    Engine lifecycle note (audit CODE-02-09): this class no longer spawns its
    own Stockfish process.  Engines come from the process-wide bounded pool in
    ``backend.evaluator`` and are handed back to it by :meth:`close`, so N
    concurrent requests can never produce N processes.  ``close()`` keeps its
    original contract (releases the engine and makes the instance reusable).
    """

    def __init__(self, stockfish_path: str = STOCKFISH_PATH):
        self.stockfish_path = stockfish_path
        self.opening_book = OpeningBook.get_instance()
        self._engine = None
        self._pool = None

    def _get_engine(self):
        if self._engine is None:
            pool = get_sync_engine_pool(self.stockfish_path)
            self._pool = pool
            # Raises FileNotFoundError (unchanged legacy contract) when the
            # binary is missing, or EngineBusy when the pool stays saturated.
            self._engine = pool.acquire()
        return self._engine

    def close(self):
        if self._engine is not None:
            engine, pool = self._engine, self._pool
            self._engine = None
            self._pool = None
            try:
                if pool is not None:
                    pool.release(engine)
                else:
                    engine.quit()
            except Exception:
                pass

    def _get_line_details(self, board: chess.Board, pv_moves: List[chess.Move], max_moves: int = 6) -> Tuple[str, List[Dict]]:
        """Converts an engine PV into clean SAN notation and structured engine move steps."""
        temp_board = board.copy()
        san_parts = []
        structured_moves = []
        for i, m in enumerate(pv_moves[:max_moves]):
            try:
                move_num = temp_board.fullmove_number
                is_white = temp_board.turn == chess.WHITE
                color = "white" if is_white else "black"
                san = temp_board.san(m)
                is_check = temp_board.gives_check(m)
                is_capture = temp_board.is_capture(m)

                move_str = f"{move_num}. {san}" if is_white else (f"{move_num}... {san}" if i == 0 else san)
                san_parts.append(move_str)

                structured_moves.append({
                    "from": chess.square_name(m.from_square),
                    "to": chess.square_name(m.to_square),
                    "uci": m.uci(),
                    "san": san,
                    "move_number": move_num,
                    "color": color,
                    "notation": f"{move_num}. {san}" if is_white else f"{move_num}... {san}",
                    "is_check": is_check,
                    "is_capture": is_capture
                })
                temp_board.push(m)
            except Exception:
                break
        return " ".join(san_parts), structured_moves

    def _generate_tactical_explanation(
        self,
        board_before: chess.Board,
        played_move: chess.Move,
        best_move: chess.Move,
        refutation_moves: List[chess.Move],
        cpl_loss: int,
        opening_name: str,
        score_after: int = 0,
        mate_after: Optional[int] = None,
        mate_before: Optional[int] = None
    ) -> Tuple[str, str, str]:
        """Provides objective engine evaluation details without arbitrary commentary.

        ``mate_after`` / ``mate_before`` are the engine's mate distances **in
        moves** from the point of view of the player who just moved (negative =
        that player is getting mated); see :func:`normalize_score`.  They are the
        only thing that can make this method claim a checkmate: no centipawn
        value is ever compared against a threshold (audit SYNC-06 -- the pre-fix
        ``score_after <= -9000`` test called a plain ``-95.00`` "Checkmate in 1").
        """
        category = "Blunder" if cpl_loss >= 200 else ("Mistake" if cpl_loss >= 100 else "Inaccuracy")

        mate_phrase = describe_score_mate(mate_after)
        if mate_phrase:
            why_bad = f"Eval loss: {mate_phrase}."
        else:
            why_bad = (f"Eval loss: {cpl_loss} centipawns "
                       f"({score_after / 100.0:+.2f} pawns, no forced mate).")
        if mate_before is not None and mate_before > 0:
            why_bad += (f" You had a forced mate in "
                        f"{mate_distance_moves(mate_before)} for you before the move.")
        strategic_idea = f"Engine top choice: {board_before.san(best_move)}."
        return why_bad, strategic_idea, category

    def analyze_game_openings(self, game_dict: Dict) -> List[Dict]:
        """Analyzes opening moves of a game, detects blunders, and stores them in the database."""
        game_id = game_dict.get("id")
        user_color = "white" if game_dict.get("user_color") == "white" else "black"
        
        # Check settings
        max_ply = max(1, int(get_setting("max_opening_ply", "24"))) # default 12 moves (24 plies)
        blunder_thresh = max(1, int(get_setting("blunder_threshold_cp", "150"))) # default 150 cp

        moves_str = game_dict.get("moves", "")
        if not moves_str:
            return []

        moves_list = moves_str.split()
        board = chess.Board()
        engine = self._get_engine()

        # Identify overall opening from book
        opening_info = self.opening_book.identify_game_opening(moves_list)
        opening_dict = game_dict.get("opening")
        if not isinstance(opening_dict, dict):
            opening_dict = {}
        opening_name = opening_dict.get("name") or opening_info.get("name") or "Standard Chess Opening"
        eco = opening_dict.get("eco") or opening_info.get("eco") or "A00"

        detected_blunders = []

        # Analyze each ply within opening threshold
        for ply, san in enumerate(moves_list[:max_ply]):
            if not san:
                continue
            clean_san = re.sub(r"[?!#+]", "", san).strip()
            if not clean_san:
                continue

            is_white = (ply % 2 == 0)
            is_user_turn = (is_white and user_color == "white") or (not is_white and user_color == "black")

            try:
                move = board.parse_san(clean_san)
            except Exception:
                try:
                    move = chess.Move.from_uci(clean_san)
                    if move not in board.legal_moves:
                        break
                except Exception:
                    break

            fen_before = board.fen()
            is_book = self.opening_book.is_known_book_move(board, move)

            # If user's turn, we evaluate carefully
            if is_user_turn:
                # 1. Evaluate position before move
                info_before = engine.analyse(board, chess.engine.Limit(depth=12))
                score_before, mate_before = normalize_score(info_before["score"], board.turn)
                best_move = info_before["pv"][0] if "pv" in info_before and len(info_before["pv"]) > 0 else move
                best_san = board.san(best_move)

                # Skip if position was already completely losing before user moved (BUG-D05)
                if score_before < -200:
                    board.push(move)
                    continue

                # Push the played move
                board.push(move)
                fen_after = board.fen()

                # 2. Evaluate position after move (from perspective of user who just moved)
                info_after = engine.analyse(board, chess.engine.Limit(depth=12))
                score_after, mate_after = normalize_score(info_after["score"], not board.turn)

                # Centipawn loss.  Bounded on both sides: a mate is stored as the
                # +/-MATE_SCORE_CP sentinel, so the raw difference of two
                # sentinels would be 20000 and would mean nothing (audit SYNC-06).
                cpl_loss = centipawn_loss(score_before, score_after)

                # If CPL loss meets or exceeds blunder threshold
                if cpl_loss >= blunder_thresh:
                    # 1. Extract opponent's refutation continuation after the blunder
                    refutation_pv = info_after.get("pv", [])
                    pv_str, structured_refutation = self._get_line_details(board, refutation_pv, max_moves=6)

                    # 2. Extract engine continuation after the best move
                    board_best = chess.Board(fen_before)
                    board_best.push(best_move)
                    info_best = engine.analyse(board_best, chess.engine.Limit(depth=12))
                    best_pv = info_best.get("pv", [])
                    _, structured_best_line = self._get_line_details(board_best, best_pv, max_moves=6)

                    # Generate explanations
                    why_bad, strategic_idea, category = self._generate_tactical_explanation(
                        chess.Board(fen_before),
                        move,
                        best_move,
                        refutation_pv,
                        cpl_loss,
                        opening_name,
                        score_after=score_after,
                        mate_after=mate_after,
                        mate_before=mate_before
                    )

                    blunder_data = {
                        "game_id": game_id,
                        "move_number": (ply // 2) + 1,
                        "ply": ply + 1,
                        "color": user_color,
                        "fen_before": fen_before,
                        "fen_after": fen_after,
                        "played_move": move.uci(),
                        "played_san": clean_san,
                        "best_move": best_move.uci(),
                        "best_san": best_san,
                        "eval_before": score_before,
                        "eval_after": score_after,
                        # Mate distance in moves from the user's point of view
                        # (None = not a mate).  Written to the DB only once the
                        # schema has the columns -- see FIXER-P0 hand-off.
                        "eval_before_mate": mate_before,
                        "eval_after_mate": mate_after,
                        "cpl_loss": cpl_loss,
                        "opening_name": opening_name,
                        "eco": eco,
                        "is_book_move": 1 if is_book else 0,
                        "refutation_pv": json.dumps(structured_refutation),
                        "best_pv": json.dumps(structured_best_line),
                        "explanation": why_bad,
                        "strategic_idea": strategic_idea,
                        "category": category,
                        "created_at": int(time.time())
                    }
                    detected_blunders.append(blunder_data)
            else:
                board.push(move)

        return detected_blunders

    @staticmethod
    def _schema_mate_columns(cursor) -> Tuple[str, ...]:
        """Mate columns present in the current schema (empty before the migration)."""
        try:
            present = {row[1] for row in cursor.execute("PRAGMA table_info(blunders)").fetchall()}
        except Exception:
            return ()
        return tuple(c for c in _OPTIONAL_MATE_COLUMNS if c in present)

    def _upsert_blunder(self, cursor, b: Dict, mate_columns: Sequence[str]) -> Tuple[int, bool]:
        """INSERT-or-UPDATE one blunder on its natural key. Returns (id, created)."""
        names: List[str] = []
        values: List[Any] = []
        for column, key in _BLUNDER_COLUMNS:
            if column in _OPTIONAL_MATE_COLUMNS and column not in mate_columns:
                continue
            names.append(column)
            values.append(
                b.get(key)
                if (key in _OPTIONAL_BLUNDER_KEYS or column in _OPTIONAL_MATE_COLUMNS)
                else b[key]
            )

        cursor.execute(
            "SELECT id FROM blunders WHERE game_id = ? AND ply = ? AND played_move = ? ORDER BY id LIMIT 1",
            (b["game_id"], b["ply"], b["played_move"])
        )
        existing = cursor.fetchone()
        if existing is not None:
            updatable = [(n, v) for n, v in zip(names, values) if n not in BLUNDER_NATURAL_KEY]
            cursor.execute(
                "UPDATE blunders SET " + ", ".join(f"{n} = ?" for n, _ in updatable) + " WHERE id = ?",
                [v for _, v in updatable] + [int(existing["id"])]
            )
            return int(existing["id"]), False

        cursor.execute(
            "INSERT INTO blunders (" + ", ".join(names) + ") VALUES (" + ", ".join("?" * len(names)) + ")",
            values
        )
        return int(cursor.lastrowid), True

    @staticmethod
    def _collapse_duplicate_rows(cursor, game_id: Optional[str]) -> int:
        """Collapse pre-existing duplicate rows of one game, keeping the best copy.

        Rows that duplicated a natural key before this fix existed are merged
        here so the table converges on one row per `(game_id, ply, played_move)`.
        The survivor is the copy with the most drill practice, so collapsing can
        never destroy the user's spaced-repetition progress.  Returns the number
        of removed rows.
        """
        if game_id is None:
            return 0
        cursor.execute(
            "SELECT b.id AS id, b.ply AS ply, b.played_move AS played_move, "
            "COALESCE(d.attempts, 0) AS attempts, COALESCE(d.successes, 0) AS successes "
            "FROM blunders b LEFT JOIN drill_reviews d ON d.blunder_id = b.id "
            "WHERE b.game_id = ? "
            "ORDER BY b.ply, b.played_move, attempts DESC, successes DESC, b.id",
            (game_id,)
        )
        seen = set()
        doomed: List[int] = []
        for row in cursor.fetchall():
            key = (row["ply"], row["played_move"])
            if key in seen:
                doomed.append(int(row["id"]))
            else:
                seen.add(key)
        for row_id in doomed:
            cursor.execute("DELETE FROM drill_reviews WHERE blunder_id = ?", (row_id,))
            cursor.execute("DELETE FROM blunders WHERE id = ?", (row_id,))
        return len(doomed)

    def save_blunders_to_db(self, blunders: List[Dict]) -> Dict[str, int]:
        """Persist one game's blunders idempotently, in a single transaction.

        Audit SYNC-15: this was a bare ``INSERT``, so re-analysing a game appended
        a second copy of every row -- the only thing preventing duplicates was
        the caller's ``games.analyzed_at > 0`` guard.  The write is now an upsert
        on the natural key ``(game_id, ply, played_move)`` and any rows that
        already duplicated that key are collapsed, so repeated analysis of the
        same game converges on the same row count.  The schema-level unique index
        belongs to ``backend/db.py`` (FIXER-P0 hand-off); this works with or
        without it.

        The whole batch is one transaction and the connection is closed on every
        path, so a failure in the middle leaves the table exactly as it was
        instead of a half-written blunder set.
        """
        summary = {"inserted": 0, "updated": 0, "duplicates_removed": 0, "drills_created": 0}
        if not blunders:
            return summary

        with _SAVE_LOCK:
            conn = get_db()
            try:
                cursor = conn.cursor()
                mate_columns = self._schema_mate_columns(cursor)
                for b in blunders:
                    blunder_id, created = self._upsert_blunder(cursor, b, mate_columns)
                    summary["inserted" if created else "updated"] += 1

                    # Initialize spaced repetition drill item
                    cursor.execute("""
                    INSERT OR IGNORE INTO drill_reviews (
                        blunder_id, attempts, successes, last_attempt_date, next_review_date, interval_days, status
                    ) VALUES (?, 0, 0, 0, ?, 1.0, 'new')
                    """, (blunder_id, int(time.time())))
                    if cursor.rowcount > 0:
                        summary["drills_created"] += 1

                for game_id in sorted({b.get("game_id") for b in blunders}, key=lambda v: (v is None, v)):
                    summary["duplicates_removed"] += self._collapse_duplicate_rows(cursor, game_id)

                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        return summary

    def analyze_and_store(self, game_dict: Dict) -> Tuple[List[Dict], Dict[str, int]]:
        """Analyse one game and persist its blunders atomically.

        Returns only after every blunder of the game is committed.  A caller
        that stamps ``games.analyzed_at`` after this returns without raising
        therefore cannot mark a game analysed unless the analysis really
        completed, and cannot leave a half-written blunder set.
        """
        blunders = self.analyze_game_openings(game_dict)
        saved = self.save_blunders_to_db(blunders)
        return blunders, saved
