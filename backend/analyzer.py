import os
import time
import json
import re
import threading
import chess
import chess.engine
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
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


# --------------------------------------------------------------------------- #
# Identity rule (audit SYNC-18)
# --------------------------------------------------------------------------- #
#: A `blunders` row is only storable when **every** component of
#: :data:`BLUNDER_NATURAL_KEY` is present and usable.  A row with a missing
#: component is both un-addressable and un-deduplicable:
#:
#: * every read of a blunder joins `blunders` to `games` (`/api/blunders`,
#:   `/api/blunders/{id}` are `JOIN`, not `LEFT JOIN`), so a row with
#:   `game_id IS NULL` is invisible to the user while `/api/dashboard`'s bare
#:   `COUNT(*)` on `blunders` still counts it;
#: * `ux_blunders_natural_key` is `UNIQUE(game_id, ply, played_move)` and
#:   SQLite compares NULLs as *distinct* inside a UNIQUE index, so such a row
#:   is never deduplicated and grows by one copy per sync, forever.
#:
#: A Lichess game id is **not** derivable, so a finding with no game identity
#: is refused rather than given a synthetic one: a made-up id would attach the
#: row to a game that does not exist and still be unreadable.  ``ply`` and
#: ``played_move`` **are** derivable (``analyze_game_openings`` computes them
#: from the parsed move while the board is still in front of it), so those two
#: are normalised here rather than refused.
class UnidentifiableBlunderError(ValueError):
    """A blunder row whose natural key has no usable component."""


#: ``next_review_date`` meaning "this card is not due, ever".  The same sentinel
#: ``backend/db.py::_collapse_duplicate_blunders`` already uses.
NEVER_REVIEW_DUE = 9223372036854775807

#: ``drill_reviews.status`` written when the blunder row a *practised* card
#: pointed at is retired (audit SYNC-19).  ``api.py`` only ever asks
#: ``status = 'mastered'`` and ``next_review_date <= ?``, so a retired card
#: leaves ``mastered_count`` and ``due_reviews_count`` truthful without a
#: change to a file this fix does not own.
RETIRED_DRILL_STATUS = "retired"

#: SQL predicate for "the user has actually drilled this card".  A card that
#: :meth:`GameAnalyzer.save_blunders_to_db` auto-created for a brand new
#: finding is all zeros with ``status='new'`` and is **not** practice.
PRACTISED_DRILL_SQL = (
    "COALESCE(d.attempts, 0) > 0 OR COALESCE(d.successes, 0) > 0 "
    "OR COALESCE(d.last_attempt_date, 0) > 0 "
    "OR COALESCE(d.status, 'new') NOT IN ('new', '')"
)

#: The score classes this module can interpret.  ``chess.engine.MateGiven`` is a
#: *singleton instance*, not a class, so it cannot appear in an ``isinstance``
#: tuple; it is compared by identity below.
_ABSOLUTE_SCORE_TYPES = (chess.engine.Cp, chess.engine.Mate)


def _coerce_ply(value: Any) -> Optional[int]:
    """`value` as a non-negative ply number, or ``None`` if it is not one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def natural_key_of(row: Mapping[str, Any]) -> Tuple[str, int, str]:
    """Validated ``(game_id, ply, played_move)`` of a blunder row.

    Normalises what can be normalised and refuses what cannot -- see the
    :class:`UnidentifiableBlunderError` docstring for why a NULL-keyed row must
    never be stored.
    """
    game_id = row.get("game_id")
    if not isinstance(game_id, str) or not game_id.strip():
        raise UnidentifiableBlunderError(
            "refusing to store a blunder with no game id: every blunders read joins "
            "to games (so the row would be invisible), and "
            "UNIQUE(game_id, ply, played_move) cannot dedup a NULL game_id because "
            "SQLite treats NULLs as distinct -- it would grow by one un-deduplicable "
            "copy per sync. Record the findings on the game they came from, or drop "
            "them."
        )
    ply = _coerce_ply(row.get("ply"))
    if ply is None:
        raise UnidentifiableBlunderError(
            "refusing to store a blunder with ply=%r: the ply is the half-move index "
            "the finding was made at and is the natural key, so a row without one can "
            "neither be addressed nor deduplicated." % (row.get("ply"),)
        )
    played_move = row.get("played_move")
    if not isinstance(played_move, str) or not played_move.strip():
        raise UnidentifiableBlunderError(
            "refusing to store a blunder with played_move=%r: the move the user played "
            "is part of the natural key, so a row without one can neither be addressed "
            "nor deduplicated." % (row.get("played_move"),)
        )
    # UCI is canonically lowercase; folding it keeps the key identical to what
    # `ux_blunders_natural_key` stored for a row written by any other tool.
    return game_id.strip(), ply, played_move.strip().lower()


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
    folds "checkmated" and ``MateGiven`` together); the positive sentinel is used
    for it because an already-finished position can never reach this code path
    from ``analyze_game_openings`` (the game is still running there).

    The function is **fail-closed** (audit SYNC-23).  It used to duck-type with
    ``hasattr``, and its "cannot tell, so assume the worst" fallback therefore
    swallowed every score that is not an absolute ``Cp``/``Mate`` -- including
    a plain ``PovScore(Cp(+30))`` passed without a colour, which it reported as
    ``(+10000, mate=0)`` and :func:`describe_score_mate` rendered as
    *"checkmate is already on the board"*.  A ``PovScore`` is relative to the
    side to move and carries neither ``mate()`` nor ``score()``, so there is
    nothing honest to return for one without a colour; the score classes are
    now identified exactly and anything else raises instead of guessing.
    """
    if isinstance(score, chess.engine.PovScore):
        if color is None:
            raise ValueError(
                "normalize_score() needs a colour for a PovScore: it is relative to "
                "the side to move and has no absolute value. Converting it without a "
                "colour would mean guessing, and the old guess reported a plain "
                "evaluation as a delivered checkmate."
            )
        score = score.pov(color)
    elif color is not None and hasattr(score, "pov"):
        score = score.pov(color)

    if isinstance(score, _ABSOLUTE_SCORE_TYPES) or score is chess.engine.MateGiven:
        mate = score.mate()
        if mate is None:
            return int(score.score()), None
        return (MATE_SCORE_CP if mate >= 0 else -MATE_SCORE_CP), int(mate)

    raise TypeError(
        "normalize_score() cannot interpret an engine score of type %r; expected "
        "chess.engine.Cp, chess.engine.Mate, chess.engine.MateGiven or a PovScore "
        "with a colour" % (type(score).__name__,)
    )


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
        if not isinstance(game_id, str) or not game_id.strip():
            # Refuse *before* leasing an engine. A finding with no game identity
            # cannot be stored: it is invisible to every reader (they join
            # `blunders` to `games`) and it is un-deduplicable, because
            # `UNIQUE(game_id, ply, played_move)` does not constrain a NULL
            # (audit SYNC-18). Refusing is loud on purpose: `POST /api/sync`
            # already rejects such records, and the two bulk writers are the
            # only remaining source.
            print("!" * 78)
            print("!! analyze_game_openings: the game record has no id, so its blunders")
            print("!! were NOT analysed and NOTHING was written. A blunders row with a")
            print("!! NULL game_id cannot be shown to the user (every read joins games)")
            print("!! and cannot be deduplicated (SQLite: NULL != NULL), so storing it")
            print("!! would only produce an invisible row that grows on every sync.")
            print("!" * 78)
            return []
        game_id = game_id.strip()
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

    def _upsert_blunder(self, cursor, b: Dict, mate_columns: Sequence[str],
                        key: Tuple[str, int, str]) -> Tuple[int, bool]:
        """INSERT-or-UPDATE one blunder on its natural key. Returns (id, created).

        ``key`` is the *validated* natural key from :func:`natural_key_of`, so
        the value stored and the value looked up are the same bytes even when a
        caller padded or upper-cased them.
        """
        game_id, ply, played_move = key
        names: List[str] = []
        values: List[Any] = []
        for column, field in _BLUNDER_COLUMNS:
            if column in _OPTIONAL_MATE_COLUMNS and column not in mate_columns:
                continue
            names.append(column)
            if column in BLUNDER_NATURAL_KEY:
                values.append({"game_id": game_id, "ply": ply,
                               "played_move": played_move}[column])
            else:
                values.append(
                    b.get(field)
                    if (field in _OPTIONAL_BLUNDER_KEYS or column in _OPTIONAL_MATE_COLUMNS)
                    else b[field]
                )

        cursor.execute(
            "SELECT id FROM blunders WHERE game_id = ? AND ply = ? AND played_move = ? ORDER BY id LIMIT 1",
            key
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
            key = (_coerce_ply(row["ply"]), (row["played_move"] or "").strip().lower())
            if key in seen:
                doomed.append(int(row["id"]))
            else:
                seen.add(key)
        for row_id in doomed:
            cursor.execute("DELETE FROM drill_reviews WHERE blunder_id = ?", (row_id,))
            cursor.execute("DELETE FROM blunders WHERE id = ?", (row_id,))
        return len(doomed)

    @staticmethod
    def _reconcile_game_blunders(cursor, game_id: str,
                                 keep: Set[Tuple[int, str]]) -> Tuple[int, int]:
        """Make ONE game's stored blunder set equal the engine's current findings.

        Audit SYNC-19: the SYNC-15 upsert made re-analysis *idempotent* but not
        *correct*.  A finding the engine no longer reports was never removed and
        ``games.analyzed_at`` guaranteed it never would be, so the gap list kept
        ranking it, ``mastered_count`` counted it, and nothing was ever reported.
        Analysis is a recomputation, so the stored set of a game in scope must
        equal the computed set.

        The scope is exactly one game id -- the ``WHERE`` clause is
        ``b.game_id = ?`` -- so a re-analysis can never touch another game's
        rows, and the "keep" set is compared in Python (never with
        ``NOT IN (...)``, whose NULL semantics are a footgun).

        Staleness policy for a row the engine no longer reports:

        * **Never practised** (no ``drill_reviews`` row, or one that is still
          the all-zero ``status='new'`` placeholder) -> the row *and* its empty
          card are deleted.  Nothing of the user's is lost and the phantom stops
          being counted.
        * **Practised** (any attempt, any success, any attempt date, or a
          non-``new`` status) -> the ``blunders`` row is deleted but the card is
          **retired, not deleted**: ``status='retired'`` and
          ``next_review_date = NEVER_REVIEW_DUE``.  The user's own training
          record (attempts, successes, dates, interval, ease) survives verbatim
          and stays queryable, while ``mastered_count`` and
          ``due_reviews_count`` -- which are bare ``COUNT(*)``s on
          ``drill_reviews`` -- stop counting a finding that no longer exists.
          ``blunders.id`` is ``INTEGER PRIMARY KEY AUTOINCREMENT``, so a retired
          card's ``blunder_id`` can never be silently re-bound to a different
          blunder later.

        A *patched* finding needs no special case: "patched" is not a column.
        ``get_game_gaps`` derives it by comparing the finding's ``best_move`` at
        its ``fen_before`` with the user's own repertoire tree, so the fix lives
        in ``repertoire_moves`` and removing the stale finding cannot lose it.

        Returns ``(rows_removed, cards_retired)``.
        """
        cursor.execute(
            "SELECT b.id AS id, b.ply AS ply, b.played_move AS played_move, "
            "       (%s) AS practised "
            "FROM blunders b LEFT JOIN drill_reviews d ON d.blunder_id = b.id "
            "WHERE b.game_id = ? ORDER BY b.id" % PRACTISED_DRILL_SQL,
            (game_id,)
        )
        removed = 0
        retired = 0
        for row in cursor.fetchall():
            # Coerce the *stored* key exactly as `natural_key_of` coerces the
            # incoming one. `blunders.ply` is an INTEGER-affinity column so
            # SQLite already normalises it, but a mis-typed stored key must
            # never be able to disguise a still-valid finding as a stale one --
            # that would delete a real row.
            key = (_coerce_ply(row["ply"]), (row["played_move"] or "").strip().lower())
            if key in keep:
                continue
            if row["practised"]:
                cursor.execute(
                    "UPDATE drill_reviews SET status = ?, next_review_date = ? "
                    "WHERE blunder_id = ?",
                    (RETIRED_DRILL_STATUS, NEVER_REVIEW_DUE, row["id"])
                )
                retired += 1
            else:
                cursor.execute("DELETE FROM drill_reviews WHERE blunder_id = ?", (row["id"],))
            cursor.execute("DELETE FROM blunders WHERE id = ?", (row["id"],))
            removed += 1
        return removed, retired

    def save_blunders_to_db(self, blunders: Optional[List[Dict]],
                            game_ids: Optional[Iterable[Any]] = None) -> Dict[str, int]:
        """Persist one game's blunders idempotently, in a single transaction.

        Audit SYNC-15: this was a bare ``INSERT``, so re-analysing a game appended
        a second copy of every row -- the only thing preventing duplicates was
        the caller's ``games.analyzed_at > 0`` guard.  The write is now an upsert
        on the natural key ``(game_id, ply, played_move)`` and any rows that
        already duplicated that key are collapsed, so repeated analysis of the
        same game converges on the same row count.  The schema-level unique index
        belongs to ``backend/db.py`` (FIXER-P0 hand-off); this works with or
        without it.

        Audit SYNC-19: idempotency is not correctness.  Every game whose id
        appears in the batch **or** in ``game_ids`` is *reconciled*: rows the
        engine no longer reports are removed (or their drill card retired) by
        :meth:`_reconcile_game_blunders`, so a re-analysis converges to the
        engine's current output instead of accumulating.  ``game_ids`` is what
        lets an analysis that found **nothing** still converge -- an empty
        ``blunders`` list names no game, so a caller that only wants the stored
        set to become empty must say which game it re-analysed.  The convergent
        entry point is :meth:`analyze_and_store`, which does it for you.

        A row whose natural key has no usable component is refused **before** the
        transaction opens (:func:`natural_key_of`); see
        :class:`UnidentifiableBlunderError` for why storing one would be worse
        than losing it.

        The whole batch is one transaction and the connection is closed on every
        path, so a failure in the middle leaves the table exactly as it was
        instead of a half-written blunder set.
        """
        summary = {"inserted": 0, "updated": 0, "duplicates_removed": 0,
                   "drills_created": 0, "stale_removed": 0, "stale_retired": 0}

        # Validate the whole batch BEFORE a transaction opens: a row that has to
        # be refused must not leave the good rows of its own batch written.
        keyed = [(natural_key_of(b), b) for b in (blunders or [])]
        scope: List[str] = []
        for key, _row in keyed:
            if key[0] not in scope:
                scope.append(key[0])
        for raw_id in (game_ids or []):
            # `str(None)`/`None` must not become the literal string "None".
            game_id = raw_id.strip() if isinstance(raw_id, str) else raw_id
            if not isinstance(game_id, str) or not game_id:
                raise UnidentifiableBlunderError(
                    "save_blunders_to_db(game_ids=...) was given %r, which is not a "
                    "game id; reconciling against it would silently wipe a game's "
                    "stored findings." % (raw_id,)
                )
            if game_id not in scope:
                scope.append(game_id)
        if not scope:
            return summary

        with _SAVE_LOCK:
            conn = get_db()
            try:
                cursor = conn.cursor()
                mate_columns = self._schema_mate_columns(cursor)
                keep: Dict[str, Set[Tuple[int, str]]] = {}
                for (game_id, ply, played_move), b in keyed:
                    blunder_id, created = self._upsert_blunder(
                        cursor, b, mate_columns, (game_id, ply, played_move))
                    summary["inserted" if created else "updated"] += 1
                    keep.setdefault(game_id, set()).add((ply, played_move))

                    # Initialize spaced repetition drill item
                    cursor.execute("""
                    INSERT OR IGNORE INTO drill_reviews (
                        blunder_id, attempts, successes, last_attempt_date, next_review_date, interval_days, status
                    ) VALUES (?, 0, 0, 0, ?, 1.0, 'new')
                    """, (blunder_id, int(time.time())))
                    if cursor.rowcount > 0:
                        summary["drills_created"] += 1

                for game_id in scope:
                    summary["duplicates_removed"] += self._collapse_duplicate_rows(cursor, game_id)
                    removed, retired = self._reconcile_game_blunders(
                        cursor, game_id, keep.get(game_id, set()))
                    summary["stale_removed"] += removed
                    summary["stale_retired"] += retired

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

        The game's id is passed as the *scope* of the write, so this is the
        convergent entry point: a re-analysis whose engine now reports **fewer**
        findings (or none at all) removes the findings the engine no longer
        reports (audit SYNC-19).  The two legacy call sites
        (``api.py`` ``POST /api/sync`` and ``repertoire.analyze_user_games`` /
        ``stream_analyze_user_games``) still guard on ``if blunders:``, so an
        analysis that finds nothing cannot name the game it re-analysed and
        cannot converge; see the FIXER-S hand-off.
        """
        game_id = game_dict.get("id")
        blunders = self.analyze_game_openings(game_dict)
        saved = self.save_blunders_to_db(blunders, game_ids=[game_id] if game_id else None)
        return blunders, saved
