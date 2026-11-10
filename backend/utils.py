"""Shared pure helpers for FEN normalization, Lichess game outcomes, and data coercion."""
import math
from typing import Any, Optional, Tuple
import chess

MS_EPOCH_THRESHOLD = 100_000_000_000
MAX_PLAYED_AT_MS = 2 ** 53
MAX_EXPLORER_COUNT = 1_000_000_000
EXPLORER_NEGATIVE_TTL_SECONDS = 60.0

RESULT_WHITE = "white"
RESULT_BLACK = "black"
RESULT_DRAW = "draw"
RESULT_ABANDONED = "abandoned"

UNFINISHED_STATUSES = frozenset({"abandoned", "unfinished", "timeout", "no_start"})


def normalize_fen(fen: str) -> str:
    """Normalize FEN to placement, turn, castling rights, and en passant square (first 4 tokens)."""
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


def normalize_fen_3(fen: str) -> str:
    """3-token logical position key (placement + side to move + castling rights)."""
    norm = normalize_fen(fen)
    return " ".join(norm.split()[:3]) if norm else ""


def normalize_played_at(value: Any, default: int = 0) -> int:
    """Total normalization of a Lichess timestamp to epoch MILLISECONDS."""
    try:
        if isinstance(value, bool):
            raise TypeError("bool is not a timestamp")
        number = float(value)
    except (TypeError, ValueError):
        return int(default)
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
    """Coerce an untrusted count to a bounded, storable `int`."""
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return default
    if number > MAX_EXPLORER_COUNT:
        return MAX_EXPLORER_COUNT
    return int(number)


def coerce_text(value: Any, default: str = "", max_len: int = 4096) -> str:
    """Coerce an untrusted field to a bounded `str`."""
    if value is None:
        return default
    if not isinstance(value, str):
        if isinstance(value, (dict, list, tuple, set, bool)):
            return default
        value = str(value)
    return value[:max_len] if len(value) > max_len else value


def number_is_unusable(value: Any) -> bool:
    """True when `value` is present but is not a finite, non-negative number."""
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(number) or number < 0


def classify_stored_result(stored: Any, user_color: str) -> str:
    """Map a stored `games.result` onto `white` / `black` / `draw` / `abandoned`."""
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
    """Canonical `games.result` token for an upstream game record, or `None` when invalid."""
    raw = winner.strip().lower() if isinstance(winner, str) else ("" if winner is None else str(winner).strip().lower())
    st = status.strip().lower() if isinstance(status, str) else ""
    if st in UNFINISHED_STATUSES:
        return RESULT_ABANDONED
    if raw in (RESULT_WHITE, RESULT_BLACK):
        return raw
    if raw in ("draw", "1/2-1/2", "½-½", "0.5", ""):
        return RESULT_DRAW
    return None


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
