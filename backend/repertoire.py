import os
import json
import math
import time
import random
import re
import sqlite3
import threading
import requests
import asyncio
import io
import chess
import chess.engine
import chess.pgn
from typing import Dict, List, Optional, Tuple, AsyncGenerator, Any
from collections import defaultdict

from backend.db import get_db, get_setting, set_setting
# Audit BUILDER-05 / ABUSE-06: the explorer eval loop used to drive ONE process-wide,
# unsynchronised `SimpleEngine` (`RepertoireManager._get_engine`). `backend.evaluator`
# now owns every Stockfish lifecycle in a bounded, health-checked pool, so this module
# leases an engine instead of owning one. Same mechanism as `backend/analyzer.py`.
from backend.evaluator import (
    EngineBusy,
    EngineUnavailable,
    get_sync_engine_pool,
)
from backend.lichess_client import LichessClient, DEFAULT_PERF_TYPES
from backend.openings import OpeningBook

STOCKFISH_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "stockfish", "stockfish-linux-x86-64-universal"
)

#: How deep the *replace* prune (`add_repertoire_move(replace_existing=True)`) is allowed to
#: cascade: level 1 = the direct children of the replaced move. A write must never cascade
#: further than the conflicting move itself, because the cascade matches positions with a
#: 3-token FEN `LIKE` and can therefore walk into transposed lines the user never touched
#: (audit SCOUT-02 escalation 3). The two *explicit* delete endpoints
#: (`delete_repertoire_branch`, `delete_study(delete_moves=True)`) prune without a cap,
#: because removing the whole branch is what the user asked for.
REPLACE_PRUNE_MAX_DEPTH = 1

#: How long `check_line_overlap` / `check_lines_overlap` may reuse an in-process index of the
#: move tree instead of re-reading `SELECT * FROM repertoire_moves` (audit SCOUT-11(4)/(5)).
#: Every write path in this module invalidates the index, so the TTL is only a safety net
#: against a writer that bypassed this class.
OVERLAP_INDEX_TTL_SECONDS = 5.0

# --------------------------------------------------------------------------- #
# Line-end semantics (audit FIXER-V / TRAINER-09, owner ruling)
# --------------------------------------------------------------------------- #

#: The line stops on the **user's own prepared move** and the repertoire genuinely has nothing
#: more: a real completion. `is_line_end` is `True`.
LINE_END_REPERTOIRE_EXHAUSTED = "repertoire_exhausted"

#: The repertoire holds one or more opponent replies for which the user has prepared **no**
#: answer. The board is still resting on the user's own move (the trainer refuses to move onto
#: an unanswered opponent branch), so `is_line_end` is `True`, but the line stopped because of a
#: *gap in the repertoire*, not because the line is finished. The UI must not celebrate it.
LINE_END_UNANSWERED_BRANCHES = "unanswered_opponent_branches"

#: The three states a drill position can be in, per the owner's ruling.
LINE_POSITION_CONTINUES = "continues"            # the opponent moved and the user has a reply
LINE_POSITION_COMPLETED = "completed_line"      # the user's own move is the last thing prepared
LINE_POSITION_AWAITING_REPLY = "awaiting_user_reply"  # the opponent moved, nothing is prepared


def classify_line_position(reached_by_user_move: bool, has_prepared_reply: bool) -> str:
    """The single definition of "is this line finished?" (owner ruling, FIXER-V).

    A **completed line** is a position reached at the end of the *user's own prepared move*,
    where the repertoire genuinely has nothing more. An **unanswered opponent branch** is a
    position reached by an *opponent* move for which the user has prepared nothing — that is a
    gap in the repertoire, never a completion, and it is never presented as a drill choice.

    :param reached_by_user_move: whether the move that produced this position is one of the
        user's own prepared moves (i.e. the board is resting on their move, so it is their turn).
    :param has_prepared_reply: whether the repertoire holds a prepared user move at this position.
    :returns: one of the three ``LINE_POSITION_*`` constants. The
        ``(False, False)`` case - the opponent moved and nothing is prepared - is the bug the
        owner overruled, and it is deliberately **not** a completion.
    """
    if has_prepared_reply:
        # The opponent moved and the user has something to play: the line continues.
        return LINE_POSITION_CONTINUES
    if reached_by_user_move:
        # The user's own move is the end of what they have prepared. A real completion.
        return LINE_POSITION_COMPLETED
    # THE BUG: the opponent moved and the user has not prepared a reply. A gap, not an end.
    return LINE_POSITION_AWAITING_REPLY


# --------------------------------------------------------------------------- #
# FSRS-5 / DSR Memory Model & Redeemable Mastery (Trainer Engine)
# --------------------------------------------------------------------------- #

#: FSRS-4.5 / FSRS-5 power-law forgetting curve constants:
#:   R(t, S) = (1 + FSRS_FACTOR * (t / S)) ** FSRS_DECAY
#: By construction, when elapsed days t == stability S, R(S, S) = 0.90 (90% recall target).
FSRS_FACTOR = 19.0 / 81.0
FSRS_DECAY = -0.5
FSRS_DEFAULT_DIFFICULTY = 5.0
FSRS_MIN_DIFFICULTY = 1.0
FSRS_MAX_DIFFICULTY = 10.0

#: Minimum number of intervening drills before an in-session mistake is re-tested in
#: `adaptive` mode, preventing 2-second visual echo memory from masquerading as learning.
SESSION_LAPSE_COOLDOWN_DRILLS = 3

#: Cadence for injecting a "Mastered Spot-Check" (audit) drill during an `adaptive`
#: session when mastered positions exist (every 5th drill = ~20% of a 20-move session).
MASTERED_AUDIT_CADENCE = 5


def compute_fsrs_retrievability(
    last_practiced: Any,
    stability_days: Any,
    now: Optional[int] = None,
) -> float:
    """Computes current recall probability R(t, S) in [0.0, 1.0] using the FSRS power forgetting curve."""
    try:
        lp = int(last_practiced or 0)
    except (TypeError, ValueError):
        lp = 0
    if lp <= 0:
        return 0.0
    if now is None:
        now = int(time.time())
    elapsed_days = max(0.0, (now - lp) / 86400.0)
    try:
        s = max(0.1, float(stability_days or 1.0))
    except (TypeError, ValueError):
        s = 1.0
    r = (1.0 + FSRS_FACTOR * (elapsed_days / s)) ** FSRS_DECAY
    return max(0.01, min(1.0, float(r)))


def infer_move_memory_state(move: Dict[str, Any], now: Optional[int] = None) -> Dict[str, Any]:
    """Normalises a `repertoire_moves` row into FSRS DSR memory variables + streak/leech state.

    Compatible with both newly-trained rows (which populate `consecutive_correct`, `lapses`,
    `difficulty`) and legacy rows or raw SQL test fixtures that only set `times_practiced` and
    `times_correct`.
    """
    try:
        tp = max(0, int(move.get("times_practiced") or 0))
    except (TypeError, ValueError):
        tp = 0
    try:
        tc = max(0, int(move.get("times_correct") or 0))
    except (TypeError, ValueError):
        tc = 0
    try:
        raw_lapses = max(0, int(move.get("lapses") or 0))
    except (TypeError, ValueError):
        raw_lapses = 0
    lapses = max(raw_lapses, max(0, tp - tc))

    try:
        raw_streak = max(0, int(move.get("consecutive_correct") or 0))
    except (TypeError, ValueError):
        raw_streak = 0
    if raw_streak > 0:
        streak = raw_streak
    elif tp > 0 and tp == tc and lapses == 0:
        streak = tc
    else:
        streak = 0

    raw_diff = move.get("difficulty")
    try:
        difficulty = float(raw_diff) if raw_diff is not None else FSRS_DEFAULT_DIFFICULTY
    except (TypeError, ValueError):
        difficulty = FSRS_DEFAULT_DIFFICULTY
    if (raw_diff is None or difficulty == FSRS_DEFAULT_DIFFICULTY) and lapses > 0 and raw_lapses == 0:
        difficulty = min(FSRS_MAX_DIFFICULTY, FSRS_DEFAULT_DIFFICULTY + 1.2 * lapses)
    difficulty = max(FSRS_MIN_DIFFICULTY, min(FSRS_MAX_DIFFICULTY, difficulty))

    try:
        stability = max(0.1, float(move.get("interval_days") or 1.0))
    except (TypeError, ValueError):
        stability = 1.0

    last_prac = move.get("last_practiced") or 0
    retrievability = compute_fsrs_retrievability(last_prac, stability, now=now) if tp > 0 else 0.0
    required_streak = 3 if lapses >= 3 else 2
    is_leech = bool(lapses >= 2 and streak < required_streak)
    is_weak = bool((lapses > 0 and streak < required_streak) or (tp > 0 and tc == 0))

    return {
        "tp": tp,
        "tc": tc,
        "lapses": lapses,
        "streak": streak,
        "difficulty": difficulty,
        "stability": stability,
        "retrievability": retrievability,
        "required_streak": required_streak,
        "is_leech": is_leech,
        "is_weak": is_weak,
    }


def classify_move_mastery(move: Dict[str, Any]) -> str:
    """Classifies a user move into `'mastered'`, `'learning'`, or `'unlearned'`.

    Fixes the legacy `tc == tp` permanent lock:
      * A move with zero lapses (`lapses == 0`) is `'mastered'` once `tc >= 2`.
      * A move that was previously missed (`lapses > 0`) can be **redeemed** to `'mastered'`
        once the user achieves `consecutive_correct >= 2` (or `>= 3` for a chronic leech with
        `lapses >= 3`) and `interval_days >= 3.0`.
      * Any move with `tc > 0` that has not yet met the mastery threshold (including a
        mastered move that was just missed, resetting `consecutive_correct` to 0 and
        `interval_days` to 1.0) is `'learning'`.
      * Otherwise (`tc == 0`), it is `'unlearned'`.
    """
    state = infer_move_memory_state(move)
    tp = state["tp"]
    tc = state["tc"]
    lapses = state["lapses"]
    streak = state["streak"]
    stability = state["stability"]
    required_streak = state["required_streak"]

    if lapses == 0 and tc >= 2 and tc == tp:
        return "mastered"
    if lapses > 0 and tc >= 2 and streak >= required_streak and stability >= 3.0:
        return "mastered"
    if tc > 0:
        return "learning"
    return "unlearned"

# --------------------------------------------------------------------------- #
# External-data hardening (audit SYNC-17 / SYNC-20 / SYNC-21 / ABUSE-17)
# --------------------------------------------------------------------------- #

#: Epoch values at or above this magnitude are milliseconds (1e11 s is the year 5138, 1e11 ms
#: is 1973), so the threshold is unambiguous. Copied from `backend/api.py` on purpose: the
#: normalisation has to be reachable from the *writer*, and `api.py` imports this module.
MS_EPOCH_THRESHOLD = 100_000_000_000

#: Upper bound for a stored `games.played_at` (milliseconds). 2**53 ms is ~285 000 years, so
#: no real game is ever clipped, while `Infinity`, `1e300` and `2**62` are (audit SYNC-21).
MAX_PLAYED_AT_MS = 2 ** 53

#: Upper bound for any count that comes back from the explorer and is stored in an INTEGER
#: column. Real master-database counts are far below 10**9; anything above is corruption
#: (audit SYNC-20: `10**300` used to raise `OverflowError` inside the analysis batch).
MAX_EXPLORER_COUNT = 1_000_000_000

#: How long a *failed* explorer lookup is remembered in-process. Distinct from a real
#: zero-game answer, which is remembered for the normal 7-day cache TTL (audit ABUSE-17).
EXPLORER_NEGATIVE_TTL_SECONDS = 60.0

#: `games.result` tokens. `white`/`black` mean "that colour won" and are the canonical
#: self-describing form (FIXER-B). `draw` is the explicit draw token. `abandoned` is a game
#: that never finished and must therefore be excluded from the personal threat statistics
#: instead of being booked as a draw or a loss (audit SYNC-17 variants A/B).
RESULT_WHITE = "white"
RESULT_BLACK = "black"
RESULT_DRAW = "draw"
RESULT_ABANDONED = "abandoned"

#: Lichess `status` values that mean "the game did not finish".
UNFINISHED_STATUSES = frozenset({"abandoned", "unfinished", "timeout", "no_start"})


def normalize_played_at(value: Any, default: int = 0) -> int:
    """Total normalisation of a Lichess timestamp to epoch MILLISECONDS.

    Audit SYNC-21. This is the *writer*-side guard. The reader-side guard lives in
    `backend/api.py` (`normalize_played_at`) and it raises `OverflowError` on a non-finite
    value, which its global handler turns into a 400 for the WHOLE Recent Games tab. The
    reader could not be fixed from here, so this copy is deliberately total:

    * anything that is not a finite number, or is not a number at all, becomes `default`
      (`0`, which every consumer already renders as "unknown date");
    * the result is clamped to `[0, MAX_PLAYED_AT_MS]`, so `Infinity`, `1e300` and
      `2**62` cannot be persisted;
    * values below `MS_EPOCH_THRESHOLD` are seconds and are scaled up (the historical
      unit mix documented in AGENTS.md §4/§5).
    """
    try:
        if isinstance(value, bool):
            raise TypeError("bool is not a timestamp")
        number = float(value)
    except (TypeError, ValueError):
        return int(default)
    # NaN / +-Infinity / any non-finite float.
    if number != number or number in (float("inf"), float("-inf")):
        return int(default)
    try:
        stamp = int(number)
    except (OverflowError, ValueError):
        return int(default)
    if stamp <= 0:
        return int(default) if stamp < 0 else 0
    if stamp < MS_EPOCH_THRESHOLD:
        stamp *= 1000
    return min(stamp, MAX_PLAYED_AT_MS)


def coerce_count(value: Any, default: int = 0) -> int:
    """Coerce an untrusted count to a bounded, storable `int`.

    Audit SYNC-20. Rejects `None`, `bool`, non-numeric strings, `NaN`, `+-Infinity` and
    anything above `MAX_EXPLORER_COUNT`. Used for every number that arrives from the Lichess
    explorer and for every `opening_games` / `total_games` value read back out of
    `explorer_cache` (a row can hold a REAL, a huge int or the string `"soon"`).
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    if number < 0:
        return default
    if number > MAX_EXPLORER_COUNT:
        return MAX_EXPLORER_COUNT
    return int(number)


def coerce_text(value: Any, default: str = "", max_len: int = 4096) -> str:
    """Coerce an untrusted field to a bounded `str` (never raises inside a sqlite bind)."""
    if value is None:
        return default
    if not isinstance(value, str):
        if isinstance(value, (dict, list, tuple, set, bool)):
            return default
        value = str(value)
    if len(value) > max_len:
        value = value[:max_len]
    return value


def _number_is_unusable(value: Any) -> bool:
    """True when `value` is present but is not a finite, non-negative, storable number."""
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(number) or number < 0


def sanitize_explorer_payload(payload: Any, is_white_turn: Optional[bool] = None) -> Optional[Dict]:
    """Validate + coerce one 2000+ explorer payload (raw reply *or* cached formatted row).

    Audit SYNC-20. `explorer_cache.data_json` is a plain TEXT column that this module used to
    write straight from `resp.json()` and then read back with bare subscripts and unguarded
    arithmetic, so four malformed shapes (a dict/list root, a string "move", string counts, a
    move with no `san`) either 500'd the *tree* endpoint - which calls
    `get_candidate_moves_2000` internally, so no `api.py` route guard could protect it - or got
    cached and broke the next four readers of the table.

    Returns the canonical, fully-coerced payload, or `None` when the payload cannot be
    understood at all (which the caller must treat as "could not check", never as a zero).
    """
    if not isinstance(payload, dict):
        return None

    raw_moves = payload.get("moves", [])
    if raw_moves is None:
        raw_moves = []
    if not isinstance(raw_moves, list):
        return None

    # --- total games -------------------------------------------------------------- #
    if "total_games" in payload:
        if _number_is_unusable(payload.get("total_games")):
            return None
        total_games = coerce_count(payload.get("total_games"))
    else:
        total_games = (coerce_count(payload.get("white"))
                       + coerce_count(payload.get("draws"))
                       + coerce_count(payload.get("black")))

    moves: List[Dict[str, Any]] = []
    for entry in raw_moves:
        if not isinstance(entry, dict):
            continue  # a "move" that is not an object is not a move
        san = coerce_text(entry.get("san"), "", 32)
        uci = coerce_text(entry.get("uci"), "", 8)
        if not san and not uci:
            # Audit SYNC-20 variant B: this shape used to be cached and then 500'd
            # `get_repertoire_tree`'s `opp_m["san"]`. A move with neither a SAN nor a UCI
            # cannot be displayed or played, so it is dropped at the boundary.
            continue
        if any(_number_is_unusable(entry.get(k)) for k in ("white", "draws", "black", "games")):
            continue

        if "white" in entry or "draws" in entry or "black" in entry:
            w = coerce_count(entry.get("white"))
            d = coerce_count(entry.get("draws"))
            b = coerce_count(entry.get("black"))
            games = w + d + b
            w_pct = round((w / games * 100), 1) if games > 0 else 0.0
            d_pct = round((d / games * 100), 1) if games > 0 else 0.0
            b_pct = round((b / games * 100), 1) if games > 0 else 0.0
        else:
            games = coerce_count(entry.get("games"))
            w_pct = float(coerce_count(entry.get("white_win_pct")))
            d_pct = float(coerce_count(entry.get("draw_pct")))
            b_pct = float(coerce_count(entry.get("black_win_pct")))

        pop_pct = round((games / total_games * 100), 1) if total_games > 0 else 0.0
        if is_white_turn is None:
            win_rate = entry.get("win_rate")
            win_rate = float(coerce_count(win_rate, 0.0))
        else:
            win_rate = w_pct if is_white_turn else b_pct

        rating = entry.get("averageRating", entry.get("average_rating", 2000))
        if _number_is_unusable(rating):
            rating = 2000

        move: Dict[str, Any] = {
            "san": san,
            "uci": uci,
            "games": games,
            "popularity_pct": pop_pct,
            "white_win_pct": w_pct,
            "draw_pct": d_pct,
            "black_win_pct": b_pct,
            "win_rate": win_rate,
            "average_rating": coerce_count(rating, 2000),
        }
        if entry.get("eval") is not None:
            # An eval is White's point of view, so a NEGATIVE value is a normal answer (Black is
            # better) and must survive. Only non-finite / non-numeric values are dropped.
            try:
                eval_value = float(entry.get("eval"))
            except (TypeError, ValueError):
                eval_value = None
            if eval_value is not None and math.isfinite(eval_value):
                move["eval"] = int(eval_value)
        moves.append(move)

    if raw_moves and not moves:
        # The upstream LISTED moves, and not one of them survived validation. That is an
        # unusable document, not a fact: reporting it as `total_games: 0, moves: []` would
        # cache a fabricated "nobody plays this above 2000" for 7 days (audit SYNC-20's
        # "a move has string counts" shape, which used to be a 500 and must now be a failure).
        return None

    return {
        "fen": coerce_text(payload.get("fen"), "", 120),
        "total_games": total_games,
        "white_total": coerce_count(payload.get("white_total", payload.get("white"))),
        "draws_total": coerce_count(payload.get("draws_total", payload.get("draws"))),
        "black_total": coerce_count(payload.get("black_total", payload.get("black"))),
        "moves": moves,
        "eval_convention": coerce_text(payload.get("eval_convention"), "white", 16) or "white",
    }


def classify_stored_result(stored: Any, user_color: str) -> str:
    """Map a stored `games.result` onto one of `white` / `black` / `draw` / `abandoned`.

    Audit SYNC-17. Understands three generations of the column:

    * the canonical self-describing tokens written by FIXER-B and by this module
      (`white` / `black` / `draw` / `abandoned`);
    * the legacy user-perspective tokens `1-0` / `0-1` / `1/2-1/2` (AGENTS.md §5 documents
      `games.result` as "from `user_color`'s perspective", and every row written before
      FIXER-B uses them, including the synthetic fixture);
    * SQL `NULL` / empty, which the *reader* (`backend/api.render_game_outcome`) has always
      rendered as a draw, so the engine must agree with it rather than book it as a loss.

    Anything genuinely unrecognised maps to `RESULT_DRAW` rather than to a loss, because a
    loss is a factual claim about the user's game and a garbage token is not evidence of one.
    """
    token = (str(stored).strip().lower() if stored is not None else "")
    colour = (user_color or "white").strip().lower()
    other = "black" if colour == "white" else "white"
    if token in (RESULT_WHITE, RESULT_BLACK, RESULT_DRAW, RESULT_ABANDONED):
        return token
    if token in UNFINISHED_STATUSES:
        return RESULT_ABANDONED
    if token in ("1-0", "1-0+", "1-0/"):
        return colour
    if token in ("0-1", "0-1+", "0-1/", "+"):
        return other
    if token in ("1/2-1/2", "½-½", "0.5"):
        return RESULT_DRAW
    return RESULT_DRAW


def normalize_game_outcome(winner: Any, status: Any = None, user_color: str = "white") -> Optional[str]:
    """Canonical `games.result` token for an upstream game record, or `None` when invalid.

    Audit SYNC-17. Lichess sends `"winner": null` for *both* a draw and an abandoned game;
    `status` is what tells them apart, and the four writers used to throw `status` away, so
    an abandoned game was stored (and scored) exactly like a drawn one.

    Returns `None` for an unrecognised non-empty `winner` token, which the writers report
    and skip instead of persisting (variant C: the junk string used to be stored verbatim,
    read back as a draw by the UI and as a loss by the threat engine).
    """
    raw = winner.strip().lower() if isinstance(winner, str) else ("" if winner is None else str(winner).strip().lower())
    st = status.strip().lower() if isinstance(status, str) else ""
    if st in UNFINISHED_STATUSES:
        return RESULT_ABANDONED
    if raw in (RESULT_WHITE, RESULT_BLACK):
        return raw
    if raw in ("draw", "1/2-1/2", "½-½", "0.5", ""):
        return RESULT_DRAW
    return None


def normalize_fen(fen: str) -> str:
    """Normalize FEN to pieces, turn, castling rights, and en passant square (first 4 tokens)."""
    if not fen or not isinstance(fen, str):
        return ""
    parts = fen.strip().split()
    if len(parts) >= 4:
        return " ".join(parts[:4])
    try:
        b = chess.Board(fen.strip())
        return " ".join(b.fen().split()[:4])
    except Exception:
        return fen.strip()

def extract_player_info(game_dict: dict, color: str) -> Tuple[str, int]:
    """Safely extracts (username, rating) for a player color from game dictionary."""
    if not isinstance(game_dict, dict):
        return ("", 0)
    players = game_dict.get("players")
    if not isinstance(players, dict):
        return ("", 0)
    pdata = players.get(color)
    if not isinstance(pdata, dict):
        return ("", 0)
    user_info = pdata.get("user")
    username = ""
    if isinstance(user_info, dict):
        username = str(user_info.get("name") or user_info.get("id") or "")
    elif "aiLevel" in pdata:
        username = f"AI Level {pdata.get('aiLevel')}"
    raw_rating = pdata.get("rating")
    rating = 0
    if raw_rating is not None:
        try:
            rating = int(raw_rating)
        except (ValueError, TypeError):
            rating = 0
    return (username, rating)

class RepertoireManager:
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self.lichess_client = LichessClient()
        self.opening_book = OpeningBook.get_instance()
        self._user_stats_cache = {}
        self._user_stats_time = 0
        self._explorer_count_memo = {}
        self._seed_lock = threading.Lock()
        # Overlap-check index (audit SCOUT-11(4)/(5)): {repertoire: (moves_by_parent,
        # moves_by_parent_3)} plus the timestamp it was read at.
        self._overlap_index: Dict[str, Tuple[Dict, Dict, float]] = {}
        # Audit ABUSE-17 (single-flight + negative cache for the 2000+ explorer):
        #   _explorer_flight   {(norm_fen, ratings): threading.Event} - the first caller for a
        #                      cold key fetches, everybody else waits on the event and then
        #                      re-reads the cache, so N concurrent requests cost ONE upstream
        #                      call instead of N (measured 24x amplification before).
        #   _explorer_negative {(norm_fen, ratings): expiry_ts}       - a *failed* lookup is
        #                      remembered for EXPLORER_NEGATIVE_TTL_SECONDS, which is a
        #                      different thing from a genuine zero-game answer and is reported
        #                      to the caller as `known=False` so it can never be rendered as 0.
        self._explorer_flight: Dict[Tuple[str, str], threading.Event] = {}
        self._explorer_negative: Dict[Tuple[str, str], float] = {}
        self._explorer_flight_lock = threading.Lock()

    @classmethod
    def get_instance(cls):
        """Process-wide singleton.

        Audit ABUSE-06: `if cls._instance is None: cls._instance = cls()` is a check-then-set
        race. `RepertoireManager.get_instance()` is called from ~20 sync routes, which
        FastAPI runs in the anyio threadpool, so N concurrent cold-start requests used to
        build N managers (and, before the pool migration, N Stockfish processes, none of
        which were ever reaped). Double-checked locking makes the construction atomic.
        """
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @staticmethod
    def _engine_pool():
        """The process-wide bounded Stockfish pool (audit BUILDER-05 / ABUSE-06)."""
        return get_sync_engine_pool(STOCKFISH_PATH)

    def close(self):
        """Release this manager's in-process caches.

        Audit BUILDER-05: this method used to `quit()` a manager-owned `SimpleEngine`. The
        manager no longer owns an engine - every process now comes from, and goes back to,
        the shared bounded pool in `backend.evaluator` (which owns the `atexit` net and the
        `close_all()` shutdown). Terminate the engines with
        `backend.evaluator.get_sync_engine_pool().close_all()`.
        """
        self._user_stats_cache = {}
        self._user_stats_time = 0
        if hasattr(self, "_user_stats_by_fen"):
            self._user_stats_by_fen = {}
        self._invalidate_overlap_index()

    def _invalidate_overlap_index(self):
        """Drop the cached overlap index (called by every write path in this class)."""
        self._overlap_index = {}

    def get_user_position_stats(self, color: str = "white") -> Dict[Tuple[str, str], Dict]:
        """Indexes user's games and returns {(norm_fen, move_uci): {'games': N, 'wins': W, 'draws': D, 'losses': L, 'win_rate': float, 'threat_score': float}}."""
        color = (color or "white").lower()
        if color not in ("white", "black"):
            color = "white"
        now = time.time()
        if color in self._user_stats_cache and (now - self._user_stats_time < 60):
            return self._user_stats_cache[color]

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_color, result, moves_san FROM games WHERE user_color = ?", (color,))
        games = cursor.fetchall()
        conn.close()

        stats = defaultdict(lambda: {"games": 0, "wins": 0, "draws": 0, "losses": 0})

        for g in games:
            moves_san = g["moves_san"]
            if not moves_san:
                continue
            u_color = (g["user_color"] or "white").strip().lower()
            # Audit SYNC-17: the four `games.result` writers used to persist a draw as SQL
            # NULL, and this scorer classified anything that was not `u_color` and not the
            # literal "draw" as a LOSS - so every draw (and every abandoned game) inflated
            # the user's threat score. The classifier now returns the canonical token, and an
            # unfinished game is not counted at all instead of being booked as a draw/loss.
            res = classify_stored_result(g["result"], u_color)
            if res == RESULT_ABANDONED:
                continue
            board = chess.Board()
            for san in moves_san.strip().split():
                if not san:
                    continue
                clean_san = re.sub(r"[?!#+]", "", san).strip()
                if not clean_san:
                    continue
                p_fen = normalize_fen(board.fen())
                try:
                    m = board.parse_san(clean_san)
                except Exception:
                    try:
                        m = chess.Move.from_uci(clean_san)
                        if m not in board.legal_moves:
                            break
                    except Exception:
                        break

                uci = m.uci()
                board.push(m)

                st = stats[(p_fen, uci)]
                st["games"] += 1
                if res == u_color:
                    st["wins"] += 1
                elif res == RESULT_DRAW:
                    st["draws"] += 1
                else:
                    st["losses"] += 1

        result = {}
        for (p_fen, uci), st in stats.items():
            g_cnt = st["games"]
            wins = st["wins"]
            draws = st["draws"]
            losses = st["losses"]
            win_pct = round((wins + 0.5 * draws) / g_cnt * 100, 1)
            loss_rate = max(0.0, min(1.0, 1.0 - (win_pct / 100.0)))
            # Smart scoring formula: N * (loss_rate^2 + 0.02)
            # Convex penalty for loss rate + baseline for volume exposure
            threat = round(g_cnt * ((loss_rate ** 2) + 0.02), 2)
            result[(p_fen, uci)] = {
                "games": g_cnt,
                "wins": wins,
                "draws": draws,
                "losses": losses,
                "win_rate": win_pct,
                "threat_score": threat
            }

        by_fen = defaultdict(dict)
        for (p_fen, uci), st in result.items():
            by_fen[p_fen][uci] = st
        if not hasattr(self, "_user_stats_by_fen"):
            self._user_stats_by_fen = {}
        self._user_stats_by_fen[color] = by_fen

        self._user_stats_cache[color] = result
        self._user_stats_time = now
        return result

    # -- 2000+ explorer read-through cache (audit ABUSE-17, SYNC-20) ------------------- #

    def _read_explorer_cache(self, norm_fen: str, ratings: str) -> Optional[Dict]:
        """Return the *validated* cached payload for a key, or `None` on miss/stale/corrupt.

        Audit SYNC-20: `explorer_cache` is a plain table whose `data_json` is whatever a
        previous process managed to write. A row holding a non-finite `total_games` used to
        make `int(...)` raise `OverflowError`/`ValueError` in `get_game_gaps` (500) or poison
        `/api/repertoire/tree` (400) and `/api/blunders/{id}` (422). Every number and every
        move is now coerced before it is trusted, and a row that cannot be understood is
        treated as a miss (never as a fact).
        """
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT data_json, updated_at FROM explorer_cache WHERE fen = ? AND ratings = ?",
                (norm_fen, ratings))
            row = cursor.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        if time.time() - (row["updated_at"] or 0) >= 86400 * 7:
            return None
        try:
            parsed = json.loads(row["data_json"])
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        payload = sanitize_explorer_payload(parsed)
        if payload is None:
            return None
        return payload

    def _fetch_explorer(self, fen: str, norm_fen: str, ratings: str) -> Optional[Dict]:
        """One live explorer call. Returns the sanitised payload, or `None` on any failure.

        `None` is the *only* representation of "we could not check" (audit ABUSE-17): it is
        never turned into `total_games: 0`, which is a factual claim about the database.
        """
        url = "https://explorer.lichess.org/lichess"
        params = {"fen": fen, "ratings": ratings, "speeds": "blitz,rapid,classical"}
        try:
            resp = requests.get(url, params=params, headers=dict(self.lichess_client.headers), timeout=10)
        except Exception as e:
            print(f"Lichess explorer query failed: {e}")
            return None
        if resp.status_code != 200:
            print(f"Lichess explorer query failed: HTTP {resp.status_code}")
            return None
        try:
            payload = sanitize_explorer_payload(resp.json())
        except Exception as e:
            print(f"Lichess explorer reply could not be parsed: {e}")
            return None
        if payload is None:
            print("Lichess explorer reply discarded: unusable payload shape")
        return payload

    def _store_explorer_payload(self, norm_fen: str, ratings: str, fen: str, payload: Dict) -> None:
        """Persist a validated payload durably (7 days) and in the coalescing window (5 s).

        A *failure* is never stored: it is remembered for `EXPLORER_NEGATIVE_TTL_SECONDS` instead
        (audit ABUSE-17). The in-process entry is what a burst of concurrent callers reads, and it
        is written by the single-flight owner before the waiters are released.
        """
        result_payload = {
            "fen": fen,
            "total_games": coerce_count(payload.get("total_games"), 0),
            "white_total": coerce_count(payload.get("white_total"), 0),
            "draws_total": coerce_count(payload.get("draws_total"), 0),
            "black_total": coerce_count(payload.get("black_total"), 0),
            "moves": [dict(m) for m in payload.get("moves", [])],
            "eval_convention": "white",
        }
        try:
            conn = get_db()
            try:
                conn.execute("""
                INSERT OR REPLACE INTO explorer_cache (fen, ratings, data_json, updated_at)
                VALUES (?, ?, ?, ?)
                """, (norm_fen, ratings, json.dumps(result_payload), int(time.time())))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"explorer_cache write failed (not fatal): {e}")

    def _explorer_single_flight(self, fen: str, ratings: str, producer) -> Tuple[bool, Any]:
        """Run `producer()` at most once at a time per `(fen, ratings)` key.

        Returns `(was_owner, result)`.

        Audit ABUSE-17: 24 concurrent requests for one cold FEN used to make 24 outbound
        calls (and 24 x 8 Stockfish analyses). The first caller performs the fetch; every
        other caller waits on the event and then re-reads the cache, so the amplification is
        gone. `finally`-safe: the event is always set and always removed, so one hung or
        failed fetch can never wedge the key for the process lifetime.
        """
        key = (normalize_fen(fen), ratings)
        with self._explorer_flight_lock:
            event = self._explorer_flight.get(key)
            owner = event is None
            if owner:
                event = threading.Event()
                self._explorer_flight[key] = event
        if not owner:
            # Someone else is already fetching this exact position. Wait (bounded) for them and
            # let them do the work; their result is already in the cache and in the coalescing
            # window by the time this returns, so the caller re-reads it.
            event.wait(timeout=30.0)
            return False, None
        try:
            return True, producer()
        finally:
            with self._explorer_flight_lock:
                self._explorer_flight.pop(key, None)
            event.set()

    def _note_explorer_failure(self, fen: str, ratings: str) -> None:
        key = (normalize_fen(fen), ratings)
        with self._explorer_flight_lock:
            if len(self._explorer_negative) > 512:
                self._explorer_negative.clear()
            self._explorer_negative[key] = time.time() + EXPLORER_NEGATIVE_TTL_SECONDS

    def _explorer_backoff_active(self, fen: str, ratings: str) -> bool:
        key = (normalize_fen(fen), ratings)
        with self._explorer_flight_lock:
            until = self._explorer_negative.get(key)
            if until is None:
                return False
            if until > time.time():
                return True
            self._explorer_negative.pop(key, None)
            return False

    def _clear_explorer_backoff(self, fen: str, ratings: str) -> None:
        with self._explorer_flight_lock:
            self._explorer_negative.pop((normalize_fen(fen), ratings), None)

    def get_explorer_game_count(self, fen: str, ratings: str = "2000,2200,2500") -> int:
        """Fast lookup of total 2000+ games played in a position, checking cache first.

        Audit CODE-6: `explorer_cache` is only ever *read* here (the writer is
        `get_candidate_moves_2000`), so an uncached FEN used to cost a live HTTP request on every
        single call. Answers are therefore memoised in-process, and a *failed* lookup is
        remembered in-process for `EXPLORER_NEGATIVE_TTL_SECONDS` (audit ABUSE-17) so a broken
        upstream costs one timeout per window instead of one per request.

        The returned `0` covers both "genuinely zero" and "could not check"; a caller that must
        not confuse the two (the `min_games` cut-off, the gap list, `coverage_pct`) uses
        :meth:`probe_explorer_game_count`, which is the same lookup plus the evidence flag.
        """
        norm_fen = normalize_fen(fen)
        payload = self._read_explorer_cache(norm_fen, ratings)
        if payload is not None:
            return coerce_count(payload.get("total_games"), 0)

        memo_key = (norm_fen, ratings)
        memo = self._explorer_count_memo.get(memo_key)
        if memo:
            age = time.time() - memo[1]
            if age < (86400 if memo[0][0] > 0 else EXPLORER_NEGATIVE_TTL_SECONDS):
                return memo[0][0]

        if self._explorer_backoff_active(norm_fen, ratings):
            return 0

        def produce():
            raw = self._fetch_explorer_raw_counts(fen, ratings)
            if raw is None:
                self._note_explorer_failure(norm_fen, ratings)
                if len(self._explorer_count_memo) > 512:
                    self._explorer_count_memo.clear()
                self._explorer_count_memo[memo_key] = ((0, False), time.time())
                return None
            if len(self._explorer_count_memo) > 512:
                self._explorer_count_memo.clear()
            self._clear_explorer_backoff(norm_fen, ratings)
            self._explorer_count_memo[memo_key] = ((int(raw), True), time.time())
            return int(raw)

        _owner, total = self._explorer_single_flight(fen, ratings, produce)
        if total is not None:
            return total
        memo = self._explorer_count_memo.get(memo_key)
        return memo[0][0] if memo else 0

    def probe_explorer_game_count(self, fen: str, ratings: str = "2000,2200,2500") -> Tuple[int, bool]:
        """`(total_games, known)` for a position in the 2000+ database.

        `known is False` means "we could not check" (the explorer was unreachable, answered a
        non-200, returned an unusable payload, or is inside the short negative-cache window).
        `known is True` with `0` means a *genuine* zero-game answer. Audit ABUSE-17: those two
        used to be byte-identical on the wire, so an outage read to the user as "this opening is
        not played by anybody above 2000".

        `known` is derived from *evidence* (a cache row, or a memo entry recorded as successful)
        rather than from the number, so a stubbed or overridden :meth:`get_explorer_game_count`
        that answers `0` without recording anything is correctly reported as unknown.
        """
        count = self.get_explorer_game_count(fen, ratings)
        if self._explorer_backoff_active(fen, ratings):
            return 0, False
        norm_fen = normalize_fen(fen)
        payload = self._read_explorer_cache(norm_fen, ratings)
        if payload is not None:
            return coerce_count(payload.get("total_games"), 0), True
        memo = self._explorer_count_memo.get((norm_fen, ratings))
        if memo and memo[0][1] and time.time() - memo[1] < 86400:
            return memo[0]
        return 0, False

    def _fetch_explorer_raw_counts(self, fen: str, ratings: str) -> Optional[int]:
        """`white + draws + black` from the explorer, or `None` when it could not be read."""
        url = "https://explorer.lichess.org/lichess"
        params = {"fen": fen, "ratings": ratings, "speeds": "blitz,rapid,classical"}
        try:
            resp = requests.get(url, params=params, headers=dict(self.lichess_client.headers), timeout=6)
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        return (coerce_count(data.get("white"))
                + coerce_count(data.get("draws"))
                + coerce_count(data.get("black")))

    def get_candidate_moves_2000(self, fen: str, ratings: str = "2000,2200,2500", color: str = "white", evaluate: bool = True) -> Dict:
        """Queries 2000+ Elo Lichess online database (with caching) and evaluates moves, enriched with user personal stats.

        Audit ABUSE-17 / SYNC-20. Three properties this read-through cache now has:

        1. **One validated boundary.** The payload is sanitised by
           :func:`sanitize_explorer_payload` *inside this function* - the only place an
           untrusted upstream JSON document becomes an `explorer_cache` row - and again on
           every read. This matters because `get_repertoire_tree` calls this function
           internally, so an `api.py` route guard could never protect the tree endpoint.
        2. **Single-flight.** A cold position costs exactly ONE upstream call no matter how
           many requests arrive for it.
        3. **A failure is not a zero.** When the explorer is unreachable, times out, answers a
           non-200 or returns an unusable document, the response carries
           `error: "explorer_unavailable"`, `degraded: True` and `total_games: None` instead of
           a fabricated `0`, and the failure is remembered for
           `EXPLORER_NEGATIVE_TTL_SECONDS` so it is not re-paid in full on every request.
        """
        try:
            board = chess.Board(fen)
            if not board.is_valid():
                return {"moves": [], "total_games": 0, "error": "Invalid FEN string"}
        except Exception:
            return {"moves": [], "total_games": 0, "error": "Invalid FEN string"}

        color = (color or "white").lower()
        if color not in ("white", "black"):
            color = "white"

        norm_fen = normalize_fen(fen)
        is_white_turn = board.turn == chess.WHITE

        # 1. Check the validated cache (7-day TTL).
        cached_payload = self._read_explorer_cache(norm_fen, ratings)

        if cached_payload is not None:
            cached_payload = sanitize_explorer_payload(cached_payload, is_white_turn) or cached_payload
            if (cached_payload.get("moves") or cached_payload.get("total_games") == 0) is False:
                cached_payload = None

        degraded = False
        error_code: Optional[str] = None

        if cached_payload is not None:
            # Ensure evaluation convention is always White-centric (+ White advantage, - Black advantage)
            if cached_payload.get("eval_convention") != "white":
                parts = fen.strip().split()
                if len(parts) >= 2 and parts[1] == 'b':
                    for m in cached_payload.get("moves", []):
                        if m.get("eval") is not None:
                            m["eval"] = -m["eval"]
                cached_payload["eval_convention"] = "white"

            total_games = coerce_count(cached_payload.get("total_games"), 0)
            white_total = coerce_count(cached_payload.get("white_total"), 0)
            draws_total = coerce_count(cached_payload.get("draws_total"), 0)
            black_total = coerce_count(cached_payload.get("black_total"), 0)
            candidate_moves = [dict(m) for m in cached_payload.get("moves", [])]
        else:
            # 2. One (and only one) live query per cold key - see `_explorer_single_flight`.
            #
            # The WHOLE miss path (fetch -> validate -> store durably -> score) runs inside the
            # producer, so the waiters are released only once the result is already readable. That
            # is what makes the stampede impossible rather than merely unlikely: a follower that
            # woke up early would otherwise find an empty cache and start a second fetch (and a
            # second batch of up to 8 Stockfish analyses against a pool capped at 2).
            def produce():
                # Re-check the cache now that we own the key: a competing thread may have read
                # the cache (a miss) and only reached the flight lock *after* that thread wrote
                # its row and released the event. Without this re-check each of those threads
                # becomes the next "owner" in turn and the stampede reappears.
                recheck = self._read_explorer_cache(norm_fen, ratings)
                if recheck is not None:
                    return recheck
                fetched = self._fetch_explorer(fen, norm_fen, ratings)
                if fetched is None:
                    self._note_explorer_failure(norm_fen, ratings)
                    return None
                self._clear_explorer_backoff(norm_fen, ratings)
                payload = (sanitize_explorer_payload(fetched, is_white_turn)
                           or {"total_games": 0, "moves": []})
                for m in payload.get("moves", []):
                    if not m.get("uci") and m.get("san"):
                        try:
                            m["uci"] = board.parse_san(m["san"]).uci()
                        except Exception:
                            pass
                # Durable record FIRST (without evals), so even a waiter that times out on the
                # engine lease still gets the data instead of a fabricated zero.
                self._store_explorer_payload(norm_fen, ratings, fen, payload)
                # 3. Fast Stockfish Evaluation of top moves (up to 8 moves) - ONCE per position.
                # The scores are written into `payload["moves"]` itself, so they are stored and
                # returned (and so a later cache hit carries them, as it did before).
                if evaluate:
                    self._evaluate_candidate_moves(board, payload.get("moves", []))
                    self._store_explorer_payload(norm_fen, ratings, fen, payload)
                return payload

            fetched = None
            if self._explorer_backoff_active(norm_fen, ratings):
                # Audit ABUSE-17: a recent failure for this exact key is remembered for
                # EXPLORER_NEGATIVE_TTL_SECONDS, so a broken upstream costs one timeout per
                # window instead of one full timeout per request.
                fetched = {"unavailable": True}
            else:
                _owner, fetched = self._explorer_single_flight(fen, ratings, produce)
                if fetched is None:
                    # Either we own the fetch and it failed, or we waited on another caller
                    # whose fetch failed. Either way the cache may now hold the result.
                    re_read = self._read_explorer_cache(norm_fen, ratings)
                    fetched = re_read if re_read is not None else {"unavailable": True}

            if isinstance(fetched, dict) and fetched.get("unavailable"):
                # An explicit, machine-readable failure. The totals are `None`, NOT 0: a caller
                # must never render an outage as "nobody plays this above 2000". Step 5 below
                # still runs, so the personal-statistics index is built exactly as it is on the
                # success path (the pre-fix failure path fell through to it as well).
                degraded = True
                error_code = "explorer_unavailable"
                total_games = None
                white_total = None
                draws_total = None
                black_total = None
                candidate_moves = []
            else:
                total_games = coerce_count(fetched.get("total_games"), 0)
                white_total = coerce_count(fetched.get("white_total"), 0)
                draws_total = coerce_count(fetched.get("draws_total"), 0)
                black_total = coerce_count(fetched.get("black_total"), 0)
                candidate_moves = [dict(m) for m in fetched.get("moves", [])]

        # 5. Attach personal game stats against responses
        user_stats = self.get_user_position_stats(color)
        moves_with_stats = []
        existing_ucis = set()

        for m in candidate_moves:
            m_copy = dict(m)
            uci = m.get("uci", "")
            existing_ucis.add(uci)
            my_st = user_stats.get((norm_fen, uci))
            if my_st:
                m_copy["my_stats"] = my_st
            else:
                m_copy["my_stats"] = {
                    "games": 0,
                    "wins": 0,
                    "draws": 0,
                    "losses": 0,
                    "win_rate": None,
                    "threat_score": 0.0
                }
            moves_with_stats.append(m_copy)

        # Include any sidelines from user's games not in top master moves
        try:
            board = chess.Board(fen)
            user_by_fen = getattr(self, "_user_stats_by_fen", {}).get(color)
            if user_by_fen is not None:
                sideline_items = user_by_fen.get(norm_fen, {}).items()
            else:
                sideline_items = [(uci, st) for (p_fen, uci), st in user_stats.items() if p_fen == norm_fen]

            for uci, my_st in sideline_items:
                if uci not in existing_ucis:
                    try:
                        move_obj = chess.Move.from_uci(uci)
                        san = board.san(move_obj)
                        moves_with_stats.append({
                            "san": san,
                            "uci": uci,
                            "games": 0,
                            "popularity_pct": 0.0,
                            "white_win_pct": 0,
                            "draw_pct": 0,
                            "black_win_pct": 0,
                            "win_rate": 50,
                            "eval": None,
                            "my_stats": my_st
                        })
                    except Exception:
                        pass
        except Exception:
            pass

        # Sort moves primarily by threat_score DESC, then by popularity_pct DESC, then games DESC
        moves_with_stats.sort(
            key=lambda x: (
                x.get("my_stats", {}).get("threat_score", 0.0),
                float(x.get("popularity_pct") or 0.0),
                x.get("games", 0)
            ),
            reverse=True
        )

        result = {
            "fen": fen,
            "total_games": total_games,
            "white_total": white_total,
            "draws_total": draws_total,
            "black_total": black_total,
            "user_games_total": self.get_total_games_count(),
            "moves": moves_with_stats
        }
        if degraded or error_code:
            result["degraded"] = True
            result["error"] = error_code or "explorer_unavailable"
        return result

    def _evaluate_candidate_moves(
        self,
        board: "chess.Board",
        candidate_moves: List[Dict],
        max_moves: int = 8,
        depth: int = 10
    ) -> Optional[str]:
        """Scores the top `max_moves` candidates with Stockfish (White's point of view).

        Audit BUILDER-05 / ABUSE-06: this used to drive ONE process-wide, unsynchronised
        `chess.engine.SimpleEngine` (`self._get_engine()`) from whatever anyio worker thread
        happened to be serving the request. `SimpleEngine.analyse()` has no internal mutual
        exclusion - two concurrent `/api/repertoire/explore` + `/api/repertoire/tree`
        requests interleaved their UCI commands on one pipe, the protocol desynchronised
        permanently (`ping() -> AssertionError: CommandState.NEW`), and because
        `_get_engine()` never health-checked an existing engine, every later candidate row
        came back with `eval: null` for the lifetime of the process. ATK-1 measured a
        maximum of two threads inside `analyse()` at once.

        The engine now comes from the process-wide bounded pool in `backend.evaluator`
        (`get_sync_engine_pool()`), which hands it to exactly one caller at a time,
        pings it on checkout and bounds the wait with `EngineBusy`. A failure to obtain an
        engine is **degraded, not fatal**: the candidate rows simply carry no `eval`, which
        is what the UI already renders for an unevaluated position.
        """
        targets = [c for c in (candidate_moves or []) if c.get("uci")][:max_moves]
        if not targets:
            return None

        pool = self._engine_pool()
        try:
            engine = pool.acquire()
        except (EngineBusy, EngineUnavailable, FileNotFoundError) as exc:
            # No engine for this request: the candidate table is still correct, it just has
            # no evals. Never raise - this is a GET.
            print(f"RepertoireManager: candidate evaluation skipped ({exc})")
            return str(exc)

        healthy = True
        try:
            for cand in targets:
                try:
                    test_b = board.copy()
                    test_b.push(chess.Move.from_uci(cand["uci"]))
                    info = engine.analyse(test_b, chess.engine.Limit(depth=depth))
                    # Always score from White's perspective (+ White advantage, - Black).
                    cand["eval"] = info["score"].white().score(mate_score=10000)
                except Exception:
                    cand["eval"] = None
        except BaseException:
            # Something escaped the per-candidate guard: the engine's state is unknown, so
            # it is terminated rather than recycled.
            healthy = False
            raise
        finally:
            if healthy:
                pool.release(engine)
            else:
                pool.discard(engine)
        return None

    def add_repertoire_move(
        self,
        repertoire: str,
        parent_fen: str,
        move_uci: str,
        is_user_move: bool = True,
        popularity_pct: Optional[float] = 0.0,
        win_rate: Optional[float] = 0.0,
        eval_score: Optional[int] = 0,
        is_important: Optional[bool] = None,
        comment: Optional[str] = None,
        arrows: Optional[str] = None,
        study_name: Optional[str] = None,
        replace_existing: bool = False,
        conn: Optional[Any] = None
    ) -> Dict:
        """Adds a move to the repertoire tree.

        **Safe by default (audit SCOUT-02 / BUILDER-02 / BUILDER-01 / SCOUT-03).** A repertoire
        permits only one user move per position, and enforcing that used to mean *delete the
        prepared move and recursively prune its whole subtree*. Every caller that was not the
        user explicitly saying "this is my move here" went through that: the Scout whole-line
        import, ``import_game_opening``, and even ``update_move_metadata`` (a note or a star).
        The prepared line therefore disappeared with no confirmation, no undo, and - because
        the prune left the ``study_moves`` membership rows behind - came **back** on the next
        app start as a second user move at the same position.

        The rule now is:

        * A *different* user move is already prepared at this position and
          ``replace_existing`` is **False** (the default): **nothing is written, nothing is
          deleted and nothing is pruned.** The call returns
          ``{"status": "conflict", "code": "prepared_move_conflict", "existing_*", ...}`` so the
          caller can surface the conflict and ask for consent. Import paths in this module
          (``import_game_opening``) treat that as "abandon the rest of the line".
        * ``replace_existing=True`` is the explicit-consent path (a deliberate "make this my
          move here" action). It deletes the prepared row **and its ``study_moves`` rows**, then
          prunes the replaced move's children through the shared
          :meth:`_prune_orphaned_children` helper - so the membership rows go with the moves and
          ``init_db()`` can no longer resurrect the discarded branch - capped at
          :data:`REPLACE_PRUNE_MAX_DEPTH` levels so a write can never cascade through a
          transposition into lines the user never touched.
        * Re-adding a move that is ALREADY in the tree is an idempotent UPSERT (audit CODE-1),
          never a delete-then-reinsert. Field policy for the upsert:
          - Refreshed unconditionally (derived data, safe to recompute): ``fen``, ``move_san``,
            ``is_user_move``, ``popularity_pct``, ``win_rate``, ``eval_score``, ``opening_name``,
            ``eco``.
          - User-authored, preserved when the caller does not send them: ``is_important``,
            ``comment``, ``arrows``. An explicitly supplied value (including an explicit empty
            string) still wins, so the UI can clear a note on purpose.
          - ``study_name`` is a **display cache only** (audit SCOUT-03). The authoritative
            membership is the ``study_moves`` row created below, so a move can belong to several
            studies. A non-empty ``study_name`` on the row is never overwritten by a different
            study name (an overlapping import can no longer hijack a curated study), and it is
            still filled in when the column is empty, or when the row is not a member of any
            other study yet.
          - Never written by this function: the spaced-repetition state (``times_practiced``,
            ``times_correct``, ``last_practiced``, ``next_review``, ``interval_days``), the row
            ``id`` and ``created_at``. Re-importing a game or re-seeding a line can therefore no
            longer reset the user's study progress (a 2/2 mastered move used to fall back to 0/0).
        """
        repertoire = (repertoire or "").lower()
        if repertoire not in ("white", "black"):
            return {"status": "error", "message": f"Invalid repertoire color: {repertoire}. Must be 'white' or 'black'."}

        is_user_move = True if is_user_move is None else bool(is_user_move)

        parts = (parent_fen or "").strip().split()
        if len(parts) < 4 or parts[0].count('/') != 7:
            return {"status": "error", "message": "Invalid FEN string: must contain at least 4 fields"}

        try:
            board = chess.Board(parent_fen)
            if not board.is_valid():
                return {"status": "error", "message": "Invalid FEN string"}
        except Exception as e:
            return {"status": "error", "message": f"Invalid FEN string: {e}"}

        # Turn vs Repertoire validation (BUG-10)
        if repertoire == "white":
            if is_user_move and board.turn != chess.WHITE:
                return {"status": "error", "message": "User moves for white repertoire must be on White's turn"}
            elif not is_user_move and board.turn != chess.BLACK:
                return {"status": "error", "message": "Opponent moves for white repertoire must be on Black's turn"}
        elif repertoire == "black":
            if is_user_move and board.turn != chess.BLACK:
                return {"status": "error", "message": "User moves for black repertoire must be on Black's turn"}
            elif not is_user_move and board.turn != chess.WHITE:
                return {"status": "error", "message": "Opponent moves for black repertoire must be on White's turn"}

        try:
            move_obj = chess.Move.from_uci(move_uci)
            if move_obj not in board.legal_moves:
                return {"status": "error", "message": f"Illegal move: {move_uci}"}
            san = board.san(move_obj)
            board.push(move_obj)
            norm_after = normalize_fen(board.fen())
        except Exception as e:
            return {"status": "error", "message": f"Illegal move: {e}"}

        norm_parent = normalize_fen(parent_fen)
        safe_popularity = float(popularity_pct or 0.0)
        safe_win_rate = float(win_rate or 0.0)
        safe_eval = int(eval_score or 0)

        # Determine opening name from encyclopedia
        opening_info = self.opening_book.get_opening_for_fen(board.fen()) or {"eco": "", "name": ""}

        own_conn = conn is None
        if own_conn:
            conn = get_db()
        cursor = conn.cursor()

        # If user move: enforce ONE primary move per position.
        #
        # Audit TRAINER-01: "one position" is the LOGICAL position, i.e. the first three FEN
        # tokens (placement + side to move + castling). `normalize_fen` keeps the en-passant
        # token, so two move orders that transpose into the same position differ only there and
        # the exact 4-token comparison happily stored a second prepared move beside the first.
        # The trainer then resolved the position with a `LIMIT 1` and could only ever grade one
        # of the two, so a `train/next` drill that served the other one was unpassable forever.
        # The identity used by the guard is now the same one the read/grade path uses.
        if is_user_move:
            norm_parent_3 = " ".join(norm_parent.split()[:3])
            cursor.execute("""
            SELECT id, parent_fen, fen, move_uci, move_san, study_name FROM repertoire_moves
            WHERE repertoire = ? AND is_user_move = 1
              AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?)
            ORDER BY (parent_fen = ?) DESC, id ASC
            """, (repertoire, norm_parent, norm_parent_3, f"{norm_parent_3} %", norm_parent))
            existing_user_move = cursor.fetchone()
            if existing_user_move and existing_user_move["move_uci"] != move_uci:
                if not replace_existing:
                    # SCOUT-02: report the conflict and touch nothing. No DELETE, no prune.
                    if own_conn:
                        conn.close()
                    return {
                        "status": "conflict",
                        "code": "prepared_move_conflict",
                        "message": (
                            f"A move is already prepared at this position for {repertoire}: "
                            f"{existing_user_move['move_san']}. Importing {san} would replace it. "
                            f"Nothing was changed; re-send with replace_existing=true to confirm."
                        ),
                        "repertoire": repertoire,
                        "parent_fen": norm_parent,
                        "is_user_move": True,
                        "attempted_uci": move_uci,
                        "attempted_san": san,
                        "existing_id": existing_user_move["id"],
                        "existing_uci": existing_user_move["move_uci"],
                        "existing_san": existing_user_move["move_san"],
                        "requires_confirmation": True,
                        "deleted_moves": 0,
                        "pruned_moves": 0,
                    }
                # Explicit consent: remove the prepared move AND its membership rows, then let
                # the shared helper prune the children (which removes *their* membership rows too
                # - BUILDER-01: without that, init_db()'s "restore moves from study_moves"
                # resurrected the entire discarded branch on the next boot).
                studies_before = self._studies_with_membership(cursor)
                cursor.execute("""
                DELETE FROM repertoire_moves
                WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1
                """, (repertoire, existing_user_move["parent_fen"]))
                cursor.execute("""
                DELETE FROM study_moves
                WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?
                """, (repertoire, existing_user_move["parent_fen"], existing_user_move["move_uci"]))
                self._prune_orphaned_children(
                    cursor, repertoire, [existing_user_move["fen"]],
                    max_depth=REPLACE_PRUNE_MAX_DEPTH,
                )
                self._drop_emptied_studies(cursor, studies_before)
            elif existing_user_move and existing_user_move["parent_fen"] != norm_parent:
                # TRAINER-01: the SAME move is already prepared for the same LOGICAL position
                # under a different en-passant token. Writing a second row would recreate the
                # "two prepared moves at one position" defect, so this is an idempotent
                # no-op - and crucially NOT a delete: nothing the user prepared is removed.
                existing_id = existing_user_move["id"]
                if own_conn:
                    conn.close()
                return {
                    "status": "success",
                    "code": "already_prepared_at_transposition",
                    "already_present": True,
                    "transposed": True,
                    "message": (
                        f"{existing_user_move['move_san']} is already your prepared move for this "
                        f"position (reached by a different move order, so the stored en-passant "
                        f"token differs). Nothing was duplicated and nothing was removed."
                    ),
                    "repertoire": repertoire,
                    "parent_fen": existing_user_move["parent_fen"],
                    "is_user_move": True,
                    "move_uci": existing_user_move["move_uci"],
                    "move_san": existing_user_move["move_san"],
                    "id": existing_id,
                    "deleted_moves": 0,
                    "pruned_moves": 0,
                }
            # NOTE (CODE-1): when the move already prepared for this position IS the move being
            # added, nothing is deleted. The row (and its id, notes, arrows, importance and
            # spaced-repetition state) is updated in place by the upsert below.

        # Check for existing metadata if not explicitly provided
        cursor.execute("""
        SELECT is_important, comment, arrows, study_name FROM repertoire_moves
        WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?
        """, (repertoire, norm_parent, move_uci))
        existing_meta = cursor.fetchone()

        # Audit SCOUT-03: which studies does this move belong to? The relation is `study_moves`;
        # `study_name` is only a display cache of ONE of them.
        cursor.execute("""
        SELECT s.name FROM study_moves sm
        JOIN repertoire_studies s ON s.id = sm.study_id
        WHERE sm.repertoire = ? AND sm.parent_fen = ? AND sm.move_uci = ?
        """, (repertoire, norm_parent, move_uci))
        member_studies = sorted({(r["name"] or "").strip() for r in cursor.fetchall() if (r["name"] or "").strip()})
        cached_study = (existing_meta["study_name"] if existing_meta else "") or ""
        requested_study = (study_name or "").strip()

        final_important = int(is_important) if is_important is not None else (existing_meta["is_important"] if existing_meta and existing_meta["is_important"] is not None else 0)
        final_comment = (comment.strip() if comment is not None else (existing_meta["comment"] if existing_meta and existing_meta["comment"] else ""))
        final_arrows = (arrows.strip() if arrows is not None else (existing_meta["arrows"] if existing_meta and existing_meta["arrows"] else ""))
        if requested_study:
            # Fill the display cache when it is free, or when it is ours to fill; never let a
            # second study hijack the label of a move the user already filed somewhere else.
            if not cached_study or cached_study == requested_study or not member_studies:
                final_study = requested_study
            else:
                final_study = cached_study
        else:
            final_study = cached_study

        now_ts = int(time.time())
        # CODE-1: upsert instead of INSERT OR REPLACE. INSERT OR REPLACE deletes the conflicting
        # row first, which reset every column that is not named in the INSERT list to its schema
        # DEFAULT (is_important, comment, arrows and the whole spaced-repetition state). The
        # conflict target is the UNIQUE(repertoire, parent_fen, move_uci) key, so the existing row
        # keeps its id, created_at and SRS counters; only derived columns are refreshed below.
        cursor.execute("""
        INSERT INTO repertoire_moves (
            repertoire, parent_fen, fen, move_uci, move_san,
            is_user_move, popularity_pct, win_rate, eval_score,
            opening_name, eco, is_important, comment, arrows, study_name, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repertoire, parent_fen, move_uci) DO UPDATE SET
            fen = excluded.fen,
            move_san = excluded.move_san,
            is_user_move = excluded.is_user_move,
            popularity_pct = excluded.popularity_pct,
            win_rate = excluded.win_rate,
            eval_score = excluded.eval_score,
            opening_name = excluded.opening_name,
            eco = excluded.eco,
            is_important = excluded.is_important,
            comment = excluded.comment,
            arrows = excluded.arrows,
            study_name = excluded.study_name
        """, (
            repertoire, norm_parent, norm_after, move_uci, san,
            1 if is_user_move else 0, safe_popularity, safe_win_rate, safe_eval,
            opening_info.get("name", ""), opening_info.get("eco", ""),
            final_important, final_comment, final_arrows, final_study,
            now_ts
        ))

        # Study membership is a relation: create the study if needed and link the move in
        # `study_moves` (audit SCOUT-03). The link uses the *requested* study, not the display
        # cache, so re-importing an already-filed line with a second study name ADDS that study
        # instead of stealing the label - and the relation is what `get_studies`,
        # `get_trainer_progress` and the drill scheduler read.
        link_study = requested_study or final_study
        if link_study:
            cursor.execute("INSERT OR IGNORE INTO repertoire_studies (name, repertoire, description, created_at, updated_at) VALUES (?, ?, '', ?, ?)",
                           (link_study, repertoire, now_ts, now_ts))
            cursor.execute("SELECT id FROM repertoire_studies WHERE name = ? AND repertoire = ?", (link_study, repertoire))
            st_row = cursor.fetchone()
            if st_row:
                cursor.execute("INSERT OR IGNORE INTO study_moves (study_id, repertoire, parent_fen, move_uci) VALUES (?, ?, ?, ?)",
                               (st_row["id"], repertoire, norm_parent, move_uci))
                cursor.execute("UPDATE repertoire_studies SET updated_at = ? WHERE id = ?", (now_ts, st_row["id"]))

        if own_conn:
            conn.commit()
            conn.close()
        self._invalidate_overlap_index()

        return {
            "status": "success",
            "san": san,
            "uci": move_uci,
            "fen": norm_after,
            "is_user_move": is_user_move,
            "is_important": bool(final_important),
            "comment": final_comment,
            "arrows": final_arrows,
            "study_name": final_study,
            "studies": member_studies + ([final_study] if final_study and final_study not in member_studies else [])
        }

    def delete_repertoire_branch(self, repertoire: str, parent_fen: str, move_uci: str):
        """Recursively deletes a move and all children branches originating from it.

        Audit FIXER-A escalation 4 / CODE-3: the lookup used a bare
        `SELECT ... WHERE parent_fen = <3-token> OR parent_fen LIKE '<3-token> %'` with no
        `ORDER BY`, and the DELETE repeated that predicate, so a position whose en-passant
        token differed could delete the *other* position's row. The target row is now chosen
        deterministically (exact 4-token parent first, then the most important, then the
        lowest id - the same tie-breaker `evaluate_trainer_move` uses) and **only that row**
        is deleted.
        """
        repertoire = (repertoire or "").lower()
        if repertoire not in ("white", "black"):
            return
        parts = (parent_fen or "").strip().split()
        if len(parts) < 4 or parts[0].count('/') != 7:
            return
        norm_parent = normalize_fen(parent_fen)
        norm_3 = " ".join(norm_parent.split()[:3])
        conn = get_db()
        cursor = conn.cursor()

        # Find the destination fen
        cursor.execute("""
        SELECT id, parent_fen, fen FROM repertoire_moves
        WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND move_uci = ?
        ORDER BY (parent_fen = ?) DESC, is_important DESC, id ASC
        LIMIT 1
        """, (repertoire, norm_parent, norm_3, f"{norm_3} %", move_uci, norm_parent))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return

        child_fen = row["fen"]
        studies_before = self._studies_with_membership(cursor)

        # Delete target move (by primary key: the fuzzy predicate must not fan out).
        cursor.execute("DELETE FROM repertoire_moves WHERE id = ?", (row["id"],))
        cursor.execute("""
        DELETE FROM study_moves
        WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?
        """, (repertoire, row["parent_fen"], move_uci))

        # Recursively delete children
        self._prune_orphaned_children(cursor, repertoire, [child_fen])
        # A study that *had* moves and now has none is a ghost in the study dropdown
        # (audit SCOUT-03). A study that was created empty by the user is left alone.
        self._drop_emptied_studies(cursor, studies_before)

        conn.commit()
        conn.close()
        self._invalidate_overlap_index()

    @staticmethod
    def _studies_with_membership(cursor) -> set:
        """Ids of the studies that currently own at least one move (audit SCOUT-03).

        Taken *before* a destructive operation so the studies that become empty because of it
        can be identified afterwards. A study the user created and has not filled yet is not in
        this set, so it can never be mistaken for a ghost.
        """
        return {r["study_id"] for r in cursor.execute(
            "SELECT DISTINCT study_id FROM study_moves"
        ).fetchall()}

    @staticmethod
    def _drop_emptied_studies(cursor, previously_populated) -> int:
        """Delete the studies that lost their last move (and are no longer referenced by any
        `repertoire_moves.study_name`). Only studies that were populated before the operation
        are considered, so a deliberately empty study survives. Runs in the caller's
        transaction. Audit SCOUT-03: an overlapping import used to leave a study in the
        dropdown claiming N moves while owning none."""
        dropped = 0
        for study_id in sorted(previously_populated or ()):
            still_linked = cursor.execute(
                "SELECT 1 FROM study_moves WHERE study_id = ? LIMIT 1", (study_id,)
            ).fetchone()
            if still_linked is not None:
                continue
            row = cursor.execute("SELECT name, repertoire FROM repertoire_studies WHERE id = ?",
                                 (study_id,)).fetchone()
            if row is None:
                continue
            still_labelled = cursor.execute(
                "SELECT 1 FROM repertoire_moves WHERE study_name = ? LIMIT 1", (row["name"],)
            ).fetchone()
            if still_labelled is not None:
                # Some move still displays this study; keep the row rather than orphaning it.
                continue
            cursor.execute("DELETE FROM repertoire_studies WHERE id = ?", (study_id,))
            dropped += 1
        return dropped

    def _prune_orphaned_children(self, cursor, repertoire: str, start_fens: List[str],
                                 max_depth: Optional[int] = None) -> int:
        """Recursively deletes the given positions' children while they are no longer reachable from any
        remaining move of the repertoire. Shared with `delete_study` (audit CODE-17) so a study deletion
        cannot leave dangling children behind, and with `add_repertoire_move`'s explicit-consent
        replace path (audit BUILDER-01) so a write cleans up the `study_moves` membership rows too -
        otherwise `init_db()`'s "restore moves from study_moves" resurrects the discarded branch on
        the next boot and the position ends up with two user moves.

        `max_depth` bounds the cascade: level 1 removes the direct children of `start_fens`,
        level 2 their children, and so on. `None` means unbounded, which is what the two *explicit*
        delete endpoints want (the user asked for the whole branch). A write passes
        :data:`REPLACE_PRUNE_MAX_DEPTH`, because the cascade matches positions with a 3-token FEN
        `LIKE` and can therefore walk into a transposed line the user never touched.

        Returns the number of `repertoire_moves` rows removed. Must run inside the caller's
        transaction."""
        start_norm = normalize_fen(chess.STARTING_FEN)
        start_3 = " ".join(start_norm.split()[:3])
        to_delete = [(f, 1) for f in (start_fens or []) if f]
        visited = set()
        removed = 0
        while to_delete:
            curr, depth = to_delete.pop()
            if max_depth is not None and depth > int(max_depth):
                continue
            norm_curr = normalize_fen(curr)
            curr_3 = " ".join(norm_curr.split()[:3])

            # Root protection: never cascade delete branches originating from the starting root position
            if norm_curr == start_norm or curr_3 == start_3:
                continue

            if norm_curr in visited:
                continue
            visited.add(norm_curr)

            cursor.execute("""
            SELECT COUNT(*) as parent_count FROM repertoire_moves
            WHERE repertoire = ? AND (fen = ? OR fen = ? OR fen LIKE ?)
            """, (repertoire, norm_curr, curr_3, f"{curr_3} %"))
            if cursor.fetchone()["parent_count"] > 0:
                # Transposition: this position is still reached by another branch
                continue
            cursor.execute("""
            SELECT fen FROM repertoire_moves
            WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?)
            """, (repertoire, norm_curr, curr_3, f"{curr_3} %"))
            next_fens = [r["fen"] for r in cursor.fetchall()]
            cursor.execute("""
            DELETE FROM repertoire_moves
            WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?)
            """, (repertoire, norm_curr, curr_3, f"{curr_3} %"))
            cursor.execute("""
            DELETE FROM study_moves
            WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?)
            """, (repertoire, norm_curr, curr_3, f"{curr_3} %"))
            removed += len(next_fens)
            to_delete.extend((f, depth + 1) for f in next_fens)
        return removed

    def clear_repertoire_database(self, repertoire: Optional[str] = None) -> int:
        """
        Completely resets and clears repertoire moves from the database.
        If repertoire ('white' or 'black') is specified, only that color is cleared.
        If None or 'all', wipes the entire repertoire database and resets the autoincrement sequence.
        Returns the number of moves deleted.
        """
        conn = get_db()
        cursor = conn.cursor()
        repertoire_clean = (repertoire or "").lower().strip()
        if repertoire_clean in ("white", "black"):
            cursor.execute("SELECT COUNT(*) as cnt FROM repertoire_moves WHERE repertoire = ?", (repertoire_clean,))
            count = cursor.fetchone()["cnt"]
            cursor.execute("DELETE FROM repertoire_moves WHERE repertoire = ?", (repertoire_clean,))
            cursor.execute("DELETE FROM study_moves WHERE repertoire = ?", (repertoire_clean,))
            cursor.execute("DELETE FROM repertoire_studies WHERE repertoire = ?", (repertoire_clean,))
        else:
            cursor.execute("SELECT COUNT(*) as cnt FROM repertoire_moves")
            count = cursor.fetchone()["cnt"]
            cursor.execute("DELETE FROM repertoire_moves")
            cursor.execute("DELETE FROM study_moves")
            cursor.execute("DELETE FROM repertoire_studies")
            try:
                cursor.execute("DELETE FROM sqlite_sequence WHERE name IN ('repertoire_moves', 'repertoire_studies')")
            except Exception:
                pass
        conn.commit()
        conn.close()
        self._user_stats_cache = {}
        self._user_stats_time = 0
        self._invalidate_overlap_index()
        return count

    def get_studies(self, repertoire: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns all studies for the given repertoire (or all repertoires) with move counts."""
        conn = get_db()
        cursor = conn.cursor()
        repertoire = (repertoire or "").lower().strip()
        if repertoire in ("white", "black"):
            cursor.execute("""
            SELECT s.id, s.name, s.repertoire, s.description, s.created_at, s.updated_at,
                   COUNT(DISTINCT sm.parent_fen || ':' || sm.move_uci) as total_moves,
                   SUM(CASE WHEN rm.is_user_move = 1 THEN 1 ELSE 0 END) as user_moves
            FROM repertoire_studies s
            LEFT JOIN study_moves sm ON sm.study_id = s.id
            LEFT JOIN repertoire_moves rm ON rm.repertoire = sm.repertoire AND rm.parent_fen = sm.parent_fen AND rm.move_uci = sm.move_uci
            WHERE s.repertoire = ?
            GROUP BY s.id
            ORDER BY s.updated_at DESC, s.name ASC
            """, (repertoire,))
        else:
            cursor.execute("""
            SELECT s.id, s.name, s.repertoire, s.description, s.created_at, s.updated_at,
                   COUNT(DISTINCT sm.parent_fen || ':' || sm.move_uci) as total_moves,
                   SUM(CASE WHEN rm.is_user_move = 1 THEN 1 ELSE 0 END) as user_moves
            FROM repertoire_studies s
            LEFT JOIN study_moves sm ON sm.study_id = s.id
            LEFT JOIN repertoire_moves rm ON rm.repertoire = sm.repertoire AND rm.parent_fen = sm.parent_fen AND rm.move_uci = sm.move_uci
            GROUP BY s.id
            ORDER BY s.updated_at DESC, s.name ASC
            """)
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        for r in rows:
            r["total_moves"] = r["total_moves"] or 0
            r["user_moves"] = r["user_moves"] or 0
        return rows

    def get_study_by_name(self, name: str, repertoire: str) -> Optional[Dict[str, Any]]:
        """Returns a study by name and repertoire."""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM repertoire_studies WHERE name = ? AND repertoire = ?", (name.strip(), repertoire.lower().strip()))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def create_study(self, name: str, repertoire: str, description: str = "") -> Dict[str, Any]:
        """Creates a new study session if it does not already exist."""
        # Sanitize name: normalize whitespace and strip zero-width characters (\u200b, \u200c, \u200d, \ufeff)
        name = re.sub(r'[\s\u200b\u200c\u200d\ufeff]+', ' ', name or "").strip()
        repertoire = (repertoire or "").lower().strip()
        if not name:
            return {"status": "error", "message": "Study name cannot be empty"}
        if repertoire not in ("white", "black"):
            return {"status": "error", "message": "Repertoire must be 'white' or 'black'"}

        conn = get_db()
        cursor = conn.cursor()
        now_ts = int(time.time())
        try:
            cursor.execute("""
            INSERT INTO repertoire_studies (name, repertoire, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """, (name, repertoire, description or "", now_ts, now_ts))
            study_id = cursor.lastrowid
            conn.commit()
            return {
                "status": "success",
                "study": {
                    "id": study_id,
                    "name": name,
                    "repertoire": repertoire,
                    "description": description or "",
                    "created_at": now_ts,
                    "updated_at": now_ts,
                    "total_moves": 0,
                    "user_moves": 0
                }
            }
        except sqlite3.IntegrityError:
            conn.close()
            existing = self.get_study_by_name(name, repertoire)
            return {"status": "exists", "study": existing, "message": f"Study '{name}' already exists."}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def delete_study(self, study_id: int, delete_moves: bool = False) -> Dict[str, Any]:
        """Deletes a study session, optionally deleting the moves that belong *exclusively* to it.

        Audit CODE-17: `delete_moves=True` used to delete by the free-text `study_name` **column**,
        which (a) destroyed moves shared with another study, (b) missed moves linked only through the
        `study_moves` relation and (c) had no child cascade, so the tree was left full of orphans.
        Study membership is now read from `study_moves` (the real relation), shared moves are kept and
        merely unlinked, and the children of a deleted move are pruned with the same helper
        `delete_repertoire_branch` uses. The `study_name` column of every surviving move is re-pointed
        at another study that still claims it (or cleared), so nothing keeps pointing at a study that
        no longer exists.
        """
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT * FROM repertoire_studies WHERE id = ?", (study_id,))
            row = cursor.fetchone()
            if not row:
                return {"status": "error", "message": "Study not found"}

            study = dict(row)
            study_name = study["name"]

            # Moves linked to THIS study through the study_moves relation.
            cursor.execute("""
            SELECT sm.repertoire, sm.parent_fen, sm.move_uci
            FROM study_moves sm
            WHERE sm.study_id = ?
            """, (study_id,))
            linked = [(r["repertoire"], r["parent_fen"], r["move_uci"]) for r in cursor.fetchall()]

            deleted_moves = 0
            kept_moves = 0
            orphaned_fens: Dict[str, List[str]] = defaultdict(list)

            for (rep, p_fen, uci) in linked:
                cursor.execute("""
                SELECT fen, study_name FROM repertoire_moves
                WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?
                """, (rep, p_fen, uci))
                move_row = cursor.fetchone()

                # Studies (other than this one) that still claim the move.
                cursor.execute("""
                SELECT s.name FROM study_moves sm
                JOIN repertoire_studies s ON s.id = sm.study_id
                WHERE sm.repertoire = ? AND sm.parent_fen = ? AND sm.move_uci = ? AND sm.study_id != ?
                """, (rep, p_fen, uci, study_id))
                other_studies = [r["name"] for r in cursor.fetchall()]

                column_owner = ((move_row["study_name"] or "").strip() if move_row else "")

                if delete_moves and move_row is not None and not other_studies and (
                    not column_owner or column_owner == study_name
                ):
                    # Exclusive to this study: remove the move and let the prune cascade take its
                    # orphaned children (and their study_moves links) with it.
                    if move_row["fen"]:
                        orphaned_fens[rep].append(move_row["fen"])
                    cursor.execute("""
                    DELETE FROM repertoire_moves
                    WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?
                    """, (rep, p_fen, uci))
                    deleted_moves += 1
                    continue

                kept_moves += 1
                # The move survives: never leave its study_name pointing at the deleted study.
                if move_row is not None and column_owner == study_name:
                    cursor.execute("""
                    UPDATE repertoire_moves SET study_name = ?
                    WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?
                    """, (other_studies[0] if other_studies else "", rep, p_fen, uci))

            for rep, fens in orphaned_fens.items():
                self._prune_orphaned_children(cursor, rep, fens)

            cursor.execute("DELETE FROM study_moves WHERE study_id = ?", (study_id,))
            cursor.execute("DELETE FROM repertoire_studies WHERE id = ?", (study_id,))
            conn.commit()
            self._invalidate_overlap_index()
            return {
                "status": "success",
                "message": f"Study '{study_name}' deleted",
                "deleted_moves": deleted_moves,
                "kept_moves": kept_moves
            }
        finally:
            conn.close()

    def group_subtree_into_study(
        self,
        repertoire: str,
        start_fen: str,
        study_name: str,
        include_path_from_root: bool = True,
        path_uci: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Groups all descendant moves starting from start_fen into the named study.
        Optionally links prelude moves leading from starting position to start_fen.
        """
        repertoire = (repertoire or "").lower().strip()
        if repertoire not in ("white", "black"):
            return {"status": "error", "message": "Repertoire must be 'white' or 'black'"}

        cleaned_study = re.sub(r'[\s\u200b\u200c\u200d\ufeff]+', ' ', study_name or "").strip()
        if not cleaned_study:
            return {"status": "error", "message": "Study name cannot be empty"}

        try:
            chess.Board(start_fen)
        except Exception:
            return {"status": "error", "message": "Invalid starting FEN position"}

        norm_start = normalize_fen(start_fen)
        norm_start_3 = " ".join(norm_start.split()[:3])

        # Ensure study exists
        study_res = self.create_study(cleaned_study, repertoire)
        if study_res.get("status") not in ("success", "exists") or not study_res.get("study"):
            return {"status": "error", "message": study_res.get("message", "Failed to create or access study")}
        study_id = study_res["study"]["id"]

        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("""
            SELECT id, parent_fen, fen, move_uci, move_san, is_user_move, study_name
            FROM repertoire_moves
            WHERE repertoire = ?
            """, (repertoire,))
            rows = [dict(r) for r in cursor.fetchall()]

            children_by_parent: Dict[str, List[Dict]] = defaultdict(list)
            moves_by_parent_and_uci: Dict[Tuple[str, str], Dict] = {}
            for r in rows:
                p4 = normalize_fen(r["parent_fen"])
                p3 = " ".join(p4.split()[:3])
                children_by_parent[p4].append(r)
                if p3 != p4:
                    children_by_parent[p3].append(r)
                moves_by_parent_and_uci[(p4, r["move_uci"])] = r
                moves_by_parent_and_uci[(p3, r["move_uci"])] = r

            # BFS to find all descendants starting from start_fen
            queue = [norm_start]
            visited_fens = {norm_start, norm_start_3}
            seen_moves = set()
            descendant_moves = []

            while queue:
                curr_fen = queue.pop(0)
                curr_fen_3 = " ".join(curr_fen.split()[:3])
                cand_moves = children_by_parent.get(curr_fen, []) + [
                    m for m in children_by_parent.get(curr_fen_3, []) if m not in children_by_parent.get(curr_fen, [])
                ]
                for m in cand_moves:
                    m_key = (normalize_fen(m["parent_fen"]), m["move_uci"])
                    if m_key not in seen_moves:
                        seen_moves.add(m_key)
                        descendant_moves.append(m)
                        child_fen = normalize_fen(m["fen"])
                        child_fen_3 = " ".join(child_fen.split()[:3])
                        if child_fen not in visited_fens:
                            visited_fens.add(child_fen)
                            visited_fens.add(child_fen_3)
                            queue.append(child_fen)

            # Optional: find prelude moves from root to start_fen
            prelude_moves = []
            if include_path_from_root:
                start_root = normalize_fen(chess.STARTING_FEN)
                if norm_start != start_root and norm_start_3 != " ".join(start_root.split()[:3]):
                    found_path = False
                    if path_uci and isinstance(path_uci, list):
                        try:
                            b = chess.Board()
                            for u in path_uci:
                                p_fen = normalize_fen(b.fen())
                                p_fen_3 = " ".join(p_fen.split()[:3])
                                mv = chess.Move.from_uci(u)
                                if mv in b.legal_moves:
                                    b.push(mv)
                                    m_row = moves_by_parent_and_uci.get((p_fen, u)) or moves_by_parent_and_uci.get((p_fen_3, u))
                                    if m_row:
                                        m_key = (normalize_fen(m_row["parent_fen"]), m_row["move_uci"])
                                        if m_key not in seen_moves:
                                            prelude_moves.append(m_row)
                            found_path = (normalize_fen(b.fen()) == norm_start or " ".join(normalize_fen(b.fen()).split()[:3]) == norm_start_3)
                        except Exception:
                            found_path = False

                    if not found_path:
                        # Build child -> (parent, move) map to trace backwards
                        parents_map: Dict[str, Tuple[str, Dict]] = {}
                        for r in rows:
                            c4 = normalize_fen(r["fen"])
                            c3 = " ".join(c4.split()[:3])
                            p4 = normalize_fen(r["parent_fen"])
                            parents_map[c4] = (p4, r)
                            parents_map[c3] = (p4, r)

                        curr_back = norm_start
                        curr_back_3 = norm_start_3
                        steps = 0
                        while steps < 50:
                            parent_info = parents_map.get(curr_back) or parents_map.get(curr_back_3)
                            if not parent_info:
                                break
                            p_fen, m_row = parent_info
                            m_key = (normalize_fen(m_row["parent_fen"]), m_row["move_uci"])
                            if m_key not in seen_moves and m_row not in prelude_moves:
                                prelude_moves.insert(0, m_row)
                            if p_fen == start_root or " ".join(p_fen.split()[:3]) == " ".join(start_root.split()[:3]):
                                break
                            curr_back = p_fen
                            curr_back_3 = " ".join(p_fen.split()[:3])
                            steps += 1

            # Insert into study_moves and update repertoire_moves.study_name
            now_ts = int(time.time())
            for m in (prelude_moves + descendant_moves):
                p_norm = normalize_fen(m["parent_fen"])
                cursor.execute("""
                INSERT OR IGNORE INTO study_moves (study_id, repertoire, parent_fen, move_uci)
                VALUES (?, ?, ?, ?)
                """, (study_id, repertoire, p_norm, m["move_uci"]))

            for m in descendant_moves:
                p_norm = normalize_fen(m["parent_fen"])
                cursor.execute("""
                UPDATE repertoire_moves
                SET study_name = ?
                WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?
                """, (cleaned_study, repertoire, p_norm, m["move_uci"]))

            cursor.execute("UPDATE repertoire_studies SET updated_at = ? WHERE id = ?", (now_ts, study_id))
            conn.commit()

            self._invalidate_overlap_index()

            updated_study = self.get_study_by_name(cleaned_study, repertoire)
            return {
                "status": "success",
                "study": updated_study,
                "study_name": cleaned_study,
                "descendant_moves": len(descendant_moves),
                "moves_grouped": len(descendant_moves),
                "prelude_moves": len(prelude_moves),
                "prelude_moves_linked": len(prelude_moves),
                "total_study_moves": len(descendant_moves) + len(prelude_moves)
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def update_move_metadata(
        self,
        repertoire: str,
        parent_fen: str,
        move_uci: str,
        is_important: Optional[bool] = None,
        comment: Optional[str] = None,
        arrows: Optional[str] = None,
        study_name: Optional[str] = None
    ) -> Dict:
        """Updates metadata (importance flag, comment notes, drawn arrows, study name) for a
        repertoire move.

        **This is an annotation endpoint and it can no longer create repertoire structure**
        (audit BUILDER-02). It used to fall through to ``add_repertoire_move()`` when the lookup
        missed, so starring or annotating a move the user had merely played on the board
        *inserted* that move - and, because a repertoire holds one user move per position, the
        insert deleted the prepared move and pruned everything below it. A missing move is now
        refused with ``{"status": "error", "code": "move_not_in_tree", ...}`` (the API layer
        turns that into a 4xx) and nothing at all is written.

        The lookup is also deterministic now (audit CODE-3 / FIXER-A escalation 4): the fuzzy
        3-token/``LIKE`` predicate can match a *different* position that only differs in its
        en-passant token, and a bare ``LIMIT 1`` annotated whichever row came first by rowid.
        """
        repertoire = (repertoire or "").lower().strip()
        if repertoire not in ("white", "black"):
            return {"status": "error", "message": "Invalid repertoire"}
        norm_parent = normalize_fen(parent_fen)
        norm_3 = " ".join(norm_parent.split()[:3])
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("""
        SELECT * FROM repertoire_moves
        WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND move_uci = ?
        ORDER BY (parent_fen = ?) DESC, is_important DESC, id ASC
        LIMIT 1
        """, (repertoire, norm_parent, norm_3, f"{norm_3} %", move_uci, norm_parent))
        row = cursor.fetchone()
        if not row:
            conn.close()
            # BUILDER-02: refuse. An annotation must never mutate repertoire structure, and a
            # `__missing__`-style insert here was a general "delete the user's prepared line"
            # primitive reachable from a single click (or from any client at all).
            return {
                "status": "error",
                "code": "move_not_in_tree",
                "message": (
                    f"{move_uci} is not in your {repertoire} repertoire at that position, so no "
                    f"metadata was changed. Add the move to the repertoire first."
                ),
                "repertoire": repertoire,
                "parent_fen": norm_parent,
                "move_uci": move_uci,
            }

        updates = []
        params = []
        if is_important is not None:
            updates.append("is_important = ?")
            params.append(1 if is_important else 0)
        if comment is not None:
            updates.append("comment = ?")
            params.append(comment.strip())
        if arrows is not None:
            updates.append("arrows = ?")
            params.append(arrows.strip())
        st_name = (study_name or "").strip()
        if st_name:
            # Audit SCOUT-03: `study_name` is a display cache of ONE owning study, the
            # authoritative membership is `study_moves`. Filing a move under a new study adds
            # that study (the user can put one line in several studies - see
            # tests/test_user_behavior_probe.py) and therefore must not silently re-label a move
            # the user had already filed elsewhere. The cache is (re)filled while the move is
            # not a member of any *other* study, or while the cache is empty/ours.
            other_members = [n for n in cursor.execute("""
            SELECT s.name FROM study_moves sm JOIN repertoire_studies s ON s.id = sm.study_id
            WHERE sm.repertoire = ? AND sm.parent_fen = ? AND sm.move_uci = ? AND s.name != ?
            """, (repertoire, row["parent_fen"], move_uci, st_name)).fetchall()]
            cached = (row["study_name"] or "").strip()
            if not other_members or not cached or cached == st_name:
                updates.append("study_name = ?")
                params.append(st_name)

        if updates:
            params.append(row["id"])
            cursor.execute(f"""
            UPDATE repertoire_moves
            SET {', '.join(updates)}
            WHERE id = ?
            """, params)

        if st_name:
            now_ts = int(time.time())
            cursor.execute("INSERT OR IGNORE INTO repertoire_studies (name, repertoire, description, created_at, updated_at) VALUES (?, ?, '', ?, ?)",
                           (st_name, repertoire, now_ts, now_ts))
            cursor.execute("SELECT id FROM repertoire_studies WHERE name = ? AND repertoire = ?", (st_name, repertoire))
            st_row = cursor.fetchone()
            if st_row:
                cursor.execute("INSERT OR IGNORE INTO study_moves (study_id, repertoire, parent_fen, move_uci) VALUES (?, ?, ?, ?)",
                               (st_row["id"], repertoire, row["parent_fen"], move_uci))
                cursor.execute("UPDATE repertoire_studies SET updated_at = ? WHERE id = ?", (now_ts, st_row["id"]))

        conn.commit()

        cursor.execute("SELECT * FROM repertoire_moves WHERE id = ?", (row["id"],))
        updated_row = dict(cursor.fetchone())
        member_studies = sorted({
            (r["name"] or "").strip() for r in cursor.execute("""
            SELECT s.name FROM study_moves sm JOIN repertoire_studies s ON s.id = sm.study_id
            WHERE sm.repertoire = ? AND sm.parent_fen = ? AND sm.move_uci = ?
            """, (repertoire, row["parent_fen"], move_uci)).fetchall() if (r["name"] or "").strip()
        })
        conn.close()
        self._invalidate_overlap_index()
        return {
            "status": "success",
            "move": updated_row,
            "is_important": updated_row.get("is_important", 0),
            "study_name": updated_row.get("study_name", "") or "",
            "studies": member_studies,
            "comment": updated_row.get("comment", "") or "",
            "arrows": updated_row.get("arrows", "") or ""
        }

    def check_line_overlap(self, repertoire: str, moves: List[Dict]) -> Dict:
        """
        Smart overlap detection for a line against the user's repertoire.
        Detects whether:
        1. The whole line is already present in the repertoire.
        2. The start/subline of this line is already present, with new moves extending it.
        3. The repertoire contains a deeper line extending beyond this subline.
        4. A user move or opponent move diverges/replaces a prepared line.
        5. The line is completely new.
        """
        repertoire = (repertoire or "white").lower().strip()
        if repertoire not in ("white", "black"):
            repertoire = "white"

        if not moves:
            return {
                "status": "empty",
                "repertoire": repertoire,
                "total_plies": 0,
                "matched_plies": 0,
                "new_plies": 0,
                "matched_sans": [],
                "new_sans": [],
                "repertoire_continues_deeper": False,
                "repertoire_deepest_ply": 0,
                "divergence_info": None,
                "message": "No moves provided to check."
            }

        moves_by_parent, moves_by_parent_3 = self._load_overlap_index(repertoire)
        return self._evaluate_line_against_index(repertoire, moves, moves_by_parent, moves_by_parent_3)

    def _load_overlap_index(self, repertoire: str) -> Tuple[Dict[str, List[Dict]], Dict[str, List[Dict]]]:
        """`{parent_fen: [move]}` and `{parent_fen_3token: [move]}` for one repertoire.

        Audit SCOUT-11(4)/(5): both overlap entry points used to run a full
        ``SELECT * FROM repertoire_moves`` and rebuild the index on **every** call, so a bulk
        "import everything" (one ``check_line`` per line plus one per line in the tree) paid for
        the same read N times. The index is now built once and reused for
        :data:`OVERLAP_INDEX_TTL_SECONDS`; every write path in this class invalidates it through
        :meth:`_invalidate_overlap_index`, so a stale index can only come from a writer that
        bypasses this class, and the TTL bounds that.
        """
        cached = self._overlap_index.get(repertoire)
        now = time.time()
        if cached is not None and (now - cached[2]) < OVERLAP_INDEX_TTL_SECONDS:
            return cached[0], cached[1]

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT parent_fen, fen, move_uci, move_san, is_user_move, opening_name, eco
            FROM repertoire_moves
            WHERE repertoire = ?
            ORDER BY id ASC
        """, (repertoire,))
        db_rows = [dict(r) for r in cursor.fetchall()]
        cursor.close()
        conn.close()

        moves_by_parent: Dict[str, List[Dict]] = {}
        moves_by_parent_3: Dict[str, List[Dict]] = {}
        for r in db_rows:
            p = r["parent_fen"]
            moves_by_parent.setdefault(p, []).append(r)
            moves_by_parent_3.setdefault(" ".join(p.split()[:3]), []).append(r)

        self._overlap_index[repertoire] = (moves_by_parent, moves_by_parent_3, now)
        return moves_by_parent, moves_by_parent_3

    def _evaluate_line_against_index(
        self,
        repertoire: str,
        moves: List[Dict],
        moves_by_parent: Dict[str, List[Dict]],
        moves_by_parent_3: Dict[str, List[Dict]]
    ) -> Dict:
        start_fen = moves[0].get("fen_before") or chess.STARTING_FEN
        try:
            curr_board = chess.Board(start_fen)
        except Exception:
            curr_board = chess.Board()

        total_plies = len(moves)
        matched_plies = 0
        matched_moves = []
        new_moves = []
        divergence_info = None
        still_matching = True
        last_matched_fen = None

        for idx, m in enumerate(moves):
            fen_before = m.get("fen_before") or curr_board.fen()
            norm_before = normalize_fen(fen_before)
            norm_before_3 = " ".join(norm_before.split()[:3])

            uci = (m.get("uci") or "").strip()
            san = (m.get("san") or "").strip()

            move_obj = None
            if uci:
                try:
                    move_obj = chess.Move.from_uci(uci)
                    if not san and move_obj in curr_board.legal_moves:
                        san = curr_board.san(move_obj)
                except Exception:
                    pass
            elif san:
                try:
                    move_obj = curr_board.parse_san(san)
                    uci = move_obj.uci()
                except Exception:
                    pass

            is_user_turn = (repertoire == "white" and curr_board.turn == chess.WHITE) or \
                           (repertoire == "black" and curr_board.turn == chess.BLACK)

            matching_db_move = None
            existing_at_parent = moves_by_parent.get(norm_before) or moves_by_parent_3.get(norm_before_3) or []

            if still_matching:
                for em in existing_at_parent:
                    if uci and em.get("move_uci") == uci:
                        matching_db_move = em
                        break
                    elif san and em.get("move_san") == san:
                        matching_db_move = em
                        break

                if matching_db_move:
                    matched_plies += 1
                    matched_moves.append({
                        "ply": idx + 1,
                        "uci": uci or matching_db_move.get("move_uci"),
                        "san": san or matching_db_move.get("move_san") or uci,
                        "is_user_move": is_user_turn
                    })
                    last_matched_fen = matching_db_move.get("fen")
                else:
                    still_matching = False
                    if existing_at_parent:
                        if is_user_turn:
                            existing_user_move = next((x for x in existing_at_parent if x.get("is_user_move")), existing_at_parent[0])
                            divergence_info = {
                                "ply": idx + 1,
                                "type": "user_move_alternative",
                                "is_user_turn": True,
                                "existing_uci": existing_user_move.get("move_uci"),
                                "existing_san": existing_user_move.get("move_san"),
                                "new_uci": uci,
                                "new_san": san or uci
                            }
                        else:
                            divergence_info = {
                                "ply": idx + 1,
                                "type": "opponent_branch",
                                "is_user_turn": False,
                                "existing_moves": [x.get("move_san") or x.get("move_uci") for x in existing_at_parent],
                                "new_uci": uci,
                                "new_san": san or uci
                            }
                    new_moves.append({
                        "ply": idx + 1,
                        "uci": uci,
                        "san": san or uci,
                        "is_user_move": is_user_turn
                    })
            else:
                new_moves.append({
                    "ply": idx + 1,
                    "uci": uci,
                    "san": san or uci,
                    "is_user_move": is_user_turn
                })

            if move_obj and move_obj in curr_board.legal_moves:
                curr_board.push(move_obj)
            elif uci:
                try:
                    mo = chess.Move.from_uci(uci)
                    curr_board.push(mo)
                except Exception:
                    pass

        repertoire_continues_deeper = False
        repertoire_deepest_ply = matched_plies

        if last_matched_fen:
            def get_max_depth(fen_str: str, depth: int, visited: set) -> int:
                norm = normalize_fen(fen_str)
                norm_3 = " ".join(norm.split()[:3])
                if norm in visited:
                    return depth
                visited.add(norm)
                children = moves_by_parent.get(norm) or moves_by_parent_3.get(norm_3) or []
                if not children:
                    return depth
                max_d = depth
                for c in children:
                    d = get_max_depth(c["fen"], depth + 1, visited)
                    if d > max_d:
                        max_d = d
                return max_d

            deepest = get_max_depth(last_matched_fen, matched_plies, set())
            if deepest > matched_plies:
                repertoire_continues_deeper = True
                repertoire_deepest_ply = deepest

        if matched_plies == total_plies:
            status = "already_present"
        elif matched_plies > 0:
            status = "partially_present"
        else:
            status = "new"

        rep_cap = repertoire.capitalize()
        matched_sans = [m["san"] for m in matched_moves]
        new_sans = [m["san"] for m in new_moves]

        if status == "already_present":
            if repertoire_continues_deeper:
                diff = repertoire_deepest_ply - total_plies
                diff_word = "ply" if diff == 1 else "plies"
                message = (
                    f"Already present in {rep_cap} repertoire! "
                    f"All {total_plies} plies are in your repertoire tree "
                    f"(which extends {diff} {diff_word} deeper to ply {repertoire_deepest_ply})."
                )
            else:
                message = (
                    f"Already present in {rep_cap} repertoire! "
                    f"All {total_plies} plies are already in your opening tree."
                )
        elif status == "partially_present":
            if divergence_info and divergence_info.get("type") == "user_move_alternative":
                exist_san = divergence_info.get("existing_san") or divergence_info.get("existing_uci")
                new_san = divergence_info.get("new_san") or divergence_info.get("new_uci")
                message = (
                    f"First {matched_plies} plies already in {rep_cap} repertoire. "
                    f"At move {divergence_info['ply']}, {new_san} replaces your prepared {exist_san} for {rep_cap}!"
                )
            else:
                new_cnt = len(new_sans)
                move_word = "move" if new_cnt == 1 else "moves"
                sample_new = " ".join(new_sans[:4]) + ("..." if len(new_sans) > 4 else "")
                message = (
                    f"Start of line is already present in {rep_cap} repertoire "
                    f"({matched_plies}/{total_plies} plies exist). "
                    f"{new_cnt} new {move_word} ({sample_new}) will extend this line."
                )
        else:
            move_word = "move" if total_plies == 1 else "moves"
            if divergence_info and divergence_info.get("type") == "user_move_alternative":
                exist_san = divergence_info.get("existing_san") or divergence_info.get("existing_uci")
                new_san = divergence_info.get("new_san") or divergence_info.get("new_uci")
                message = (
                    f"At move 1, {new_san} replaces your existing prepared {exist_san} for {rep_cap}."
                )
            else:
                message = f"New line: all {total_plies} {move_word} are new to your {rep_cap} repertoire."

        return {
            "status": status,
            "repertoire": repertoire,
            "total_plies": total_plies,
            "matched_plies": matched_plies,
            "new_plies": len(new_moves),
            "matched_sans": matched_sans,
            "new_sans": new_sans,
            "repertoire_continues_deeper": repertoire_continues_deeper,
            "repertoire_deepest_ply": repertoire_deepest_ply,
            "divergence_info": divergence_info,
            "message": message
        }

    def check_lines_overlap(self, repertoire: str, lines: List[Dict]) -> List[Dict]:
        """Batch checks multiple lines against the repertoire tree.

        Audit SCOUT-11(4)/(5):
          * the whole batch used to re-read and re-index the entire move tree once **per line**;
            it now shares one index (see :meth:`_overlap_index`);
          * the results are returned in **depth-first order** - a line is reported after every
            line that shares its first moves, so a bulk import writes parents before children and
            a sibling can no longer be silently overwritten by a line that happens to come later
            in the payload;
          * every line additionally carries `batch_conflicts`: the other lines **in this batch**
            that prepare a *different* user move at the same position. A one-user-move-per-position
            repertoire cannot hold all of them, so a bulk import that ignores this field will
            destroy the siblings it just wrote (the "📥 All → White" data loss).
        """
        repertoire = (repertoire or "white").lower().strip()
        if repertoire not in ("white", "black"):
            repertoire = "white"

        moves_by_parent, moves_by_parent_3 = self._load_overlap_index(repertoire)

        # Which user move does each line prepare at each of its own positions?
        prepared_by_line: Dict[int, Dict[str, str]] = {}
        first_position: Dict[int, str] = {}
        depth_of: Dict[int, int] = {}
        for idx, line_item in enumerate(lines or []):
            moves = (line_item.get("moves") or []) if isinstance(line_item, dict) else []
            board = chess.Board()
            prepared: Dict[str, str] = {}
            for m in moves:
                fen_before = m.get("fen_before") or board.fen()
                uci = (m.get("uci") or "").strip()
                san = (m.get("san") or "").strip()
                try:
                    board = chess.Board(fen_before)
                except Exception:
                    pass
                if not uci and san:
                    try:
                        uci = board.parse_san(san).uci()
                    except Exception:
                        uci = ""
                if not uci:
                    continue
                is_user_ply = ((board.turn == chess.WHITE) == (repertoire == "white"))
                if is_user_ply:
                    prepared.setdefault(normalize_fen(fen_before), uci)
                try:
                    board.push(chess.Move.from_uci(uci))
                except Exception:
                    pass
            prepared_by_line[idx] = prepared
            first_position[idx] = normalize_fen(
                (moves[0].get("fen_before") if moves and isinstance(moves[0], dict) else None)
                or chess.STARTING_FEN
            )
            depth_of[idx] = len(moves)

        results: List[Tuple] = []
        for idx, line_item in enumerate(lines or []):
            line_id = line_item.get("id") or line_item.get("idx")
            moves = line_item.get("moves") or []
            res = self._evaluate_line_against_index(repertoire, moves, moves_by_parent, moves_by_parent_3)
            res["id"] = line_id
            mine = prepared_by_line.get(idx, {})
            siblings = []
            for other_idx, theirs in prepared_by_line.items():
                if other_idx == idx:
                    continue
                for parent, uci in theirs.items():
                    if parent in mine and mine[parent] != uci:
                        siblings.append({
                            "parent_fen": parent,
                            "line_index": other_idx,
                            "line_id": (lines[other_idx].get("id") or lines[other_idx].get("idx")
                                        if isinstance(lines[other_idx], dict) else other_idx),
                            "this_line_uci": mine[parent],
                            "other_line_uci": uci,
                        })
            res["batch_conflicts"] = siblings
            results.append((depth_of.get(idx, 0), first_position.get(idx, ""), idx, res))

        # DFS batch order (audit SCOUT-11(4)): the line that *starts* a variation (fewest plies,
        # same opening position) is checked and imported before the lines that continue it, so a
        # bulk import writes parents before children instead of letting the payload order decide
        # which branch survives.
        results.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in results]

    def get_repertoire_tree(self, repertoire: str = "white", gap_threshold: float = 5.0, min_games: int = 30, include_patched: bool = False) -> Dict:
        """Returns the full repertoire move tree, coverage %, unaddressed gaps, and game blunder gaps."""
        repertoire = (repertoire or "white").lower()
        if repertoire not in ("white", "black"):
            repertoire = "white"

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("""
        SELECT * FROM repertoire_moves
        WHERE repertoire = ?
        ORDER BY id ASC
        """, (repertoire,))
        moves = [dict(r) for r in cursor.fetchall()]
        conn.close()

        # Index moves by parent_fen
        tree_by_parent = {}
        for m in moves:
            p = m["parent_fen"]
            if p not in tree_by_parent:
                tree_by_parent[p] = []
            tree_by_parent[p].append(m)
            p3 = " ".join(p.split()[:3])
            if p3 != p:
                if p3 not in tree_by_parent:
                    tree_by_parent[p3] = []
                if m not in tree_by_parent[p3]:
                    tree_by_parent[p3].append(m)

        # Coverage and Gaps detection
        gaps = []
        is_user_white = (repertoire == "white")

        # Collect positions where it is the opponent's turn
        opp_positions = []
        if not is_user_white and moves:
            # For Black repertoire, White (opponent) plays the first move from the starting position!
            opp_positions.append((normalize_fen(chess.STARTING_FEN), "Starting Position", ""))

        for m in moves:
            try:
                board = chess.Board(m["fen"])
            except Exception:
                continue

            is_opponent_turn = (board.turn == chess.BLACK and is_user_white) or (board.turn == chess.WHITE and not is_user_white)
            if is_opponent_turn:
                opp_positions.append((m["fen"], m.get("opening_name") or "Repertoire Line", m.get("eco", "")))

        # CODE-4/CODE-5: fetch the 2000+ explorer **once per opponent position** and thread the
        # repertoire colour through, so a black repertoire is scored with the black player's personal
        # stats instead of the white player's (get_candidate_moves_2000 defaults to color="white"),
        # and the gap list + coverage metric share one (HTTP + up to 8 Stockfish analyses) lookup
        # instead of paying for it twice.
        #
        # SYNC-20 / ABUSE-17: this IS the real path into `get_candidate_moves_2000`, so an
        # `api.py` route guard protects nothing here. The payload is validated inside that
        # function, and this loop additionally:
        #   * survives an exception from one position (one bad FEN must not 500 the whole
        #     Repertoire Builder tab), and
        #   * records which positions could not be checked, so `coverage_pct` is reported as
        #     `None` (unknown) rather than as 100% ("nobody plays any of these above 2000").
        cand_cache: Dict[str, Dict] = {}
        unavailable_positions: List[str] = []
        total_opp_weight = 0.0
        covered_opp_weight = 0.0

        # AUDIT FIXER-V / TRAINER-09 (owner ruling): the coverage metric and the gap list are the
        # other place where an opponent branch with no reply used to be counted as a finished
        # line. `prepared_replies` was simply "is there a repertoire row for this opponent move",
        # so recording `1... c5` and answering nothing to it counted as 100% coverage of `c5`.
        #
        # The distinction used here is the same one the trainer uses:
        #   * ANSWERED  - the repertoire holds the opponent move *and* a prepared user move at the
        #                 position it produces. That is a line you can actually play: covered.
        #   * UNANSWERED - the repertoire holds the opponent move but nothing after it. The line
        #                 finishes on the opponent's move, so it is NOT covered; it is a gap the
        #                 Builder has to close. Reported as such, with a marker the UI can use.
        #
        # A 3-token key is used on both sides for the same reason the trainer and the progress
        # counter use one (AGENTS.md 4F + TRAINER-01): `normalize_fen` keeps the en-passant token,
        # so the two transposing spellings of one position must agree about the answer.
        user_move_parents3 = {
            " ".join((m.get("parent_fen") or "").split()[:3])
            for m in moves if m.get("is_user_move")
        }
        unanswered_branch_count = 0
        counted_positions = set()
        for pos_fen, op_name, eco in opp_positions:
            if pos_fen not in cand_cache:
                try:
                    cand_cache[pos_fen] = self.get_candidate_moves_2000(pos_fen, color=repertoire)
                except Exception as e:
                    # SYNC-20: never let one position's malformed/hostile data take out the
                    # endpoint. The position is treated as "could not be checked".
                    print(f"get_repertoire_tree: explorer lookup failed for {pos_fen!r}: {e}")
                    cand_cache[pos_fen] = {"moves": [], "degraded": True,
                                           "error": "explorer_unavailable", "total_games": None}
            cand = cand_cache[pos_fen]
            if cand.get("degraded") or cand.get("total_games") is None:
                unavailable_positions.append(pos_fen)
            cand_moves = cand.get("moves") or []

            pos_3 = " ".join((pos_fen or "").split()[:3])
            covered_replies, unanswered_replies = set(), set()
            for reply in tree_by_parent.get(pos_fen, []) or tree_by_parent.get(pos_3, []):
                if reply.get("is_user_move"):
                    continue
                uci = reply.get("move_uci") or ""
                if not uci or uci in covered_replies or uci in unanswered_replies:
                    continue
                child_3 = " ".join((reply.get("fen") or "").split()[:3])
                if child_3 and child_3 in user_move_parents3:
                    covered_replies.add(uci)
                else:
                    unanswered_replies.add(uci)
                    # `opp_positions` can name one position twice (a 4-token and a 3-token
                    # spelling of it), so the repertoire-side count must be de-duplicated.
                    key = (pos_fen, uci)
                    if key not in counted_positions:
                        counted_positions.add(key)
                        unanswered_branch_count += 1

            for opp_m in cand_moves:
                if not isinstance(opp_m, dict):
                    continue
                # SYNC-20 variant B: a move with no `san` used to be cached and then 500'd
                # this bare subscript on the *next* request.
                opp_san = opp_m.get("san") or ""
                if not opp_san:
                    continue
                pop_pct = float(opp_m.get("popularity_pct") or 0.0)
                cand_uci = opp_m.get("uci", "")
                # FIXER-V: only an ANSWERED branch counts as coverage (see the comment above).
                total_opp_weight += pop_pct
                if cand_uci in covered_replies:
                    covered_opp_weight += pop_pct

                if pop_pct >= gap_threshold and cand_uci not in covered_replies:
                    # FIXER-V: a branch the repertoire holds but cannot answer is a gap of the
                    # same kind as a branch it does not hold at all - it finishes on the
                    # opponent's move either way - so it is reported here, tagged so the Builder
                    # can say "you have this move, you have no answer" instead of "missing".
                    is_unanswered = cand_uci in unanswered_replies
                    gaps.append({
                        "parent_fen": pos_fen,
                        "opening_name": op_name,
                        "eco": eco,
                        "opponent_san": opp_san,
                        "opponent_uci": cand_uci,
                        "popularity_pct": pop_pct,
                        "win_rate": float(opp_m.get("win_rate") or 0.0),
                        "games": opp_m.get("games", 0),
                        "in_repertoire": is_unanswered,
                        "gap_kind": "unanswered_branch" if is_unanswered else "missing_branch",
                    })

        coverage_pct = round((covered_opp_weight / total_opp_weight * 100), 1) if total_opp_weight > 0 else 100.0

        # Include game blunder gaps
        game_gaps_data = self.get_game_gaps(repertoire, min_games=min_games, include_patched=include_patched)

        result = {
            "repertoire": repertoire,
            "total_moves": len(moves),
            # FIXER-V: how many of this repertoire's own opponent branches have no prepared user
            # reply. A pure repertoire-side count (no explorer involved), reported next to the
            # coverage figure so a dead-end branch is never silently read as a finished line.
            "unanswered_branches": unanswered_branch_count,
            "coverage_pct": min(100.0, max(0.0, coverage_pct)),
            "gap_threshold": gap_threshold,
            "opening_threshold": min_games,
            "gaps": sorted(gaps, key=lambda x: float(x.get("popularity_pct") or 0.0), reverse=True)[:20],
            "game_gaps": game_gaps_data,
            "moves_by_parent": tree_by_parent
        }
        if unavailable_positions:
            # ABUSE-17: an explorer outage used to be completely invisible, and
            # `coverage_pct: 100.0` ("these openings are not played by anybody above 2000") is
            # the most misleading thing this endpoint can say. `coverage_pct` keeps its numeric
            # contract - it is asserted and rendered as a percentage by
            # `tests/test_user_workflows.py` and `frontend/repertoire_builder.js:1120` - but it
            # is now computed over the positions that could actually be checked and the outage is
            # reported explicitly, with the affected positions named, so a consumer can render
            # "could not check" instead of a percentage. Frontend follow-up: one line at
            # `repertoire_builder.js:1120` to show an em dash when `coverage_degraded` is set.
            result["coverage_degraded"] = True
            result["coverage_positions_checked"] = len(opp_positions) - len(unavailable_positions)
            result["coverage_positions_total"] = len(opp_positions)
            result["unavailable_positions"] = unavailable_positions[:20]
            result["error"] = "explorer_unavailable"
        return result

    def get_game_gaps(
        self,
        color: str,
        min_games: int = 30,
        max_move: Optional[int] = None,
        include_patched: bool = False,
        max_explorer_lookups: int = 25
    ) -> Dict:
        """Returns opening blunders from user's actual games played in positions with >= min_games in 2000+ Elo Lichess database, excluding positions that were already completely losing, sorted by cpl_loss DESC.

        Audit CODE-6: this is a READ endpoint and it now performs **no writes at all**. It used to
        back-fill `blunders.opening_games` from a GET (one new connection + one commit per row, one
        live explorer HTTP call per FEN) and stored the `0` returned by a failed/timeout request,
        which permanently buried that blunder (`opening_games IS NULL` was never true again, so the
        back-fill could not be re-triggered). The explorer count for never-looked-up positions is now
        resolved **in memory only**, capped at `max_explorer_lookups` distinct FENs per request, and
        nothing is persisted, so a transient network failure is retried on the next page load instead
        of being frozen into the database.

        Audit ABUSE-17: a position the explorer could not be asked about is now reported as
        **unknown** and excluded from this response (it is retried on the next request), instead of
        being treated as a real `0` games. Audit SYNC-20: a stored `opening_games` that is a
        non-integer REAL, a `NaN`/`Infinity`, or a value SQLite cannot store as an INTEGER used to
        make `int(...)` raise `ValueError`/`OverflowError` here - a 500 on a GET, or worse, a
        persisted REAL that permanently defeats the `min_games` cut-off.
        """
        color = (color or "white").lower()
        if color not in ("white", "black"):
            color = "white"

        conn = get_db()
        cursor = conn.cursor()

        # In-memory (never persisted) resolution of `opening_games` for positions never looked up.
        cursor.execute("SELECT id, fen_before FROM blunders WHERE opening_games IS NULL")
        fen_cache: Dict[str, Optional[int]] = {}
        row_opening_games: Dict[int, int] = {}
        unresolved: List[str] = []
        for r in cursor.fetchall():
            f = r["fen_before"]
            if f not in fen_cache:
                if len(fen_cache) >= max(0, int(max_explorer_lookups or 0)):
                    break  # bounded: the rest stay unknown and are retried on the next request
                count, known = self.probe_explorer_game_count(f)
                fen_cache[f] = count if known else None
                if not known and f not in unresolved:
                    unresolved.append(f)
            if fen_cache[f] is None:
                continue  # unknown (outage / negative TTL / cap) - not a "0 games" verdict
            row_opening_games[r["id"]] = fen_cache[f]

        # Query blunders where eval_before >= -200 and opening_games >= min_games
        # (or all if min_games == 0, and optional max_move filter).
        # Rows with a still-unresolved opening_games are also selected and filtered below, because
        # their count only exists in memory.
        cursor.execute("""
        SELECT b.*, g.white_player, g.black_player, g.played_at, g.speed, g.moves_san
        FROM blunders b
        LEFT JOIN games g ON b.game_id = g.id
        WHERE b.color = ? AND b.eval_before >= -200
          AND (? = 0 OR b.opening_games >= ? OR b.opening_games IS NULL)
          AND (? IS NULL OR b.move_number <= ?)
        ORDER BY b.cpl_loss DESC
        """, (color, min_games, min_games, max_move, max_move))
        rows = [dict(r) for r in cursor.fetchall()]

        # Apply the min_games cut-off to the in-memory counts, without writing anything back.
        resolved_rows = []
        for r in rows:
            raw_count = r.get("opening_games")
            if raw_count is None:
                if r["id"] not in row_opening_games:
                    continue  # not resolved (lookup cap reached) - retried on the next request
                r["opening_games"] = row_opening_games[r["id"]]
            elif _number_is_unusable(raw_count):
                # SYNC-20: a persisted REAL / NaN / Infinity in an INTEGER column. Treat it as
                # "unknown" (re-probe in memory) instead of raising here.
                fen = r.get("fen_before")
                count, known = self.probe_explorer_game_count(fen) if fen else (0, False)
                if not known:
                    unresolved.append(fen) if fen else None
                    continue
                r["opening_games"] = count
            # SYNC-20: never `int(...)` an uncoerced column value.
            safe_count = coerce_count(r.get("opening_games"), 0)
            r["opening_games"] = safe_count
            if min_games and safe_count < min_games:
                continue
            resolved_rows.append(r)
        rows = resolved_rows

        # Query all blunders for these games to detect earlier mistakes and provide complete blunder context
        game_ids = list(set(r["game_id"] for r in rows if r.get("game_id")))
        all_blunders_by_game = defaultdict(list)
        if game_ids:
            placeholders = ",".join("?" for _ in game_ids)
            cursor.execute(f"""
            SELECT id, game_id, move_number, ply, color, played_san, played_move, best_san, best_move, eval_before, eval_after, cpl_loss, fen_before, opening_games
            FROM blunders
            WHERE game_id IN ({placeholders})
            ORDER BY move_number ASC
            """, game_ids)
            for b in cursor.fetchall():
                all_blunders_by_game[b["game_id"]].append(dict(b))

        # Query user repertoire moves to see if already patched
        cursor.execute("""
        SELECT parent_fen, move_uci FROM repertoire_moves
        WHERE repertoire = ? AND is_user_move = 1
        """, (color,))
        rep_moves = {r["parent_fen"]: r["move_uci"] for r in cursor.fetchall()}

        cursor.execute("SELECT COUNT(*) as cnt FROM games WHERE user_color = ?", (color,))
        total_games_color = cursor.fetchone()["cnt"]
        cursor.execute("SELECT COUNT(*) as cnt FROM games")
        total_games_all = cursor.fetchone()["cnt"]
        conn.close()

        gaps = []
        patched_gaps = []
        for r in rows:
            norm_fen = normalize_fen(r["fen_before"])
            is_patched = (rep_moves.get(norm_fen) == r["best_move"])
            opp_name = r["black_player"] if color == "white" else r["white_player"]

            g_bls = all_blunders_by_game.get(r["game_id"], [])
            # Find earlier mistake in this game occurring before this move
            earlier = [b for b in g_bls if b["ply"] < r["ply"]]
            earlier_mistake = earlier[0] if earlier else None

            gap_item = {
                "id": r["id"],
                "game_id": r["game_id"],
                "move_number": r["move_number"],
                "ply": r["ply"],
                "color": r["color"],
                "fen_before": r["fen_before"],
                "fen_after": r["fen_after"],
                "played_san": r["played_san"],
                "played_uci": r["played_move"],
                "best_san": r["best_san"],
                "best_uci": r["best_move"],
                "cpl_loss": r["cpl_loss"],
                "eval_before": r["eval_before"],
                "eval_after": r["eval_after"],
                "opening_name": r["opening_name"] or "Opening Line",
                "eco": r["eco"] or "",
                "opening_games": r.get("opening_games") or 0,
                "opponent_name": opp_name,
                "played_at": r.get("played_at", 0),
                "is_patched": is_patched,
                "speed": r.get("speed", "rapid"),
                "game_moves": r.get("moves_san", "") or "",
                "all_blunders": g_bls,
                "earlier_mistake": earlier_mistake,
                "game_url": f"https://lichess.org/{r['game_id']}" if r["game_id"] else ""
            }

            if is_patched:
                patched_gaps.append(gap_item)
                if include_patched:
                    gaps.append(gap_item)
            else:
                gaps.append(gap_item)

        result = {
            "color": color,
            "min_games": min_games,
            "total_games_analyzed": total_games_all,
            "total_games_color": total_games_color,
            "total_gaps": len(rows),
            "unpatched_gaps": len([r for r in rows if rep_moves.get(normalize_fen(r["fen_before"])) != r["best_move"]]),
            "unpatched_count": len([r for r in rows if rep_moves.get(normalize_fen(r["fen_before"])) != r["best_move"]]),
            "patched_count": len(patched_gaps),
            "gaps": gaps,
            "patched_gaps": patched_gaps
        }
        if unresolved:
            # ABUSE-17: name the positions the 2000+ database could not be asked about, so the
            # caller can say "unknown" instead of silently dropping the blunders.
            result["unresolved_positions"] = len(unresolved)
            result["opening_games_degraded"] = True
            result["error"] = "explorer_unavailable"
        return result

    def get_total_games_count(self) -> int:
        """Returns total number of user games stored in the database."""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM games")
        count = cursor.fetchone()[0]
        conn.close()
        return count

    # -- game ingestion (audit SYNC-16 / SYNC-17 / SYNC-21 / SYNC-22) ------------------ #

    @staticmethod
    def _game_row_from_upstream(game: dict, username: str) -> Optional[Dict[str, Any]]:
        """Validate + normalise one upstream game record into `games` column values.

        Returns `None` for a record that must not be written (no id, an unrecognised `winner`
        token). Audit SYNC-17 variant C / SYNC-21 / SYNC-22: the four writers used to bind
        whatever the JSON document happened to contain, so `"winner": {"a": 1}` raised
        `sqlite3.ProgrammingError` *inside the bind* (500 plus a leaked write transaction that
        blocked every other writer for the full 30 s timeout), `"createdAt": Infinity` was
        persisted as a REAL that made `GET /api/games/recent` answer 400 for every page forever,
        and an abandoned game was stored exactly like a drawn one.

        Every field is coerced to the column's type here, so the `INSERT` can no longer fail on a
        bind, and the caller decides what to do with a rejected record.
        """
        if not isinstance(game, dict):
            return None
        game_id = coerce_text(game.get("id"), "", 64).strip()
        if not game_id:
            return None

        white_user, white_rating = extract_player_info(game, "white")
        black_user, black_rating = extract_player_info(game, "black")
        user_color = "white" if white_user.lower() == (username or "").lower() else "black"

        opening_data = game.get("opening")
        if not isinstance(opening_data, dict):
            opening_data = {}

        result_token = normalize_game_outcome(game.get("winner"), game.get("status"), user_color)
        if result_token is None:
            return None  # unknown `winner` token -> rejected, never stored verbatim

        return {
            "id": game_id,
            "speed": coerce_text(game.get("speed"), "", 24),
            "rated": 1 if game.get("rated") else 0,
            # SYNC-21: total and clamped. `Infinity`, `1e300`, a list and `"soon"` all become
            # the "unknown date" sentinel instead of a poisoned INTEGER column.
            "played_at": normalize_played_at(game.get("createdAt"), default=int(time.time() * 1000)),
            "white_player": coerce_text(white_user, "", 64),
            "black_player": coerce_text(black_user, "", 64),
            "white_rating": white_rating,
            "black_rating": black_rating,
            "user_color": user_color,
            "result": result_token,
            "opening_name": coerce_text(opening_data.get("name"), "", 256),
            "eco": coerce_text(opening_data.get("eco"), "", 16),
            "moves_san": coerce_text(game.get("moves"), "", 8192),
        }

    @staticmethod
    def _upsert_game(cursor, row: Dict[str, Any]) -> None:
        """`INSERT ... ON CONFLICT(id) DO UPDATE`, never touching `analyzed_at`.

        Audit SYNC-16: `INSERT OR REPLACE` is a DELETE + INSERT in SQLite, so every re-sync
        destroyed and recreated the game row (new rowid, every unlisted column reset) and would
        orphan its `blunders` rows the moment `PRAGMA foreign_keys` is ever turned on. `analyzed_at`
        is the "work is done" flag and is owned exclusively by the analysis path, so the upsert
        deliberately does not list it in its SET clause.
        """
        cursor.execute("""
        INSERT INTO games (
            id, speed, rated, played_at, white_player, black_player,
            white_rating, black_rating, user_color, result, opening_name, eco, moves_san, analyzed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(id) DO UPDATE SET
            speed = excluded.speed,
            rated = excluded.rated,
            played_at = excluded.played_at,
            white_player = excluded.white_player,
            black_player = excluded.black_player,
            white_rating = excluded.white_rating,
            black_rating = excluded.black_rating,
            user_color = excluded.user_color,
            result = excluded.result,
            opening_name = excluded.opening_name,
            eco = excluded.eco,
            moves_san = excluded.moves_san
        """, (
            row["id"], row["speed"], row["rated"], row["played_at"],
            row["white_player"], row["black_player"], row["white_rating"], row["black_rating"],
            row["user_color"], row["result"], row["opening_name"], row["eco"], row["moves_san"],
        ))

    @staticmethod
    def repair_poisoned_played_at() -> int:
        """Rewrite `games.played_at` rows that are not a storable millisecond integer.

        Audit SYNC-21. `backend/api.py::migrate_played_at_units()` can only *scale* a value that
        is a positive integer below `MS_EPOCH_THRESHOLD`; a row holding `Infinity` (a REAL), the
        TEXT `'soon'` or a negative value matches no branch of its `WHERE` clause, so it survives
        every reboot, cache clear and re-sync, and `normalize_played_at`'s `int(float(value))`
        keeps raising `OverflowError` for the whole Recent Games tab. That migration lives in
        `api.py` (not this file), so the repertoire-owned writers call this additive, idempotent
        repair after every batch. Unusable values become `0`, which every consumer already renders
        as "unknown date" - honest, reversible by a later sync, and never an exception.
        """
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE games SET played_at = 0 "
                "WHERE played_at IS NOT NULL AND ("
                "  typeof(played_at) != 'integer'"
                "  OR played_at < 0"
                "  OR played_at > ?"
                ")",
                (MAX_PLAYED_AT_MS,),
            )
            changed = cursor.rowcount or 0
            if changed:
                conn.commit()
            return changed
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"repair_poisoned_played_at failed (non-fatal): {e}")
            return 0
        finally:
            conn.close()

    def _fetch_user_games(self, username: str, max_games: Optional[int] = None, perf_type: Optional[str] = None) -> List[Dict]:
        custom_perf = perf_type or get_setting("sync_perf_types")
        if custom_perf:
            try:
                return self.lichess_client.fetch_user_games(username, max_games=max_games, perf_type=custom_perf)
            except TypeError:
                pass
        try:
            return self.lichess_client.fetch_user_games(username, max_games=max_games)
        except TypeError:
            return self.lichess_client.fetch_user_games(username)

    def sync_recent_games(self, max_games: int = 20, perf_type: Optional[str] = None) -> Dict:
        """Quickly fetches recent games from Lichess and saves them to the DB without slow Stockfish blunder analysis.

        Audit SYNC-17 / SYNC-21 / SYNC-22. This writer used to have no `try`/`finally` at all:
        sqlite3 opens its implicit write transaction at the first `INSERT`, so a malformed record
        (`"speed": {"k": 1}`) raised `ProgrammingError` *inside the bind*, the frame kept the
        connection alive, and every other writer in the app was then blocked by SQLite's single
        write lock for the full `timeout=30.0` (measured). The batch is now committed, rolled back
        on error, and the connection is always closed; each record is validated by
        `_game_row_from_upstream` so a bind can no longer fail; and a rejected record is reported
        instead of aborting the batch.
        """
        username = get_setting("lichess_username") or "RobiDeli"
        games = self._fetch_user_games(username, max_games=max_games, perf_type=perf_type)
        if not games:
            return {
                "status": "no_games",
                "synced_games": 0,
                "total_games": self.get_total_games_count()
            }

        conn = get_db()
        cursor = conn.cursor()
        synced = 0
        rejected: List[Dict[str, Any]] = []

        try:
            for g in games:
                row = self._game_row_from_upstream(g, username)
                if row is None:
                    rejected.append({
                        "game_id": str(coerce_text((g or {}).get("id"), "", 64)),
                        "error": "unrecognised or missing game record (rejected, nothing written)",
                    })
                    continue
                game_id = row["id"]

                opening_data = g.get("opening") if isinstance(g, dict) else None
                op_name = row["opening_name"]
                eco = row["eco"]
                moves_san = row["moves_san"]

                if (not op_name or not eco) and moves_san:
                    ident = self.opening_book.identify_game_opening(moves_san.split())
                    if not op_name:
                        op_name = coerce_text(ident.get("name"), "", 256)
                    if not eco:
                        eco = coerce_text(ident.get("eco"), "", 16)
                    row["opening_name"] = op_name
                    row["eco"] = eco

                self._upsert_game(cursor, row)
                synced += 1

            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"sync_recent_games failed, batch rolled back: {e}")
            return {
                "status": "error",
                "message": f"Could not save the synced games: {e}",
                "synced_games": 0,
                "rejected_games": rejected,
                "total_games": self.get_total_games_count()
            }
        finally:
            conn.close()

        repaired = self.repair_poisoned_played_at()

        self._user_stats_cache = {}
        self._user_stats_time = 0

        return {
            "status": "partial" if rejected else "success",
            "synced_games": synced,
            "rejected_games": rejected,
            "repaired_played_at": repaired,
            "total_games": self.get_total_games_count()
        }

    async def stream_sync_recent_games(
        self,
        max_games: int = 30,
        request: Optional[Any] = None,
        perf_type: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """Progressively yields SSE events while fetching and storing recent games with real-time progress."""
        username = get_setting("lichess_username") or "RobiDeli"
        effective_perf = perf_type or get_setting("sync_perf_types") or DEFAULT_PERF_TYPES

        # Step 1: Connecting
        yield f"data: {json.dumps({'stage': 'connecting', 'message': 'Connecting to Lichess API...', 'percent': 10, 'current': 0, 'total': 0, 'synced_games': 0})}\n\n"
        await asyncio.sleep(0.04)

        # Step 2: Fetching
        yield f"data: {json.dumps({'stage': 'fetching', 'message': f'Downloading latest games for {username}...', 'percent': 25, 'current': 0, 'total': 0, 'synced_games': 0})}\n\n"
        await asyncio.sleep(0.04)

        try:
            games = await asyncio.to_thread(self._fetch_user_games, username, max_games, perf_type)
        except Exception as e:
            yield f"data: {json.dumps({'stage': 'error', 'message': f'Failed to fetch games from Lichess: {str(e)}', 'percent': 0, 'current': 0, 'total': 0, 'synced_games': 0})}\n\n"
            return

        if not games:
            yield f"data: {json.dumps({'stage': 'completed', 'message': 'No recent games found on Lichess or rate limit reached.', 'percent': 100, 'current': 0, 'total': 0, 'synced_games': 0})}\n\n"
            return

        total = len(games)
        yield f"data: {json.dumps({'stage': 'processing_start', 'message': f'Downloaded {total} games. Processing openings & saving to database...', 'percent': 40, 'current': 0, 'total': total, 'synced_games': 0})}\n\n"
        await asyncio.sleep(0.02)

        conn = get_db()
        cursor = conn.cursor()
        synced = 0
        rejected = 0

        try:
            for idx, g in enumerate(games, 1):
                if request and await request.is_disconnected():
                    break

                row = self._game_row_from_upstream(g, username)
                if row is None:
                    rejected += 1
                    continue
                game_id = row["id"]
                opponent = (row["black_player"] if row["user_color"] == "white"
                            else row["white_player"])
                op_name = row["opening_name"]
                eco = row["eco"]
                moves_san = row["moves_san"]

                if (not op_name or not eco) and moves_san:
                    ident = self.opening_book.identify_game_opening(moves_san.split())
                    if not op_name:
                        op_name = coerce_text(ident.get("name"), "", 256)
                    if not eco:
                        eco = coerce_text(ident.get("eco"), "", 16)
                    row["opening_name"] = op_name
                    row["eco"] = eco

                self._upsert_game(cursor, row)
                synced += 1

                game_title = op_name if op_name else "Opening"
                pct = 40 + int((idx / total) * 55)
                payload = {
                    "stage": "processing",
                    "message": f"Game {idx}/{total}: {game_title} vs {opponent}",
                    "current": idx,
                    "total": total,
                    "percent": pct,
                    "game_id": game_id,
                    "opening": op_name,
                    "eco": eco,
                    "opponent": opponent,
                    "synced_games": synced
                }
                yield f"data: {json.dumps(payload)}\n\n"
                await asyncio.sleep(0.015)

            conn.commit()
        except Exception as e:
            # SYNC-22: the implicit write transaction is rolled back explicitly and the
            # connection is closed in the `finally`, so a mid-batch failure cannot hold
            # SQLite's single write lock for the full 30 s timeout.
            try:
                conn.rollback()
            except Exception:
                pass
            yield f"data: {json.dumps({'stage': 'error', 'message': f'Error saving games: {str(e)}', 'percent': 0, 'current': 0, 'total': total, 'synced_games': synced})}\n\n"
            return
        finally:
            conn.close()

        repaired = self.repair_poisoned_played_at()

        self._user_stats_cache = {}
        self._user_stats_time = 0

        yield f"data: {json.dumps({'stage': 'completed', 'message': f'Successfully synced {synced} games from Lichess!', 'percent': 100, 'current': total, 'total': total, 'synced_games': synced, 'rejected_games': rejected, 'repaired_played_at': repaired})}\n\n"

    def _position_is_reachable(self, repertoire: str, fen: str) -> bool:
        """Whether `fen` is the starting position or the result of a move in this repertoire.

        Audit SCOUT-11(5): nothing checked this, so a crafted/garbled import could graft an
        arbitrary mid-game position into the tree as a floating branch, which then becomes
        collateral for the 3-token prune. An import must only extend a position the repertoire
        can actually reach.
        """
        norm = normalize_fen(fen)
        if not norm:
            return False
        if norm == normalize_fen(chess.STARTING_FEN):
            return True
        conn = get_db()
        cursor = conn.cursor()
        try:
            row = cursor.execute(
                "SELECT 1 FROM repertoire_moves WHERE repertoire = ? AND fen = ? LIMIT 1",
                (repertoire, norm),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def import_game_opening(
        self,
        game_id: str,
        repertoire: str,
        max_ply: Optional[int] = None,
        replace_existing: bool = False
    ) -> Dict:
        """Replays a stored game's opening into a repertoire.

        Audit BUILDER-03 / SYNC-12 (the "📥 Add Opening" button):

        * **The side the user played is honoured.** The old code decided "is this the user's
          move?" from the *requested* repertoire colour and the ply parity, and never looked at
          `games.user_color` - so importing a game the user played as **Black** into the White
          repertoire installed the *opponent's* moves as the user's prepared White moves, and
          (because the import went through the one-user-move-per-position rule) destroyed the
          real prepared line. A colour that disagrees with the recorded `user_color` is now
          refused with a `ValueError`, which the API turns into a 4xx, and nothing is written.
        * **It never overwrites.** Every ply is added with `replace_existing=False`, so a ply that
          diverges from a prepared user move is refused instead of deleting that move and its
          subtree. The walk then **stops** at the conflict (the remaining plies would hang off a
          position the repertoire does not have) and reports what conflicted, so the caller can
          ask for consent and re-send with `replace_existing=True`.
        * **It never grafts onto an unreachable position** (audit SCOUT-11(5)) - see
          :meth:`_position_is_reachable`.
        """
        repertoire = (repertoire or "white").lower()
        if repertoire not in ("white", "black"):
            raise ValueError("Repertoire must be 'white' or 'black'")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, moves_san, opening_name, eco, user_color FROM games WHERE id = ?", (game_id,))
        game = cursor.fetchone()
        conn.close()

        if not game:
            raise ValueError(f"Game '{game_id}' not found.")

        # BUILDER-03: the import is driven by "is this the user's move?", so the side the user
        # actually played decides it - not the colour the caller asked for.
        user_color = (game["user_color"] or "").strip().lower()
        if user_color not in ("white", "black"):
            raise ValueError(
                f"Game '{game_id}' has no recorded user_color, so its moves cannot be attributed "
                f"to the {repertoire} repertoire. Nothing was imported."
            )
        if user_color != repertoire:
            raise ValueError(
                f"Game '{game_id}' was played as {user_color}, so it cannot be imported into the "
                f"{repertoire} repertoire (that would file the opponent's moves as your prepared "
                f"moves). Nothing was imported."
            )

        moves_str = game["moves_san"] or ""
        san_tokens = [m for m in moves_str.strip().split() if m and not m.endswith(".") and not m[0].isdigit()]
        if not san_tokens:
            return {"status": "error", "message": "No moves found in game.", "imported_moves": 0,
                    "repertoire": repertoire, "game_id": game_id, "conflicts": []}

        identified = self.opening_book.identify_game_opening(san_tokens)
        op_name = game["opening_name"] or identified.get("name") or "Game Opening"
        eco = game["eco"] or identified.get("eco") or ""
        book_ply = identified.get("ply", 0)

        if max_ply is not None and max_ply > 0:
            target_ply = min(max_ply, len(san_tokens))
        elif book_ply > 0:
            target_ply = min(max(book_ply, 6), len(san_tokens))
        else:
            target_ply = min(16, len(san_tokens))

        board = chess.Board()
        imported = 0
        conflicts: List[Dict[str, Any]] = []
        skipped_unreachable: List[int] = []
        abandoned_at_ply: Optional[int] = None
        stopped_at_ply: Optional[int] = None
        stop_reason = ""
        errored: List[str] = []

        for ply in range(target_ply):
            san = san_tokens[ply]
            try:
                move = board.parse_san(san)
            except Exception as exc:
                errored.append(f"ply {ply + 1} ({san}): {exc}")
                break

            p_fen = board.fen()
            uci = move.uci()
            # The user played `user_color`, which we have just proven is `repertoire`.
            is_user_turn = (board.turn == chess.WHITE) == (repertoire == "white")

            if not self._position_is_reachable(repertoire, p_fen):
                # Nothing in the tree leads here and we are not at the start of the game, so the
                # import would create a floating branch. Report and stop.
                skipped_unreachable.append(ply + 1)
                stopped_at_ply = ply + 1
                stop_reason = ("the position after ply %d is not reachable in the %s repertoire, "
                               "so importing it would graft a floating branch onto the tree"
                               % (ply, repertoire))
                break

            res = self.add_repertoire_move(
                repertoire=repertoire,
                parent_fen=p_fen,
                move_uci=uci,
                is_user_move=is_user_turn,
                replace_existing=replace_existing
            )
            if res.get("status") == "success":
                imported += 1
            elif res.get("status") == "conflict":
                conflicts.append({
                    "ply": ply + 1,
                    "san": san,
                    "parent_fen": normalize_fen(p_fen),
                    "attempted_uci": res.get("attempted_uci"),
                    "existing_uci": res.get("existing_uci"),
                    "existing_san": res.get("existing_san"),
                    "message": res.get("message"),
                })
                # The rest of this line hangs off a position the repertoire does not have, so the
                # import stops here. Nothing below was written.
                abandoned_at_ply = ply + 1
                stopped_at_ply = ply + 1
                stop_reason = "a prepared move had to be replaced"
                break
            else:
                errored.append(f"ply {ply + 1} ({san}): {res.get('message', 'unknown error')}")
                break

            board.push(move)

        if abandoned_at_ply is not None:
            status = "conflict"
            message = (
                f"Imported {imported} ply/plies of {op_name} into the {repertoire} repertoire, then "
                f"stopped at ply {abandoned_at_ply}: {conflicts[0]['message']} Nothing was replaced - "
                f"re-send with replace_existing=true to make that replacement."
            )
        elif stopped_at_ply is not None:
            status = "incomplete"
            message = (f"Imported {imported} ply/plies of {op_name} into the {repertoire} repertoire, "
                       f"then stopped at ply {stopped_at_ply}: {stop_reason}.")
        elif imported:
            status = "success"
            message = f"Imported {imported} move(s) from {op_name} into the {repertoire} repertoire."
        else:
            status = "error"
            message = "No moves were imported: " + ("; ".join(errored[:3]) if errored else "nothing to import")

        return {
            "status": status,
            "message": message,
            "imported_moves": imported,
            "target_ply": target_ply,
            "opening_name": op_name,
            "eco": eco,
            "repertoire": repertoire,
            "game_id": game_id,
            "user_color": user_color,
            "conflicts": conflicts,
            "abandoned_at_ply": abandoned_at_ply,
            "stopped_at_ply": stopped_at_ply,
            "skipped_unreachable": skipped_unreachable,
            "errors": errored[:3],
        }

    def analyze_user_games(self, max_games: Optional[int] = None, max_ply: int = 30, perf_type: Optional[str] = None) -> Dict:
        """Fetches and analyzes opening moves (up to move 15) for user's games. If max_games is None or 0, fetches all games.

        Audit SYNC-16 / SYNC-17 / SYNC-21 / SYNC-22. This writer was the worst of the four:

        * it **stamped and committed `analyzed_at` BEFORE analysing**, so any engine/pool/DB
          failure permanently marked the game "already analysed" with zero blunders stored and
          every later sync skipped it;
        * the loop had **no per-game `try`/`except`**, so one bad game answered a bare 500 for the
          whole library (the other 49 analyses were lost);
        * `analyzer.close()` and `conn.close()` were *after* the loop, so a failure leaked the
          pooled Stockfish handle for the lifetime of the request and leaked the connection with
          a live write transaction (SYNC-22: every other writer blocked for 30 s);
        * it used `INSERT OR REPLACE`, which is DELETE + INSERT in SQLite.

        It now mirrors the contract `POST /api/sync` already uses (FIXER-B): save the game as
        PENDING, analyse, and only then stamp `analyzed_at`; isolate each game; roll back and
        close in a `finally`; report the failures.
        """
        from backend.analyzer import GameAnalyzer
        username = get_setting("lichess_username") or "RobiDeli"
        games = self._fetch_user_games(username, max_games=max_games, perf_type=perf_type)
        if not games:
            return {
                "status": "no_games",
                "synced_games": 0,
                "new_blunders": 0,
                "total_games": self.get_total_games_count()
            }

        analyzer = GameAnalyzer()
        conn = get_db()
        cursor = conn.cursor()

        new_blunders_count = 0
        synced_count = 0
        already_analyzed_count = 0
        failed_games: List[Dict[str, str]] = []
        errors: List[str] = []

        try:
            for g in games:
                row = self._game_row_from_upstream(g, username)
                if row is None:
                    failed_games.append({
                        "game_id": str(coerce_text((g or {}).get("id"), "", 64)),
                        "error": "unrecognised or missing game record (rejected, nothing written)",
                    })
                    continue
                game_id = row["id"]
                # Incremental optimization: skip games already analyzed
                cursor.execute("SELECT id, analyzed_at FROM games WHERE id = ?", (game_id,))
                existing = cursor.fetchone()

                if existing and existing["analyzed_at"] and existing["analyzed_at"] > 0:
                    already_analyzed_count += 1
                    synced_count += 1
                    continue

                user_color = row["user_color"]
                g["user_color"] = user_color

                # Per-game isolation: a malformed record, a dead engine or a DB error must not
                # skip the rest of the library. The game is left PENDING (analyzed_at = 0), so
                # the next sync retries it.
                try:
                    self._upsert_game(cursor, row)
                    conn.commit()

                    blunders = analyzer.analyze_game_openings(g)
                    if blunders:
                        # Precompute opening_games for blunders so they rank and filter properly.
                        # ABUSE-17 / SYNC-20: `known=False` must be stored as NULL ("we could not
                        # check"), never as a fabricated 0 that would bury the blunder.
                        for b in blunders:
                            try:
                                count, known = self.probe_explorer_game_count(b["fen_before"])
                                b["opening_games"] = count if known else None
                            except Exception:
                                b["opening_games"] = None
                        analyzer.save_blunders_to_db(blunders)
                        new_blunders_count += len(blunders)

                    # Analysis finished cleanly -> only NOW mark the game as done.
                    cursor.execute("UPDATE games SET analyzed_at = ? WHERE id = ?",
                                   (int(time.time()), game_id))
                    conn.commit()
                    synced_count += 1
                except Exception as ex:
                    failed_games.append({"game_id": game_id, "error": "Analysis failed for this game."})
                    errors.append(f"{game_id}: {ex}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        finally:
            # SYNC-16: the pooled Stockfish handle is ALWAYS returned, even on an exception.
            try:
                analyzer.close()
            except Exception as e:
                errors.append(f"analyzer.close: {e}")
            # SYNC-22: the connection is ALWAYS closed, so no write lock survives this call.
            try:
                conn.close()
            except Exception:
                pass

        repaired = self.repair_poisoned_played_at()

        # Invalidate in-memory caches so new game data takes effect immediately
        self._user_stats_cache = {}
        self._user_stats_time = 0

        if synced_count == 0 and failed_games:
            return {
                "status": "error",
                "message": (f"Analysis failed for all {len(failed_games)} game(s). The games stay "
                            "pending and will be retried."),
                "synced_games": 0,
                "new_blunders": new_blunders_count,
                "already_analyzed": already_analyzed_count,
                "failed_games": failed_games,
                "rejected_games": [],
                "repaired_played_at": repaired,
                "errors": errors[:3],
                "total_games": self.get_total_games_count()
            }

        return {
            "status": "partial" if failed_games else "success",
            "synced_games": synced_count,
            "new_blunders": new_blunders_count,
            "already_analyzed": already_analyzed_count,
            "failed_games": failed_games,
            "rejected_games": [],
            "repaired_played_at": repaired,
            "errors": errors[:3],
            "total_games": self.get_total_games_count()
        }

    async def stream_analyze_user_games(
        self,
        max_games: Optional[int] = None,
        max_ply: int = 30,
        request: Optional[Any] = None,
        perf_type: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """Progressively yields SSE events during fetching and analyzing user's games.

        Audit SYNC-16 / SYNC-17 / SYNC-21 / SYNC-22 - the SSE twin of `analyze_user_games`, with
        the same contract: pending write -> analyse -> stamp `analyzed_at` on success only, one
        failing game never aborts the stream, and the pooled engine handle and the connection are
        released in a `finally` that also covers a failure *before* the loop (the previous
        `finally` only ran if the `try` was entered, and `get_db()` was unprotected).
        """
        from backend.analyzer import GameAnalyzer
        username = get_setting("lichess_username") or "RobiDeli"
        effective_perf = perf_type or get_setting("sync_perf_types") or DEFAULT_PERF_TYPES

        # Stage 1: Connecting & Fetching
        yield f"data: {json.dumps({'stage': 'fetching', 'message': 'Connecting to Lichess and downloading games...', 'current': 0, 'total': 0, 'percent': 0, 'new_blunders': 0, 'already_analyzed': 0})}\n\n"
        await asyncio.sleep(0.01)

        try:
            games = await asyncio.to_thread(self._fetch_user_games, username, max_games, perf_type)
        except Exception as e:
            yield f"data: {json.dumps({'stage': 'error', 'message': f'Failed to fetch games from Lichess: {str(e)}', 'current': 0, 'total': 0, 'percent': 0, 'new_blunders': 0, 'already_analyzed': 0})}\n\n"
            return

        if not games:
            yield f"data: {json.dumps({'stage': 'no_games', 'message': 'No games retrieved from Lichess or rate limit reached.', 'current': 0, 'total': 0, 'percent': 100, 'new_blunders': 0, 'already_analyzed': 0})}\n\n"
            return

        total_games = len(games)
        yield f"data: {json.dumps({'stage': 'analyzing_start', 'message': f'Downloaded {total_games} games. Starting engine analysis...', 'current': 0, 'total': total_games, 'percent': 0, 'new_blunders': 0, 'already_analyzed': 0})}\n\n"
        await asyncio.sleep(0.01)

        analyzer = None
        conn = None
        new_blunders_count = 0
        synced_count = 0
        already_analyzed_count = 0
        failed_games: List[Dict[str, str]] = []
        rejected_games: List[Dict[str, str]] = []

        try:
            analyzer = GameAnalyzer()
            conn = get_db()
            cursor = conn.cursor()

            for idx, g in enumerate(games, 1):
                if request and await request.is_disconnected():
                    break

                row = self._game_row_from_upstream(g, username)
                if row is None:
                    rejected_games.append({
                        "game_id": str(coerce_text((g or {}).get("id"), "", 64)),
                        "error": "unrecognised or missing game record (rejected, nothing written)",
                    })
                    continue
                game_id = row["id"]
                cursor.execute("SELECT id, analyzed_at FROM games WHERE id = ?", (game_id,))
                existing = cursor.fetchone()

                user_color = row["user_color"]
                g["user_color"] = user_color
                opening_name = row["opening_name"] or "Standard Opening"
                eco = row["eco"]

                pct = int(round((idx / total_games) * 100))

                if existing and existing["analyzed_at"] and existing["analyzed_at"] > 0:
                    already_analyzed_count += 1
                    synced_count += 1
                    yield f"data: {json.dumps({'stage': 'analyzing', 'current': idx, 'total': total_games, 'percent': pct, 'game_id': game_id, 'opening': opening_name, 'eco': eco, 'cached': True, 'game_blunders': 0, 'new_blunders': new_blunders_count, 'already_analyzed': already_analyzed_count, 'message': f'Game {idx}/{total_games}: {opening_name} (Cached)'})}\n\n"
                    await asyncio.sleep(0.005)
                    continue

                game_blunders_count = 0
                game_failed = False
                try:
                    self._upsert_game(cursor, row)
                    conn.commit()

                    blunders = await asyncio.to_thread(analyzer.analyze_game_openings, g)
                    if blunders:
                        for b in blunders:
                            try:
                                count, known = self.probe_explorer_game_count(b["fen_before"])
                                b["opening_games"] = count if known else None
                            except Exception:
                                b["opening_games"] = None
                        analyzer.save_blunders_to_db(blunders)
                        game_blunders_count = len(blunders)
                        new_blunders_count += game_blunders_count

                    # Analysis finished cleanly -> only NOW mark the game as done.
                    cursor.execute("UPDATE games SET analyzed_at = ? WHERE id = ?",
                                   (int(time.time()), game_id))
                    conn.commit()
                except Exception as ex:
                    # The game stays PENDING and is retried by the next sync.
                    failed_games.append({"game_id": game_id, "error": "Analysis failed for this game."})
                    print(f"Error analyzing game {game_id}: {ex}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    game_blunders_count = 0
                    game_failed = True

                synced_count += 1
                yield f"data: {json.dumps({'stage': 'analyzing', 'current': idx, 'total': total_games, 'percent': pct, 'game_id': game_id, 'opening': opening_name, 'eco': eco, 'cached': False, 'game_blunders': game_blunders_count, 'new_blunders': new_blunders_count, 'already_analyzed': already_analyzed_count, 'failed': game_failed, 'message': f'Game {idx}/{total_games}: {opening_name}'})}\n\n"
                await asyncio.sleep(0.005)

        except Exception as e:
            # Never leave a live write transaction behind (SYNC-22).
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            print(f"stream_analyze_user_games aborted: {e}")
            yield f"data: {json.dumps({'stage': 'error', 'message': f'Analysis stopped: {str(e)}', 'current': 0, 'total': total_games, 'percent': 0, 'new_blunders': new_blunders_count, 'already_analyzed': already_analyzed_count})}\n\n"
            return
        finally:
            if analyzer is not None:
                try:
                    analyzer.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._user_stats_cache = {}
            self._user_stats_time = 0

        repaired = self.repair_poisoned_played_at()

        yield f"data: {json.dumps({'stage': 'completed', 'message': f'Analysis complete! {synced_count} games processed, {new_blunders_count} new blunders identified.', 'synced_games': synced_count, 'new_blunders': new_blunders_count, 'already_analyzed': already_analyzed_count, 'failed_games': failed_games, 'rejected_games': rejected_games, 'repaired_played_at': repaired, 'total_games': self.get_total_games_count(), 'percent': 100})}\n\n"

    def seed_default_repertoires(self, force: bool = False) -> Dict[str, Any]:
        """Seeds the standard Italian Game / Sicilian Dragon / King's Indian example lines.

        Safety contract (audit CODE-2, AGENTS.md 9.3 + 9.4):
          * The "is the repertoire empty?" guard runs inside an explicit ``BEGIN IMMEDIATE``
            transaction (and under a process-wide lock), so it can no longer be defeated by a
            concurrent writer slipping in between the COUNT and the seed.
          * Seeding NEVER replaces or removes a move the user has prepared. A seed line is abandoned
            at the first position where the user prepared a *different* move, and a user move that
            is already prepared is left completely untouched (its notes, arrows, importance,
            study membership and spaced-repetition state are never rewritten).
          * Every return path yields a result dict, so ``POST /api/repertoire/seed`` can no longer
            answer ``200 null``.
        """
        with self._seed_lock:
            conn = get_db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                count = conn.execute("SELECT COUNT(*) FROM repertoire_moves").fetchone()[0]
                conn.commit()
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return {"status": "error", "message": f"Could not inspect the repertoire: {exc}"}
            finally:
                conn.close()

            if count > 0 and not force:
                return {
                    "status": "skipped",
                    "message": "Repertoire already contains moves. Seeding skipped to protect user data.",
                    "existing_moves": count
                }

            # 1. White Repertoire: Italian Game, Open Sicilian, French Defense
            white_lines = [
                # Main Giuoco Pianissimo Line
                ("Italian Game", ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "c2c3", "g8f6", "d2d3", "d7d6", "e1g1", "e8g8", "b1d2", "a7a6", "f1e1", "c8e6", "c4b3", "f8e8", "d2f1"]),
                # Against 3... Nf6 (Two Knights Defense)
                ("Italian Game", ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "d2d3", "f8c5", "c2c3", "d7d6", "e1g1", "e8g8"]),
                # Against 1... c5 (Sicilian) -> Open Sicilian
                ("Open Sicilian", ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4", "g8f6", "b1c3"]),
                # Against 1... e6 (French Defense)
                ("French Defense", ["e2e4", "e7e6", "d2d4", "d7d5", "b1c3", "g8f6", "e4e5", "f6d7"])
            ]

            # 2. Black Repertoire: Sicilian Dragon vs 1.e4, King's Indian vs 1.d4
            black_lines = [
                # Sicilian Dragon Main Line vs 1.e4
                ("Sicilian Dragon", ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4", "g8f6", "b1c3", "g7g6", "c1e3", "f8g7", "f2f3", "e8g8", "d1d2", "b8c6", "f1c4", "c8d7"]),
                # King's Indian Defense vs 1.d4
                ("King's Indian Defense", ["d2d4", "g8f6", "c2c4", "g7g6", "b1c3", "f8g7", "e2e4", "d7d6", "g1f3", "e8g8", "f1e2", "e7e5", "e1g1", "b8c6", "d4d5", "c6e7"])
            ]

            # Phase 1 (read-only): decide, for every ply of every line, whether it may be written.
            plan: List[Tuple[str, str, str, str, bool]] = []
            conflicts: List[str] = []
            preserved: List[str] = []
            lines_total = 0
            lines_skipped = 0

            probe = get_db()
            probe_cursor = probe.cursor()
            try:
                for repertoire, parity, seed_lines in (
                    ("white", 0, white_lines),
                    ("black", 1, black_lines),
                ):
                    for study_name, line in seed_lines:
                        lines_total += 1
                        b = chess.Board()
                        abandoned = False
                        for ply, uci in enumerate(line):
                            p_fen = b.fen()
                            is_user = (ply % 2 == parity)
                            if is_user:
                                probe_cursor.execute("""
                                SELECT move_uci FROM repertoire_moves
                                WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1
                                """, (repertoire, normalize_fen(p_fen)))
                                existing = probe_cursor.fetchone()
                                if existing:
                                    if existing["move_uci"] != uci:
                                        # The user prepared a different move here: never overwrite it.
                                        conflicts.append(
                                            f"{repertoire}: {study_name} abandoned at {p_fen} "
                                            f"(user prepared {existing['move_uci']}, seed wanted {uci})"
                                        )
                                        abandoned = True
                                        break
                                    # Same move already prepared -> leave the row completely alone.
                                    preserved.append(f"{repertoire}: {study_name} kept user move {uci} at {p_fen}")
                                    b.push(chess.Move.from_uci(uci))
                                    continue
                            plan.append((repertoire, study_name, p_fen, uci, is_user))
                            b.push(chess.Move.from_uci(uci))
                        if abandoned:
                            lines_skipped += 1
            finally:
                probe.close()

            # Phase 2 (writes): only the plies that are provably free of a user-prepared move.
            # `replace_existing=False` is the safe default, so a planning bug here can no longer
            # destroy a prepared line; the plan phase above is what decides what may be written.
            seeded = 0
            write_conn = get_db()
            try:
                for (repertoire, study_name, p_fen, uci, is_user) in plan:
                    res = self.add_repertoire_move(
                        repertoire, p_fen, uci, is_user_move=is_user, study_name=study_name,
                        replace_existing=False, conn=write_conn,
                    )
                    if res.get("status") == "success":
                        seeded += 1
                write_conn.commit()
            finally:
                write_conn.close()

            return {
                "status": "success",
                "message": f"Seeded {seeded} move(s) from {lines_total - lines_skipped}/{lines_total} example line(s).",
                "moves_added": seeded,
                "lines_total": lines_total,
                "lines_skipped": lines_skipped,
                "user_moves_preserved": len(preserved),
                "conflicts": conflicts
            }

    def get_trainer_progress(self, repertoire: str = "white", study_name: Optional[str] = None) -> Dict[str, Any]:
        """Calculates Chessbook-style repertoire mastery: Fully Mastered, Partially Mastered (Learning), and Unlearned."""
        repertoire = (repertoire or "white").lower().strip()
        is_mixed = repertoire in ("mixed", "both", "all")
        if not is_mixed and repertoire not in ("white", "black"):
            repertoire = "white"

        conn = get_db()
        cursor = conn.cursor()
        try:
            # Fetch all user moves with their study association
            if is_mixed:
                cursor.execute("""
                SELECT rm.id, rm.parent_fen, rm.fen, rm.move_uci, rm.move_san, rm.times_practiced, rm.times_correct,
                       rm.last_practiced, rm.next_review, rm.interval_days, rm.study_name,
                       rm.consecutive_correct, rm.lapses, rm.difficulty,
                       COALESCE(rs.name, rm.study_name, '') as resolved_study
                FROM repertoire_moves rm
                LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                WHERE rm.is_user_move = 1
                """)
                rows = [dict(r) for r in cursor.fetchall()]

                cursor.execute("SELECT id, name FROM repertoire_studies")
                study_records = [dict(s) for s in cursor.fetchall()]
            else:
                cursor.execute("""
                SELECT rm.id, rm.parent_fen, rm.fen, rm.move_uci, rm.move_san, rm.times_practiced, rm.times_correct,
                       rm.last_practiced, rm.next_review, rm.interval_days, rm.study_name,
                       rm.consecutive_correct, rm.lapses, rm.difficulty,
                       COALESCE(rs.name, rm.study_name, '') as resolved_study
                FROM repertoire_moves rm
                LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                WHERE rm.repertoire = ? AND rm.is_user_move = 1
                """, (repertoire,))
                rows = [dict(r) for r in cursor.fetchall()]

                cursor.execute("SELECT id, name FROM repertoire_studies WHERE repertoire = ?", (repertoire,))
                study_records = [dict(s) for s in cursor.fetchall()]
        finally:
            conn.close()

        # Collect unique moves and all studies each move is associated with
        unique_user_moves = {}
        move_studies_map = {}

        for r in rows:
            # TRAINER-01: the progress counter must use the same LOGICAL position identity as
            # the writer and the grader, otherwise a transposed pair is counted twice and every
            # percentage is computed over a larger denominator than the trainer drills.
            key = (" ".join((r["parent_fen"] or "").split()[:3]), r["move_uci"])
            if key not in unique_user_moves:
                unique_user_moves[key] = r
                move_studies_map[key] = set()

            res_st = (r.get("resolved_study") or "").strip()
            if res_st:
                move_studies_map[key].add(res_st)
            st_name = (r.get("study_name") or "").strip()
            if st_name:
                move_studies_map[key].add(st_name)

        all_user_moves = list(unique_user_moves.values())

        def categorize_move(m):
            return classify_move_mastery(m)

        def _moves_key(m):
            return (" ".join((m["parent_fen"] or "").split()[:3]), m["move_uci"])

        def _segment(moves):
            """(total, mastered, learning, unlearned, the three percentages).

            TRAINER-15b: `unlearned_pct` used to be derived as `100 - mastered_pct - learning_pct`
            from the two *already rounded* numbers, so a third always disagreed with its own
            count (3 moves with 1 unlearned reported 33.4 %). Each segment is now computed from
            its own count; the three independently rounded values can therefore sum to 99.9 or
            100.1, which is a rounding artefact rather than a miscount.
            """
            total = len(moves)
            mastered = sum(1 for m in moves if categorize_move(m) == "mastered")
            learning = sum(1 for m in moves if categorize_move(m) == "learning")
            unlearned = total - mastered - learning
            if total > 0:
                return (total, mastered, learning, unlearned,
                        round((mastered / total * 100), 1),
                        round((learning / total * 100), 1),
                        round((unlearned / total * 100), 1))
            return (0, 0, 0, 0, 0.0, 0.0, 0.0)

        filter_clean = (study_name or "").strip()
        target_moves = all_user_moves
        if filter_clean:
            target_moves = [
                m for m in all_user_moves
                if filter_clean in move_studies_map.get(_moves_key(m), set())
            ]

        (total, mastered, learning, unlearned,
         mastered_pct, learning_pct, unlearned_pct) = _segment(target_moves)

        studies_breakdown = []
        for s in study_records:
            s_name = s["name"]
            s_moves = [
                m for m in all_user_moves
                if s_name in move_studies_map.get(_moves_key(m), set())
            ]
            (s_tot, s_mast, s_learn, s_unl,
             s_mast_pct, s_learn_pct, s_unl_pct) = _segment(s_moves)

            studies_breakdown.append({
                "study_id": s["id"],
                "name": s_name,
                "total_moves": s_tot,
                "mastered_count": s_mast,
                "mastered_pct": s_mast_pct,
                "learning_count": s_learn,
                "learning_pct": s_learn_pct,
                "unlearned_count": s_unl,
                "unlearned_pct": s_unl_pct,
            })

        return {
            "repertoire": repertoire,
            "filter_study": filter_clean or None,
            "total_moves": total,
            "mastered_count": mastered,
            "mastered_pct": mastered_pct,
            "learning_count": learning,
            "learning_pct": learning_pct,
            "unlearned_count": unlearned,
            "unlearned_pct": unlearned_pct,
            "studies": studies_breakdown
        }
    @staticmethod
    def format_pgn_moves(path_san: List[str]) -> str:
        """Formats a list of SAN moves into standard PGN string: e.g. '1. e4 e6 2. d4 d5'."""
        if not path_san:
            return ""
        result = []
        move_num = 1
        for i, san in enumerate(path_san):
            if i % 2 == 0:
                result.append(f"{move_num}. {san}")
            else:
                result.append(san)
                move_num += 1
        return " ".join(result)

    def build_repertoire_tree_graph(self, repertoire: str, study_filter: Optional[str] = None) -> Dict[int, Dict[str, Any]]:
        """
        Traverses the repertoire tree from root positions using BFS.
        Computes for every user move:
          - ply_depth: distance from root (1 for 1. e4, 2 for 1... c5, 3 for 2. Nf3, etc.)
          - path_san: list of string SAN moves leading to this position
          - path_formatted: formatted PGN string (e.g. '1. e4 e6 2. d4 d5')
          - ancestors: list of parent user move IDs along this branch
          - prerequisites_met: bool (True if all ancestor user moves have times_correct >= 1)
          - is_frontier: bool (True if move has times_practiced == 0 and prerequisites_met is True)
          - is_due: bool (True if times_practiced > 0 and next_review <= now)
          - urgency: float memory decay factor
        """
        conn = get_db()
        cursor = conn.cursor()
        now = int(time.time())

        # Map (parent_fen, move_uci) -> list of all study names
        cursor.execute("""
        SELECT sm.parent_fen, sm.move_uci, rs.name
        FROM study_moves sm
        JOIN repertoire_studies rs ON rs.id = sm.study_id
        WHERE sm.repertoire = ?
        """, (repertoire,))
        study_map = defaultdict(list)
        for r in cursor.fetchall():
            norm_p = normalize_fen(r["parent_fen"])
            p3 = " ".join(norm_p.split()[:3])
            sname = r["name"].strip()
            if sname and sname not in study_map[(norm_p, r["move_uci"])]:
                study_map[(norm_p, r["move_uci"])].append(sname)
            if sname and sname not in study_map[(p3, r["move_uci"])]:
                study_map[(p3, r["move_uci"])].append(sname)

        clean_filter = (study_filter or "").strip() or None
        if clean_filter:
            # TRAINER-04: this used to be an unescaped, unanchored
            # `rs.name LIKE '%<filter>%' OR rm.opening_name LIKE '%<filter>%'`, while
            # `get_trainer_progress` filters by exact study membership. Two studies whose names
            # are prefixes of one another (`Caro-Kann` / `Caro-Kann Advance`,
            # `Queen's Gambit` / `Queen's Gambit Declined`) are extremely common, and the
            # substring match made `train/next` serve a *different* study's move under the
            # selected label that `train/progress` then refused to count. `%` and `_` in the
            # filter were also plain LIKE wildcards. A study filter now means exactly one
            # study: exact equality on the resolved study name or on the opening name.
            cursor.execute("""
            SELECT DISTINCT rm.*, COALESCE(rs.name, rm.study_name) as effective_study
            FROM repertoire_moves rm
            LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
            LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
            WHERE rm.repertoire = ?
              AND (rs.name = ? OR rm.study_name = ? OR rm.opening_name = ?)
            """, (repertoire, clean_filter, clean_filter, clean_filter))
        else:
            cursor.execute("""
            SELECT DISTINCT rm.*, COALESCE(rs.name, rm.study_name) as effective_study
            FROM repertoire_moves rm
            LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
            LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
            WHERE rm.repertoire = ?
            """, (repertoire,))
        all_moves = [dict(r) for r in cursor.fetchall()]
        conn.close()

        by_parent = {}
        all_fen3 = set()
        for m in all_moves:
            p3 = " ".join(m["parent_fen"].split()[:3])
            m_fen3 = " ".join(m["fen"].split()[:3])
            by_parent.setdefault(p3, []).append(m)
            all_fen3.add(m_fen3)

        start_fen3 = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq"
        root_parents = [start_fen3] if start_fen3 in by_parent else []
        for p3 in by_parent:
            if p3 != start_fen3 and p3 not in all_fen3 and p3 not in root_parents:
                root_parents.append(p3)

        graph = {}
        visited = set()
        queue = []
        for rp in root_parents:
            queue.append((rp, 0, [], []))

        while queue:
            curr_fen3, depth, path_san, path_moves = queue.pop(0)
            for m in by_parent.get(curr_fen3, []):
                m_fen3 = " ".join(m["fen"].split()[:3])
                new_path_san = path_san + [m["move_san"]]
                new_path_moves = path_moves + [m]

                if m["is_user_move"] == 1:
                    ancestor_user_moves = [p for p in path_moves if p.get("is_user_move") == 1]
                    prereqs_met = all((p.get("times_correct") or 0) >= 1 for p in ancestor_user_moves)
                    mem = infer_move_memory_state(m, now=now)
                    tp = mem["tp"]
                    tc = mem["tc"]
                    next_rev = m.get("next_review") or 0
                    interv = max(1.0, mem["stability"])
                    is_due = (tp > 0 and next_rev > 0 and next_rev <= now)
                    urgency = (now - next_rev) / (interv * 86400.0) if is_due else 0.0
                    mastery = classify_move_mastery(m)
                    is_frontier = (tp == 0 and prereqs_met)
                    node_depth = depth + 1

                    if mem["is_weak"]:
                        node_weakness = 8.0 + mem["lapses"] * 1.5 + mem["difficulty"] * 0.3 + (1.0 - mem["retrievability"]) * 2.0
                    elif is_due:
                        node_weakness = 5.0 + min(6.0, urgency * 2.0) + (1.0 - mem["retrievability"]) * 3.0 + (mem["difficulty"] - 5.0) * 0.25
                    elif is_frontier:
                        node_weakness = 4.0 + min(2.0, float(m.get("popularity_pct") or 0.0) / 25.0) + (1.5 if m.get("is_important") else 0.0)
                    elif mastery == "learning":
                        node_weakness = 2.5 + (1.0 - mem["retrievability"]) * 3.0
                    else:
                        node_weakness = 0.5 + (1.0 - mem["retrievability"]) * 2.5 + min(1.6, node_depth * 0.1)

                    norm_parent = normalize_fen(m["parent_fen"])
                    parent_3 = " ".join(norm_parent.split()[:3])
                    m_studies = list(study_map.get((norm_parent, m["move_uci"])) or study_map.get((parent_3, m["move_uci"])) or [])
                    fallback = (m.get("effective_study") or m.get("study_name") or m.get("opening_name") or "").strip()
                    if fallback and fallback not in m_studies:
                        m_studies.append(fallback)

                    if m["id"] in graph:
                        for s in m_studies:
                            if s not in graph[m["id"]]["studies"]:
                                graph[m["id"]]["studies"].append(s)
                    else:
                        graph[m["id"]] = {
                            "move": m,
                            "ply_depth": node_depth,
                            "path_san": path_san,
                            "path_formatted": self.format_pgn_moves(path_san),
                            "ancestors": [p["id"] for p in ancestor_user_moves],
                            "prerequisites_met": prereqs_met,
                            "is_frontier": is_frontier,
                            "is_due": is_due,
                            "urgency": urgency,
                            "mastery_status": mastery,
                            "retrievability": mem["retrievability"],
                            "difficulty": mem["difficulty"],
                            "stability": mem["stability"],
                            "lapses": mem["lapses"],
                            "streak": mem["streak"],
                            "is_leech": mem["is_leech"],
                            "is_weak": mem["is_weak"],
                            "node_weakness": node_weakness,
                            "subtree_weakness": node_weakness,
                            "studies": m_studies,
                            "effective_study": m_studies[0] if m_studies else fallback
                        }

                if m_fen3 not in visited:
                    visited.add(m_fen3)
                    queue.append((m_fen3, depth + 1, new_path_san, new_path_moves))

        # Catch any disconnected user moves
        for m in all_moves:
            if m["is_user_move"] == 1 and m["id"] not in graph:
                mem = infer_move_memory_state(m, now=now)
                tp = mem["tp"]
                tc = mem["tc"]
                next_rev = m.get("next_review") or 0
                interv = max(1.0, mem["stability"])
                is_due = (tp > 0 and next_rev > 0 and next_rev <= now)
                urgency = (now - next_rev) / (interv * 86400.0) if is_due else 0.0
                mastery = classify_move_mastery(m)
                is_frontier = (tp == 0)
                if mem["is_weak"]:
                    node_weakness = 8.0 + mem["lapses"] * 1.5 + mem["difficulty"] * 0.3 + (1.0 - mem["retrievability"]) * 2.0
                elif is_due:
                    node_weakness = 5.0 + min(6.0, urgency * 2.0) + (1.0 - mem["retrievability"]) * 3.0 + (mem["difficulty"] - 5.0) * 0.25
                elif is_frontier:
                    node_weakness = 4.0 + min(2.0, float(m.get("popularity_pct") or 0.0) / 25.0)
                elif mastery == "learning":
                    node_weakness = 2.5 + (1.0 - mem["retrievability"]) * 3.0
                else:
                    node_weakness = 0.5 + (1.0 - mem["retrievability"]) * 2.5 + 1.0

                norm_parent = normalize_fen(m["parent_fen"])
                parent_3 = " ".join(norm_parent.split()[:3])
                m_studies = list(study_map.get((norm_parent, m["move_uci"])) or study_map.get((parent_3, m["move_uci"])) or [])
                fallback = (m.get("effective_study") or m.get("study_name") or m.get("opening_name") or "").strip()
                if fallback and fallback not in m_studies:
                    m_studies.append(fallback)

                graph[m["id"]] = {
                    "move": m,
                    "ply_depth": 99,
                    "path_san": [],
                    "path_formatted": "",
                    "ancestors": [],
                    "prerequisites_met": True,
                    "is_frontier": is_frontier,
                    "is_due": is_due,
                    "urgency": urgency,
                    "mastery_status": mastery,
                    "retrievability": mem["retrievability"],
                    "difficulty": mem["difficulty"],
                    "stability": mem["stability"],
                    "lapses": mem["lapses"],
                    "streak": mem["streak"],
                    "is_leech": mem["is_leech"],
                    "is_weak": mem["is_weak"],
                    "node_weakness": node_weakness,
                    "subtree_weakness": node_weakness,
                    "studies": m_studies,
                    "effective_study": m_studies[0] if m_studies else fallback
                }

        # Tree-aware backpropagation: propagate each descendant's weakness up to its ancestors
        # so shallow entry moves know which branches hold downstream mistakes, due cards or gaps.
        for node in graph.values():
            nw = node["node_weakness"]
            ancs = node.get("ancestors") or []
            total_ancs = len(ancs)
            for idx, anc_id in enumerate(ancs):
                if anc_id in graph:
                    dist = max(1, total_ancs - idx)
                    discounted = nw * (0.92 ** dist)
                    if discounted > graph[anc_id]["subtree_weakness"]:
                        graph[anc_id]["subtree_weakness"] = discounted

        return graph

    def get_next_trainer_drill(
        self,
        repertoire: str,
        opening_filter: Optional[str] = None,
        session_mode: str = "adaptive",
        recent_session_lapses: Optional[List[int]] = None,
        recent_drilled_ids: Optional[List[int]] = None
    ) -> Optional[Dict]:
        repertoire = (repertoire or "").lower().strip()
        if repertoire in ("mixed", "both", "all"):
            # Determine last drilled color for alternating balance
            last_color = None
            if recent_drilled_ids:
                try:
                    conn = get_db()
                    cursor = conn.cursor()
                    cursor.execute("SELECT repertoire FROM repertoire_moves WHERE id = ?", (recent_drilled_ids[-1],))
                    r_row = cursor.fetchone()
                    conn.close()
                    if r_row:
                        last_color = r_row["repertoire"]
                except Exception:
                    pass

            drill_w = self._get_single_trainer_drill("white", opening_filter, session_mode, recent_session_lapses, recent_drilled_ids)
            drill_b = self._get_single_trainer_drill("black", opening_filter, session_mode, recent_session_lapses, recent_drilled_ids)

            is_real_w = bool(drill_w and "expected_uci" in drill_w)
            is_real_b = bool(drill_b and "expected_uci" in drill_b)

            if is_real_w and not is_real_b:
                return drill_w
            if is_real_b and not is_real_w:
                return drill_b
            if not is_real_w and not is_real_b:
                return drill_w or drill_b

            # Both have real moves: check priority
            reason_priority = {
                "session_lapse": 4,
                "leech_repair": 3,
                "due_review": 3,
                "frontier_new": 2,
                "mastery_audit": 1,
                "maintenance": 1,
                "mastery_refresh": 1
            }
            p_w = reason_priority.get(drill_w.get("drill_reason", ""), 0)
            p_b = reason_priority.get(drill_b.get("drill_reason", ""), 0)

            if p_w > p_b:
                return drill_w
            elif p_b > p_w:
                return drill_b

            # Equal priority: alternate colors
            if last_color == "white":
                return drill_b
            elif last_color == "black":
                return drill_w
            else:
                return random.choice([drill_w, drill_b])

        return self._get_single_trainer_drill(repertoire, opening_filter, session_mode, recent_session_lapses, recent_drilled_ids)

    def _get_single_trainer_drill(
        self,
        repertoire: str,
        opening_filter: Optional[str] = None,
        session_mode: str = "adaptive",
        recent_session_lapses: Optional[List[int]] = None,
        recent_drilled_ids: Optional[List[int]] = None
    ) -> Optional[Dict]:
        """
        Hybrid FSRS + Tree-Aware + Mistake-Consolidation + Mastered-Audit drill selector:
        1. Cooldown-Ready In-Session Lapses (re-test session mistakes after 3-drill spacing lag)
        2. Mastered Spot-Check Audit (~20% cadence in adaptive sessions or in mastered_audit mode,
           targeting deep critical mastered positions with lowest FSRS retrievability)
        3. Weak / Leech Consolidation & FSRS Due Reviews (urgency + retrievability decay + difficulty)
        4. Learnable Frontier Moves (progressive depth + subtree weakness priority)
        5. Learning Consolidation (1-hit moves needing a 2nd recall to graduate to mastered)
        6. Maintenance / Retention Refresh (alternating subtree-guided line walks and deep spot-checks)
        """
        repertoire = (repertoire or "").lower()
        if repertoire not in ("white", "black"):
            return None

        now = int(time.time())
        target_study = (opening_filter or "").strip() or None

        graph = self.build_repertoire_tree_graph(repertoire, study_filter=target_study)
        if not graph:
            return None

        # Queue Summary (using redeemable mastery classification)
        due_nodes = [node for node in graph.values() if node["is_due"]]
        frontier_nodes = [node for node in graph.values() if node["is_frontier"]]
        mastered_nodes = [node for node in graph.values() if node["mastery_status"] == "mastered"]
        learning_nodes = [node for node in graph.values() if node["mastery_status"] == "learning"]
        unlearned_nodes = [node for node in graph.values() if node["mastery_status"] == "unlearned"]
        weak_nodes = [node for node in graph.values() if node["is_weak"]]
        leech_nodes = [node for node in graph.values() if node["is_leech"]]

        queue_summary = {
            "due_count": len(due_nodes),
            "frontier_count": len(frontier_nodes),
            "mastered_count": len(mastered_nodes),
            "learning_count": len(learning_nodes),
            "unlearned_count": len(unlearned_nodes),
            "weak_count": len(weak_nodes),
            "leech_count": len(leech_nodes),
            "total_moves": len(graph)
        }

        recent_list = list(recent_drilled_ids or [])
        recent_ids_set = set(recent_list)
        step_idx = len(recent_list)
        chosen_node = None
        chosen_study = None
        drill_reason = "frontier_new"
        is_mastered_audit = False

        def _resolve_study(node):
            if target_study:
                return target_study
            if node.get("studies"):
                return node["studies"][0]
            return node.get("effective_study")

        def _audit_score(n):
            """Scores a mastered position for cold spot-checking:
            Prioritises memory decay (1 - R), deeper branching positions (ply >= 3) over move 1,
            historical difficulty/lapses, and importance."""
            decay = 1.0 - float(n.get("retrievability") or 0.0)
            depth = min(14, int(n.get("ply_depth") or 1))
            depth_bonus = (depth * 0.15) if depth >= 3 else 0.05
            history_risk = float(n.get("lapses") or 0) * 0.6 + (float(n.get("difficulty") or 5.0) - 5.0) * 0.2
            imp = 0.5 if n["move"].get("is_important") else 0.0
            last_p = int(n["move"].get("last_practiced") or 0)
            return (round(decay * 4.0 + depth_bonus + history_risk + imp, 4), -last_p, -int(n["move"]["id"]))

        # Collect distinct studies represented in this repertoire graph
        all_studies = []
        for n in graph.values():
            for s in n.get("studies", []):
                if s and s not in all_studies:
                    all_studies.append(s)
            fallback_s = (n.get("effective_study") or n["move"].get("study_name") or "").strip()
            if fallback_s and fallback_s not in all_studies:
                all_studies.append(fallback_s)

        # Track study history from recent drills (most recent to least recent)
        recent_study_history = []
        for mid in reversed(recent_list):
            if mid in graph:
                for st in graph[mid].get("studies", []):
                    if st and st not in recent_study_history:
                        recent_study_history.append(st)

        # Candidate studies: unseen studies first (shuffled for equal distribution when starting fresh),
        # followed by seen studies ordered by least recently trained.
        unseen_studies = [s for s in all_studies if s not in recent_study_history]
        random.shuffle(unseen_studies)
        seen_studies = list(reversed(recent_study_history))
        candidate_studies = unseen_studies + seen_studies
        if not candidate_studies:
            candidate_studies = all_studies[:]

        # --- TIER 1: In-Session Lapses (with 3-Drill Cooldown Spacing) ---
        if recent_session_lapses:
            valid_lapses = [mid for mid in recent_session_lapses if mid in graph]
            if valid_lapses:
                def _lapse_rank(mid):
                    if not recent_ids_set or mid not in recent_ids_set:
                        return -1
                    for idx in range(len(recent_list) - 1, -1, -1):
                        if recent_list[idx] == mid:
                            return idx
                    return -1

                if session_mode == "lapses_only" or not recent_list:
                    eligible_lapses = valid_lapses
                else:
                    cooldown_window = set(recent_list[-SESSION_LAPSE_COOLDOWN_DRILLS:])
                    cooled = [mid for mid in valid_lapses if mid not in cooldown_window]
                    other_available = any(
                        nid not in cooldown_window
                        and nid not in valid_lapses
                        and (
                            graph[nid]["is_due"]
                            or graph[nid]["is_frontier"]
                            or graph[nid]["is_weak"]
                            or graph[nid]["mastery_status"] in ("learning", "mastered")
                        )
                        for nid in graph
                    )
                    if cooled:
                        eligible_lapses = cooled
                    elif not other_available:
                        eligible_lapses = valid_lapses
                    else:
                        # Hold lapse in cooldown buffer while 1-3 other moves are drilled first
                        eligible_lapses = []

                if eligible_lapses:
                    ordered = sorted(
                        eligible_lapses,
                        key=lambda mid: (0 if _lapse_rank(mid) < 0 else 1, _lapse_rank(mid), mid),
                    )
                    chosen_node = graph[ordered[0]]
                    drill_reason = "session_lapse"
                    chosen_study = _resolve_study(chosen_node)

        if session_mode == "lapses_only":
            if not chosen_node:
                return {
                    "repertoire": repertoire,
                    "all_lapses_completed": True,
                    "queue_summary": queue_summary,
                    "message": "All missed moves reinforced! 🎉"
                }

        # --- Dedicated Mode: Weak & Leeches Only ---
        if not chosen_node and session_mode == "weak_only":
            avail_weak = [n for n in weak_nodes if n["move"]["id"] not in recent_ids_set] or weak_nodes
            if not avail_weak:
                avail_weak = [n for n in due_nodes if n["move"]["id"] not in recent_ids_set] or due_nodes
            if avail_weak:
                avail_weak.sort(
                    key=lambda n: (
                        n["lapses"],
                        n["difficulty"],
                        1.0 - n["retrievability"],
                        int(n["move"].get("is_important") or 0),
                    ),
                    reverse=True,
                )
                chosen_node = avail_weak[0]
                drill_reason = "due_review"
                chosen_study = _resolve_study(chosen_node)
            else:
                return {
                    "repertoire": repertoire,
                    "all_due_completed": True,
                    "queue_summary": queue_summary,
                    "message": "All weak spots and leeches consolidated! 🎉"
                }

        # --- Dedicated Mode: Mastered Spot-Check Audit ---
        if not chosen_node and session_mode == "mastered_audit":
            avail_mast = [n for n in mastered_nodes if n["move"]["id"] not in recent_ids_set] or mastered_nodes
            if not avail_mast:
                avail_mast = [n for n in learning_nodes if n["move"]["id"] not in recent_ids_set] or list(graph.values())
            avail_mast.sort(key=_audit_score, reverse=True)
            chosen_node = avail_mast[0]
            drill_reason = "maintenance"
            is_mastered_audit = True
            chosen_study = _resolve_study(chosen_node)

        # --- TIER 1.5 (Adaptive): Mastered Spot-Check Quota (~20% of session) ---
        # Periodically spot-checks previously mastered positions (preferring deep positions with
        # decayed retrievability) even while due or frontier moves exist.
        if (
            not chosen_node
            and session_mode == "adaptive"
            and mastered_nodes
            and step_idx > 0
            and (step_idx + 1) % MASTERED_AUDIT_CADENCE == 0
        ):
            avail_audit = [n for n in mastered_nodes if n["move"]["id"] not in recent_ids_set] or mastered_nodes
            avail_audit.sort(key=_audit_score, reverse=True)
            chosen_node = avail_audit[0]
            drill_reason = "maintenance"
            is_mastered_audit = True
            chosen_study = _resolve_study(chosen_node)

        # --- TIER 2: Spaced Repetition Due Reviews (FSRS Retrievability + Ebbinghaus Urgency) ---
        if not chosen_node and session_mode in ("adaptive", "due_only"):
            available_due = [node for node in due_nodes if node["move"]["id"] not in recent_ids_set]
            if not available_due and due_nodes and session_mode == "due_only":
                available_due = due_nodes
            elif not available_due and due_nodes and not frontier_nodes and not weak_nodes:
                available_due = due_nodes

            if available_due:
                available_due.sort(
                    key=lambda n: (
                        round(
                            n["urgency"]
                            + (1.0 - n["retrievability"]) * 0.5
                            + n["lapses"] * 0.35
                            + (n["difficulty"] - 5.0) * 0.05,
                            4,
                        ),
                        int(n["move"].get("is_important") or 0),
                    ),
                    reverse=True
                )
                chosen_node = available_due[0]
                drill_reason = "due_review"
                chosen_study = _resolve_study(chosen_node)
            elif session_mode == "due_only":
                return {
                    "repertoire": repertoire,
                    "all_due_completed": True,
                    "queue_summary": queue_summary,
                    "message": "All reviews are up to date! 🎉"
                }

        # --- TIER 2.5 (Adaptive): Weak / Leech Recovery Interleaving ---
        # Consolidates positions with past mistakes (`is_weak`) that aren't strictly past their
        # 24h `next_review` timestamp yet, interleaving them with frontier moves.
        if not chosen_node and session_mode == "adaptive":
            avail_weak = [
                n for n in weak_nodes
                if not n["is_due"] and n["move"]["id"] not in recent_ids_set
            ]
            if avail_weak and (not frontier_nodes or (step_idx > 0 and step_idx % 2 == 1)):
                avail_weak.sort(
                    key=lambda n: (
                        n["lapses"],
                        n["difficulty"],
                        1.0 - n["retrievability"],
                        int(n["move"].get("is_important") or 0),
                    ),
                    reverse=True,
                )
                chosen_node = avail_weak[0]
                drill_reason = "due_review"
                chosen_study = _resolve_study(chosen_node)

        # --- TIER 3: Learnable Frontier Moves (Root-to-Leaf Progressive Depth + Subtree Weakness) ---
        if not chosen_node and session_mode in ("adaptive", "frontier_only"):
            available_frontier = [node for node in frontier_nodes if node["move"]["id"] not in recent_ids_set]
            if not available_frontier and frontier_nodes and session_mode == "frontier_only":
                available_frontier = frontier_nodes
            elif not available_frontier and frontier_nodes and not learning_nodes and not mastered_nodes:
                available_frontier = frontier_nodes

            if available_frontier:
                if not target_study and candidate_studies:
                    for cand_st in candidate_studies:
                        matching_frontier = [n for n in available_frontier if cand_st in n.get("studies", []) or n.get("effective_study") == cand_st]
                        if matching_frontier:
                            matching_frontier.sort(
                                key=lambda n: (
                                    n["ply_depth"],
                                    -int(n["move"].get("is_important") or 0),
                                    -float(n["move"].get("popularity_pct") or 0.0),
                                    -round(n["subtree_weakness"], 3),
                                )
                            )
                            chosen_node = matching_frontier[0]
                            chosen_study = cand_st
                            drill_reason = "frontier_new"
                            break

                if not chosen_node:
                    available_frontier.sort(
                        key=lambda n: (
                            n["ply_depth"],
                            -int(n["move"].get("is_important") or 0),
                            -float(n["move"].get("popularity_pct") or 0.0),
                            -round(n["subtree_weakness"], 3),
                        )
                    )
                    chosen_node = available_frontier[0]
                    drill_reason = "frontier_new"
                    chosen_study = _resolve_study(chosen_node)
            elif session_mode == "frontier_only":
                return {
                    "repertoire": repertoire,
                    "all_frontier_completed": True,
                    "queue_summary": queue_summary,
                    "message": "All repertoire lines have been learned! 🎉"
                }

        # --- TIER 3.5 (Adaptive): Learning Consolidation ---
        # Consolidates partially learned moves (`mastery_status == 'learning'`) that haven't
        # been drilled in this session so they can graduate to `'mastered'`.
        if not chosen_node and session_mode == "adaptive" and learning_nodes:
            avail_learning = [n for n in learning_nodes if n["move"]["id"] not in recent_ids_set]
            if avail_learning:
                avail_learning.sort(
                    key=lambda n: (
                        n["lapses"],
                        n["difficulty"],
                        1.0 - n["retrievability"],
                        int(n["move"].get("is_important") or 0),
                        n["ply_depth"],
                        -(n["move"].get("last_practiced") or 0),
                    ),
                    reverse=True,
                )
                chosen_node = avail_learning[0]
                drill_reason = "due_review"
                chosen_study = _resolve_study(chosen_node)

        # --- TIER 4: Maintenance / Mastered Verification ---
        if not chosen_node:
            drill_reason = "maintenance"
            if target_study:
                chosen_study = target_study
            elif candidate_studies:
                chosen_study = candidate_studies[0]
            else:
                chosen_study = all_studies[0] if all_studies else None

            study_nodes = []
            if chosen_study:
                study_nodes = [n for n in graph.values() if chosen_study in n.get("studies", []) or n.get("effective_study") == chosen_study]
            if not study_nodes:
                study_nodes = list(graph.values())

            unseen_study_nodes = [n for n in study_nodes if n["move"]["id"] not in recent_ids_set]
            if unseen_study_nodes:
                study_nodes = unseen_study_nodes

            # Alternate between:
            # - starting from shallow positions of the highest-weakness/decay subtree (even step_idx), and
            # - jumping directly to deep mastered positions via `_audit_score` (odd step_idx)
            if step_idx > 0 and step_idx % 2 == 1:
                study_nodes.sort(key=_audit_score, reverse=True)
                is_mastered_audit = True
            else:
                study_nodes.sort(
                    key=lambda n: (
                        n["ply_depth"],
                        -round(n["subtree_weakness"], 3),
                        n["move"].get("last_practiced") or 0,
                    )
                )
            chosen_node = study_nodes[0]

        chosen = chosen_node["move"]
        p_fen = chosen["parent_fen"].strip()
        parts = p_fen.split()
        if len(parts) == 4:
            # TRAINER-15a: this used to append the hard-coded clock "0 1", so a ply-9 castling
            # drill was announced to the client as "move 1". The halfmove clock is genuinely
            # unrecoverable (only 4 FEN tokens are stored), but the fullmove number is
            # derivable from the drill's own depth and is no longer a lie.
            ply_depth = int(chosen_node.get("ply_depth") or 0)
            fullmove = max(1, (ply_depth + 1) // 2)
            p_fen += f" 0 {fullmove}"
        elif len(parts) == 5:
            p_fen += " 1"

        # Check for opponent's previous move that reached this position
        last_move = None
        conn = get_db()
        cursor = conn.cursor()
        parent_3 = " ".join(p_fen.split()[:3])
        # Audit FIXER-A escalation 4 (the CODE-3 tie-breaker): the `fen` column stores the 4-token
        # normalisation, while `p_fen` above has been extended with the halfmove/fullmove counters,
        # so `fen = ?` could never match and a bare `LIMIT 1` answered from the fuzzy 3-token
        # branch in whatever order the rows came. The exact 4-token key now wins deterministically.
        norm_p4 = " ".join(p_fen.split()[:4])
        cursor.execute("""
        SELECT * FROM repertoire_moves
        WHERE repertoire = ? AND (fen = ? OR fen = ? OR fen LIKE ?)
        ORDER BY (fen = ?) DESC, is_important DESC, id ASC
        LIMIT 1
        """, (repertoire, norm_p4, parent_3, f"{parent_3} %", norm_p4))
        prev_row = cursor.fetchone()
        conn.close()
        if prev_row:
            uci = prev_row["move_uci"]
            last_move = {
                "from": uci[:2],
                "to": uci[2:4],
                "san": prev_row["move_san"]
            }

        effective_study = (target_study or chosen_study or chosen_node.get("effective_study") or chosen.get("study_name") or chosen.get("opening_name", "")).strip()
        opening_display = target_study or chosen_study or chosen_node.get("effective_study") or chosen.get("study_name") or chosen.get("opening_name") or effective_study

        tp = chosen.get("times_practiced") or 0
        tc = chosen.get("times_correct") or 0
        mastery = classify_move_mastery(chosen)

        return {
            "id": chosen["id"],
            "repertoire": repertoire,
            "parent_fen": p_fen,
            "expected_uci": chosen["move_uci"],
            "expected_san": chosen["move_san"],
            "opening_name": opening_display,
            "study_name": effective_study,
            "eco": chosen["eco"],
            "is_important": bool(chosen.get("is_important")),
            "comment": chosen.get("comment", "") or "",
            "arrows": chosen.get("arrows", "") or "",
            "last_move": last_move,
            "mastery_status": mastery,
            "times_practiced": tp,
            "times_correct": tc,
            "consecutive_correct": chosen_node["streak"],
            "lapses": chosen_node["lapses"],
            "difficulty": chosen_node["difficulty"],
            "retrievability": round(chosen_node["retrievability"], 3),
            "is_leech": chosen_node["is_leech"],
            "is_mastered_audit": is_mastered_audit,
            "path_san": chosen_node["path_san"],
            "path_formatted": chosen_node["path_formatted"],
            "drill_reason": drill_reason,
            "ply_depth": chosen_node["ply_depth"],
            "queue_summary": queue_summary
        }

    def get_repertoire_openings(self, repertoire: Optional[str] = None) -> Dict[str, Any]:
        """Returns distinct user/seeded studies in the repertoire as openings for the trainer dropdown."""
        clean_rep = None if (not repertoire or repertoire.lower().strip() in ("mixed", "both", "all")) else repertoire.lower().strip()
        studies = self.get_studies(clean_rep)
        openings = []
        for s in studies:
            openings.append({
                "opening_name": s["name"],
                "study_id": s["id"],
                "eco": "",
                # TRAINER-15g: this is the number of moves the trainer can actually drill, so it
                # must be the *user* moves. Falling back to `total_moves` advertised a study
                # that holds only opponent replies as having "1 move" and then served an empty
                # drill queue.
                "move_count": s["user_moves"] or 0,
                "repertoire": s["repertoire"]
            })

        recent_raw = get_setting("recent_learned_opening", "")
        recent_opening = None
        if recent_raw:
            try:
                recent_opening = json.loads(recent_raw)
            except Exception:
                recent_opening = {"name": recent_raw, "eco": ""}

        return {
            "openings": openings,
            "recent_opening": recent_opening
        }

    def evaluate_trainer_move(self, repertoire: str, current_fen: str, attempted_uci: str, study_filter: Optional[str] = None, is_retry: bool = False) -> Dict:
        """Validates user's move against prepared repertoire and picks opponent's weighted reply within active study.

        Audit TRAINER-02 / ABUSE-05: the spaced-repetition update used to be an untransacted
        read-modify-write (`SELECT` -> compute in Python -> `UPDATE` with absolute values) on a
        connection with sqlite3's default `isolation_level=""`, so every concurrent request read
        the same starting counters and the last writer won: 24 accepted answers were recorded as
        9, one card reached a 747-day interval, and `tc == tp` could be satisfied while six
        graded mistakes vanished (minting "mastered"). The read and the write now share one
        explicit `BEGIN IMMEDIATE` transaction, so the counters are serialised and the
        derived interval is computed from the values read *inside* that transaction.
        """
        repertoire = (repertoire or "").lower()
        if repertoire not in ("white", "black"):
            return {"correct": False, "message": f"Invalid repertoire color: {repertoire}"}

        try:
            board = chess.Board(current_fen)
            if not board.is_valid():
                return {"correct": False, "message": "Invalid FEN string"}
        except Exception:
            return {"correct": False, "message": "Invalid FEN string"}

        norm_fen = normalize_fen(current_fen)

        # Sanitize UCI string
        attempted_uci = (attempted_uci or "").strip().lower()
        if len(attempted_uci) == 5:
            # TRAINER-15f: any 5-character string whose 4th character was not `1`/`8` used to be
            # silently truncated to 4 characters, so `e2e4q` was graded as a correct `e4`. A
            # promotion suffix is a real UCI (`a7a8Q`); anything else is a malformed move and is
            # rejected instead of being turned into an answer.
            try:
                from_sq = chess.parse_square(attempted_uci[:2])
                piece = board.piece_at(from_sq)
                is_promotion = bool(piece and piece.piece_type == chess.PAWN
                                    and attempted_uci[3] in ('1', '8'))
            except Exception:
                is_promotion = False
            if not is_promotion:
                return {
                    "correct": False,
                    "message": f"Invalid move format: {attempted_uci}."
                }

        # Check legality on board
        try:
            move_obj = chess.Move.from_uci(attempted_uci)
            if move_obj not in board.legal_moves:
                return {
                    "correct": False,
                    "message": f"Illegal move: {attempted_uci} is not legal in this position."
                }
        except Exception:
            return {
                "correct": False,
                "message": f"Invalid move format: {attempted_uci}."
            }

        conn = get_db()
        cursor = conn.cursor()
        in_txn = False
        try:
            norm_3 = " ".join(norm_fen.split()[:3])
            # TRAINER-02 / ABUSE-05: serialise the whole grade-and-schedule step. `BEGIN IMMEDIATE`
            # takes SQLite's write lock up front, so two concurrent attempts for the same card
            # cannot both read the same `times_practiced` and then both write `prev + 1`.
            cursor.execute("BEGIN IMMEDIATE")
            in_txn = True

            # Find user's prepared move for this position (matching 4-token or 3-token prefix).
            # CODE-3: this predicate can match more than one row (two positions sharing a 3-token
            # prefix but differing in the en-passant token are both stored, and the LEFT JOIN to
            # study_moves multiplies a row that belongs to several studies). A bare `LIMIT 1` with
            # no ORDER BY then let SQLite answer with whichever row came first by rowid, so the
            # trainer validated - and incremented the spaced-repetition counters of - a move that
            # belongs to a *different* position. The lookup is now deterministic: an exact 4-token
            # parent always wins over a 3-token/LIKE near-miss, then the most important move, then
            # the lowest id.
            cursor.execute("""
            SELECT rm.*, COALESCE(rs.name, rm.study_name) as effective_study FROM repertoire_moves rm
            LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
            LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
            WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 1
            ORDER BY (rm.parent_fen = ?) DESC, rm.is_important DESC, rm.id ASC
            LIMIT 1
            """, (repertoire, norm_fen, norm_3, f"{norm_3} %", norm_fen))
            prepared = cursor.fetchone()

            if not prepared:
                conn.rollback()
                in_txn = False
                return {
                    "correct": False,
                    "message": "This position is not yet in your repertoire tree.",
                    "is_in_tree": False
                }

            prepared = dict(prepared)
            expected_uci = prepared["move_uci"]
            expected_san = prepared["move_san"]
            active_study = (study_filter or "").strip() or prepared.get("effective_study") or prepared.get("study_name") or ""

            # Validate match
            is_match = (attempted_uci == expected_uci)
            now = int(time.time())

            mem_state = infer_move_memory_state(prepared, now=now)
            prev_tp = mem_state["tp"]
            prev_tc = mem_state["tc"]
            prev_lapses = mem_state["lapses"]
            prev_streak = mem_state["streak"]
            prev_diff = mem_state["difficulty"]
            prev_interval = max(1.0, mem_state["stability"])
            last_prac = max(0, int(prepared.get("last_practiced") or 0))

            if not is_match:
                new_tp = min(9223372036854775807, prev_tp + 1)
                new_tc = prev_tc
                new_lapses = min(9223372036854775807, prev_lapses + 1)
                new_streak = 0
                new_diff = min(FSRS_MAX_DIFFICULTY, round(prev_diff + 1.5, 2))
                # On mistake: reset interval back to 1 day, reset consecutive_correct to 0,
                # increment lapses and raise difficulty so the card is prioritized for consolidation.
                cursor.execute("""
                UPDATE repertoire_moves
                SET times_practiced = ?,
                    interval_days = 1.0,
                    last_practiced = ?,
                    next_review = ?,
                    consecutive_correct = 0,
                    lapses = ?,
                    difficulty = ?
                WHERE id = ?
                """, (new_tp, now, now + 86400, new_lapses, new_diff, prepared["id"]))
                conn.commit()
                in_txn = False

                updated_row = dict(prepared)
                updated_row.update({
                    "times_practiced": new_tp,
                    "times_correct": new_tc,
                    "interval_days": 1.0,
                    "consecutive_correct": 0,
                    "lapses": new_lapses,
                    "difficulty": new_diff,
                })
                mastery = classify_move_mastery(updated_row)
                return {
                    "correct": False,
                    "is_retry": bool(is_retry),
                    "prepared_id": prepared["id"],
                    "expected_san": expected_san,
                    "expected_uci": expected_uci,
                    "study_name": active_study,
                    "mastery_status": mastery,
                    "times_practiced": new_tp,
                    "times_correct": new_tc,
                    "consecutive_correct": 0,
                    "lapses": new_lapses,
                    "difficulty": new_diff,
                    "message": f"Not in your repertoire. Your prepared move is {expected_san}."
                }

            # Success on user move!
            if is_retry:
                # User played the correct move on a retry (or assisted) after a mistake or hint.
                # It is NOT a win: do NOT increment times_correct, do NOT advance spaced repetition interval!
                new_tp = prev_tp
                new_tc = prev_tc
                new_lapses = prev_lapses
                new_streak = prev_streak
                new_diff = prev_diff
                conn.commit()
                in_txn = False
                mastery = classify_move_mastery(prepared)
            else:
                new_tp = min(9223372036854775807, prev_tp + 1)
                new_tc = min(9223372036854775807, prev_tc + 1)
                new_lapses = prev_lapses
                new_streak = min(9223372036854775807, prev_streak + 1)

                r_now = compute_fsrs_retrievability(last_prac, prev_interval, now=now) if last_prac > 0 else 1.0
                diff_delta = 0.35 + 0.25 * (1.0 - r_now)
                new_diff = max(FSRS_MIN_DIFFICULTY, round(prev_diff - diff_delta, 2))

                if new_streak == 1:
                    new_interval = 1.0
                elif new_streak == 2:
                    # Chronic leech (3+ lapses) grows more conservatively on streak 2
                    new_interval = 2.0 if new_lapses >= 3 else 3.0
                else:
                    # FSRS-inspired stability expansion: scales by difficulty and spacing bonus
                    eff_diff = FSRS_DEFAULT_DIFFICULTY if (new_lapses == 0 and new_streak <= 3) else new_diff
                    diff_factor = max(1.4, min(2.8, 2.2 - 0.12 * (eff_diff - 5.0)))
                    spacing_bonus = 1.0 if (new_lapses == 0 and new_streak <= 3) else (1.0 + 0.5 * (1.0 - r_now))
                    new_interval = min(3650.0, max(3.0, round(prev_interval * diff_factor * spacing_bonus, 1)))

                max_future_seconds = int(3650.0 * 86400)
                review_delta = min(max_future_seconds, int(new_interval * 86400))
                next_review_ts = min(9223372036854775807, now + review_delta)

                cursor.execute("""
                UPDATE repertoire_moves
                SET times_practiced = ?,
                    times_correct = ?,
                    interval_days = ?,
                    last_practiced = ?,
                    next_review = ?,
                    consecutive_correct = ?,
                    lapses = ?,
                    difficulty = ?
                WHERE id = ?
                """, (new_tp, new_tc, new_interval, now, next_review_ts, new_streak, new_lapses, new_diff, prepared["id"]))
                conn.commit()
                in_txn = False

                updated_row = dict(prepared)
                updated_row.update({
                    "times_practiced": new_tp,
                    "times_correct": new_tc,
                    "interval_days": new_interval,
                    "last_practiced": now,
                    "next_review": next_review_ts,
                    "consecutive_correct": new_streak,
                    "lapses": new_lapses,
                    "difficulty": new_diff,
                })
                mastery = classify_move_mastery(updated_row)

            # Find opponent replies from tree, strictly prioritizing active study!
            #
            # TRAINER-04: the scoped predicate was `rs.name LIKE '%<study>%'` (unescaped,
            # unanchored), so a study named "Caro-Kann" also matched "Caro-Kann Advance" and the
            # unscoped fallback below then walked the user through a *different* study while
            # `next_study_name` still claimed the selected one. Scoping is now exact equality and
            # the *label* of the chosen continuation is the study the continuation really belongs
            # to, so the two fields can no longer disagree.
            after_fen = prepared.get("fen") or ""
            after_3 = " ".join(after_fen.split()[:3]) if after_fen else ""
            opp_replies = []
            study_scoped = False
            if active_study:
                clean_study = active_study.strip()
                cursor.execute("""
                SELECT DISTINCT rm.* FROM repertoire_moves rm
                LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 0
                  AND (rs.name = ? OR rm.study_name = ?)
                """, (repertoire, after_fen, after_3, f"{after_3} %", clean_study, clean_study))
                opp_replies = [dict(r) for r in cursor.fetchall()]
                study_scoped = True

            if not opp_replies:
                study_scoped = False
                cursor.execute("""
                SELECT * FROM repertoire_moves
                WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND is_user_move = 0
                """, (repertoire, after_fen, after_3, f"{after_3} %"))
                opp_replies = [dict(r) for r in cursor.fetchall()]

            opponent_reply = None
            next_expected_uci = None
            next_expected_san = None
            next_opening_name = None
            next_prep = None
            next_study = active_study

            # AUDIT FIXER-V / TRAINER-09 (owner ruling: "A line should never finish on an
            # opponent's move."): an opponent move the user has **no prepared reply** for is not a
            # line end, it is an unanswered branch. It is therefore *not a drill choice*: the
            # trainer never moves the board onto it, so the board can never come to rest on an
            # opponent's move with nothing to play. `is_line_end` below is derived from that
            # invariant (see `is_line_ended`), and `line_end_reason` states which of the two
            # honest reasons the line stopped. The "line ends after an opponent move" branch that
            # used to live here is gone.
            unanswered_branches = 0
            unanswered_branch_sans: List[str] = []
            if opp_replies:
                # Check which opponent replies have a prepared user follow-up move
                replies_with_followup = []
                for opp in opp_replies:
                    opp_fen = opp["fen"]
                    opp_fen_3 = " ".join(opp_fen.split()[:3])
                    prep_cand = None
                    if active_study:
                        clean_study = active_study.strip()
                        # AUDIT FIXER-W (CODE-3): this lookup had a bare `LIMIT 1` and **no
                        # `ORDER BY` at all**, and the `LEFT JOIN` to `study_moves` multiplies a row
                        # that belongs to several studies while the fuzzy 3-token/`LIKE` predicate
                        # also matches the other en-passant spelling of the same position. SQLite
                        # therefore answered with whichever row came first by rowid, and the
                        # trainer went on to serve `next_expected_uci` from — and to schedule the
                        # spaced-repetition counters of — a move belonging to a *different*
                        # position. Deterministic now, with exactly the tie-breaker the rest of
                        # this file uses: an exact 4-token parent wins over a 3-token/`LIKE`
                        # near-miss, then the most important move, then the lowest id.
                        cursor.execute("""
                        SELECT DISTINCT rm.* FROM repertoire_moves rm
                        LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                        LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                        WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 1
                          AND (rs.name = ? OR rm.study_name = ?)
                        ORDER BY (rm.parent_fen = ?) DESC, rm.is_important DESC, rm.id ASC
                        LIMIT 1
                        """, (repertoire, opp_fen, opp_fen_3, f"{opp_fen_3} %",
                               clean_study, clean_study, opp_fen))
                        prep_cand = cursor.fetchone()

                    if not prep_cand:
                        # Same tie-breaker as above. This arm already ordered by
                        # `is_important DESC, id ASC` but was still missing the
                        # exact-parent preference, so it could answer from the 3-token near-miss
                        # of a position whose stored `parent_fen` is the 4-token key.
                        cursor.execute("""
                        SELECT * FROM repertoire_moves
                        WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND is_user_move = 1
                        ORDER BY (parent_fen = ?) DESC, is_important DESC, id ASC
                        LIMIT 1
                        """, (repertoire, opp_fen, opp_fen_3, f"{opp_fen_3} %", opp_fen))
                        prep_cand = cursor.fetchone()

                    if prep_cand:
                        replies_with_followup.append((opp, dict(prep_cand)))
                    else:
                        unanswered_branches += 1
                        unanswered_branch_sans.append(opp.get("move_san") or opp.get("move_uci") or "?")

                if replies_with_followup:
                    def _branch_pedagogical_weight(opp_row: Dict[str, Any], user_followup: Dict[str, Any]) -> float:
                        """Tree-aware continuation weighting: steers opponent replies toward
                        unlearned, due, or high-mistake (leech) branches instead of repeating
                        already-mastered main lines purely by Lichess popularity."""
                        f_mem = infer_move_memory_state(user_followup, now=now)
                        f_mast = classify_move_mastery(user_followup)
                        f_next_rev = int(user_followup.get("next_review") or 0)
                        f_is_due = (f_mem["tp"] > 0 and f_next_rev > 0 and f_next_rev <= now)

                        if f_mem["is_weak"]:
                            ped_mult = 20.0 + f_mem["lapses"] * 5.0
                        elif f_mem["tc"] == 0:
                            ped_mult = 15.0
                        elif f_is_due:
                            ped_mult = 12.0
                        elif f_mast != "mastered":
                            ped_mult = 6.0
                        else:
                            ped_mult = 1.0 + 4.0 * (1.0 - f_mem["retrievability"])

                        # Look 1 user-move deeper in this branch so deeper gaps also pull steering
                        deep_fen = user_followup.get("fen") or ""
                        if deep_fen:
                            deep_3 = " ".join(deep_fen.split()[:3])
                            cursor.execute("""
                            SELECT u.* FROM repertoire_moves o
                            JOIN repertoire_moves u ON u.repertoire = o.repertoire
                              AND (u.parent_fen = o.fen OR u.parent_fen LIKE substr(o.fen, 1, length(o.fen) - 4) || '%')
                            WHERE o.repertoire = ? AND o.is_user_move = 0 AND u.is_user_move = 1
                              AND (o.parent_fen = ? OR o.parent_fen = ? OR o.parent_fen LIKE ?)
                            LIMIT 6
                            """, (repertoire, deep_fen, deep_3, f"{deep_3} %"))
                            for d_row in cursor.fetchall():
                                d_dict = dict(d_row)
                                d_mem = infer_move_memory_state(d_dict, now=now)
                                if d_mem["is_weak"]:
                                    ped_mult = max(ped_mult, 18.0 + d_mem["lapses"] * 4.0)
                                elif d_mem["tc"] == 0:
                                    ped_mult = max(ped_mult, 12.0)

                        pop_factor = math.sqrt(max(1.0, float(opp_row.get("popularity_pct") or 1.0)))
                        imp_bonus = 1.5 if user_followup.get("is_important") else 1.0
                        return max(0.5, pop_factor * ped_mult * imp_bonus)

                    weights = [_branch_pedagogical_weight(r[0], r[1]) for r in replies_with_followup]
                    chosen_pair = random.choices(replies_with_followup, weights=weights, k=1)[0]
                    opponent_reply = chosen_pair[0]
                    next_prep = chosen_pair[1]
                    next_expected_uci = next_prep["move_uci"]
                    next_expected_san = next_prep["move_san"]
                    # TRAINER-04: report the study the continuation REALLY belongs to, not the
                    # requested one, so `next_study_name` and `next_opening_name` cannot disagree.
                    next_study = ((next_prep.get("study_name") or "").strip()
                                  or active_study
                                  or next_prep.get("opening_name") or "")
                    next_opening_name = next_study or next_prep.get("opening_name") or ""
                # else: every opponent branch in the repertoire is unanswered. The line stops on
                # the user's OWN move (`opponent_reply` stays None) and `unanswered_branches`
                # reports how much of the position is still unplayable, so the client can say
                # "add a reply in the Builder" instead of celebrating a completed line.
        except Exception:
            # Never leave a write transaction open (SYNC-22 shape) if anything above raised.
            if in_txn:
                try:
                    conn.rollback()
                except Exception:
                    pass
                in_txn = False
            raise
        finally:
            if in_txn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            conn.close()

        # AUDIT FIXER-V / TRAINER-09 — the owner's ruling, applied through the one classifier
        # that defines it (`classify_line_position`).
        #
        #   A COMPLETED LINE is a position reached at the end of the user's own prepared move,
        #   where the repertoire genuinely has nothing more.
        #
        #   An OPPONENT BRANCH WITH NO REPLY is a position produced by an *opponent* move for which
        #   the user has prepared nothing. It is a gap, not a completion.
        #
        # The selection above already refuses to move onto such a position, so the classifier's
        # `AWAITING_REPLY` case should be unreachable; it is handled anyway, because the guarantee
        # the owner asked for is that such a position is *never reported* as a line end, and that
        # guarantee must not depend on the weighting loop staying correct.
        line_position = classify_line_position(
            reached_by_user_move=(opponent_reply is None),
            has_prepared_reply=bool(next_expected_uci),
        )
        if line_position == LINE_POSITION_AWAITING_REPLY:
            is_line_ended = False
            line_end_reason = None
            unanswered_branches = max(1, unanswered_branches)
            opponent_reply = None
            next_prep = None
            next_expected_uci = None
            next_expected_san = None
        elif line_position == LINE_POSITION_COMPLETED:
            is_line_ended = True
            line_end_reason = (LINE_END_UNANSWERED_BRANCHES if unanswered_branches
                               else LINE_END_REPERTOIRE_EXHAUSTED)
        else:
            is_line_ended = False
            line_end_reason = None

        return {
            "correct": True,
            "is_retry": bool(is_retry),
            "prepared_id": prepared["id"],
            "message": f"Retried move: {expected_san} (will be reinforced later)." if is_retry else f"Correct! {expected_san} is your prepared move.",
            "your_san": expected_san,
            "your_uci": expected_uci,
            "after_fen": after_fen,
            "study_name": active_study,
            "study_scoped": study_scoped,
            "mastery_status": mastery,
            "times_practiced": new_tp,
            "times_correct": new_tc,
            "is_important": bool(prepared.get("is_important")),
            "comment": prepared.get("comment", "") or "",
            "arrows": prepared.get("arrows", "") or "",
            "opponent_reply": {
                "san": opponent_reply["move_san"],
                "uci": opponent_reply["move_uci"],
                "fen": opponent_reply["fen"]
            } if opponent_reply else None,
            "next_expected_uci": next_expected_uci,
            "next_expected_san": next_expected_san,
            "next_opening_name": next_opening_name,
            "next_study_name": next_study,
            "next_is_important": bool(next_prep.get("is_important")) if next_prep else False,
            "next_comment": (next_prep.get("comment", "") or "") if next_prep else "",
            "next_arrows": (next_prep.get("arrows", "") or "") if next_prep else "",
            "is_line_end": is_line_ended,
            # FIXER-V: the classification that produced `is_line_end`, plus the reason the line
            # stopped. "repertoire_exhausted" = the user's own prepared move is genuinely the last
            # thing in the repertoire (a real completion); "unanswered_opponent_branches" = the
            # repertoire holds N opponent replies the user has prepared no answer to, so the line
            # stops because of a *gap*, not because the line is finished. `null` while it continues.
            "line_position": line_position,
            "line_end_reason": line_end_reason,
            "unanswered_branches": unanswered_branches,
            "unanswered_branch_sans": unanswered_branch_sans[:8],
        }

    @staticmethod
    def _detect_pgn_repertoire_color(games: List[Any]) -> str:
        """Determines whether a collection of PGN games represents a White or Black repertoire.

        Heuristic:
        1. Checks header tags (Event, White, Black).
        2. Branching factor analysis: In a White repertoire, White plays a single prepared move
           while Black branches into multiple opponent responses. In a Black repertoire,
           White branches into multiple opening moves while Black plays single replies.
        """
        for game in games:
            event = (game.headers.get("Event") or "").lower()
            if "white" in event:
                return "white"
            elif "black" in event:
                return "black"
            white_player = (game.headers.get("White") or "").lower()
            black_player = (game.headers.get("Black") or "").lower()
            if any(k in white_player for k in ("repertoire", "user", "my")):
                return "white"
            if any(k in black_player for k in ("repertoire", "user", "my")):
                return "black"

        white_branch_points = 0
        black_branch_points = 0
        for game in games:
            def check_branches(node, board):
                nonlocal white_branch_points, black_branch_points
                if len(node.variations) > 1:
                    if board.turn == chess.WHITE:
                        white_branch_points += 1
                    else:
                        black_branch_points += 1
                for child in node.variations:
                    next_b = board.copy()
                    next_b.push(child.move)
                    check_branches(child, next_b)
            check_branches(game, game.board())

        if black_branch_points > white_branch_points:
            return "white"
        elif white_branch_points > black_branch_points:
            return "black"
        return "white"

    def preview_pgn_repertoire(self, pgn_text: str) -> Dict[str, Any]:
        """Parses a PGN string and extracts preview metadata without modifying the database."""
        if not pgn_text or not pgn_text.strip():
            raise ValueError("No PGN content provided.")

        pgn_io = io.StringIO(pgn_text)
        games = []
        while True:
            try:
                game = chess.pgn.read_game(pgn_io)
            except Exception:
                break
            if game is None:
                break
            games.append(game)
            if len(games) >= 200:
                break

        if not games:
            raise ValueError("Could not parse any chess games or variations from the provided PGN.")

        detected_color = self._detect_pgn_repertoire_color(games)

        total_plies = 0
        user_plies = 0
        opp_plies = 0
        max_depth = 0
        chapters = []
        first_moves = []

        for g_idx, game in enumerate(games):
            title = game.headers.get("Event") or game.headers.get("White") or f"Chapter {g_idx + 1}"
            g_moves = 0

            def walk(node, board, depth=1):
                nonlocal total_plies, user_plies, opp_plies, max_depth, g_moves
                for child in node.variations:
                    total_plies += 1
                    g_moves += 1
                    if depth > max_depth:
                        max_depth = depth
                    is_user = (board.turn == chess.WHITE) if detected_color == "white" else (board.turn == chess.BLACK)
                    if is_user:
                        user_plies += 1
                    else:
                        opp_plies += 1
                    if len(first_moves) < 6 and depth <= 3:
                        san = board.san(child.move)
                        prefix = f"{(depth + 1) // 2}." if board.turn == chess.WHITE else f"{(depth + 1) // 2}..."
                        first_moves.append(f"{prefix} {san}")
                    next_b = board.copy()
                    next_b.push(child.move)
                    walk(child, next_b, depth + 1)

            walk(game, game.board())
            chapters.append({
                "title": title,
                "moves_count": g_moves,
                "eco": game.headers.get("ECO", ""),
            })

        if total_plies == 0:
            raise ValueError("Could not parse any chess moves or variations from the provided PGN.")

        return {
            "status": "success",
            "detected_color": detected_color,
            "game_count": len(games),
            "total_moves": total_plies,
            "user_moves": user_plies,
            "opponent_moves": opp_plies,
            "max_depth": max_depth,
            "chapters": chapters,
            "first_moves": first_moves[:6],
        }

    def import_pgn_repertoire(
        self,
        pgn_text: str,
        repertoire: str = "auto",
        study_name: Optional[str] = None,
        split_chapters_as_studies: bool = False,
        replace_conflicts: bool = True,
        clean_repertoire: bool = False,
    ) -> Dict[str, Any]:
        """Imports an entire PGN opening repertoire tree into the database.

        Supports:
        - Deep recursive variation trees (RAV) from Chessbook, Lichess studies, or Chessbase.
        - Multi-game chapters optionally mapped to named studies.
        - Color auto-detection or explicit White/Black target.
        - Preserving mainline user move priority over secondary branches in the PGN.
        - Conflict resolution: replacing existing DB moves vs keeping existing preparation.
        - Clean replacement mode (wiping selected color first).
        """
        if not pgn_text or not pgn_text.strip():
            raise ValueError("No PGN content provided.")

        pgn_io = io.StringIO(pgn_text)
        games = []
        while True:
            try:
                game = chess.pgn.read_game(pgn_io)
            except Exception:
                break
            if game is None:
                break
            games.append(game)
            if len(games) >= 200:
                break

        if not games or not any(g.variations for g in games):
            raise ValueError("Could not parse any chess moves or variations from the provided PGN.")

        rep_clean = (repertoire or "auto").lower().strip()
        if rep_clean == "auto":
            rep_clean = self._detect_pgn_repertoire_color(games)
        if rep_clean not in ("white", "black"):
            raise ValueError(f"Invalid repertoire color: {rep_clean}. Must be 'white' or 'black'.")

        # Clean wipe if requested
        if clean_repertoire:
            self.clear_repertoire_database(rep_clean)

        imported_count = 0
        user_count = 0
        opp_count = 0
        studies_created = set()
        conflicts = []

        # Track positions that received a user move during THIS import session to ensure
        # that mainline user moves are not overwritten by later side-variations in the same PGN
        session_prepared_user_positions = set()

        for g_idx, game in enumerate(games):
            current_study = (study_name or "").strip()
            if split_chapters_as_studies and len(games) > 1:
                chapter_title = (game.headers.get("Event") or "").strip()
                if not chapter_title or chapter_title.lower() in ("white repertoire", "black repertoire", "?", "n/a", "none"):
                    raw_player = game.headers.get("White") if rep_clean == "white" else game.headers.get("Black")
                    chapter_title = (raw_player or "").strip()
                if not chapter_title or chapter_title.lower() in ("white repertoire", "black repertoire", "?", "n/a", "none"):
                    chapter_title = f"Chapter {g_idx + 1}"
                current_study = chapter_title
            elif not current_study:
                evt = (game.headers.get("Event") or "").strip()
                if evt and evt.lower() not in ("white repertoire", "black repertoire", "?", "n/a", "none"):
                    current_study = evt
                else:
                    site = (game.headers.get("Site") or "").lower()
                    current_study = f"Chessbook {rep_clean.capitalize()}" if "chessbook" in site else f"{rep_clean.capitalize()} Repertoire"

            if current_study:
                self.create_study(current_study, rep_clean)
                studies_created.add(current_study)

            def walk_and_import(node, board):
                nonlocal imported_count, user_count, opp_count
                for child in node.variations:
                    p_fen = normalize_fen(board.fen())
                    m = child.move
                    uci = m.uci()
                    is_user = (board.turn == chess.WHITE) if rep_clean == "white" else (board.turn == chess.BLACK)

                    if is_user:
                        p_fen_3 = " ".join(p_fen.split()[:3])
                        if p_fen_3 in session_prepared_user_positions:
                            conflicts.append({
                                "parent_fen": p_fen,
                                "uci": uci,
                                "san": child.san(),
                                "reason": "secondary_variation_in_pgn",
                            })
                            continue
                        session_prepared_user_positions.add(p_fen_3)

                    comment = (child.comment or "").strip()
                    is_important = bool(any(n in (1, 3) for n in child.nags))

                    res = self.add_repertoire_move(
                        repertoire=rep_clean,
                        parent_fen=p_fen,
                        move_uci=uci,
                        is_user_move=is_user,
                        study_name=current_study or None,
                        comment=comment,
                        is_important=is_important,
                        replace_existing=replace_conflicts,
                    )

                    status = res.get("status")
                    if status in ("success", "patched"):
                        imported_count += 1
                        if is_user:
                            user_count += 1
                        else:
                            opp_count += 1
                    elif status == "conflict":
                        conflicts.append({
                            "parent_fen": p_fen,
                            "uci": uci,
                            "san": child.san(),
                            "reason": "existing_user_move_in_db",
                        })

                    next_b = board.copy()
                    next_b.push(m)
                    walk_and_import(child, next_b)

            walk_and_import(game, game.board())

        self._invalidate_overlap_index()

        return {
            "status": "success",
            "repertoire": rep_clean,
            "imported_moves": imported_count,
            "user_moves": user_count,
            "opponent_moves": opp_count,
            "studies_created": sorted(studies_created),
            "conflicts_count": len(conflicts),
            "message": f"Successfully imported {imported_count} moves ({user_count} user moves, {opp_count} opponent replies) into {rep_clean.capitalize()} repertoire.",
        }

