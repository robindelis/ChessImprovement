"""
backend/deviations.py — Repertoire Deviation Analysis Engine

Detects when and where the user played a move other than their prepared
repertoire move in real games. Unlike tactical blunder analysis, this is
a pure move-tree comparison: 0 Stockfish engine time, 100% deterministic,
fast, and offline.
"""
import json
import re
import time
import chess
from typing import Any, Dict, List, Optional, Set, Tuple
from backend.db import get_db, get_setting, set_setting
from backend.repertoire import normalize_fen

RESOLVED_DEVIATIONS_SETTING_KEY = "resolved_deviations"


def get_resolved_deviations() -> Dict[str, Dict[str, Any]]:
    """Retrieves all remembered resolved deviations from persistent storage."""
    raw = get_setting(RESOLVED_DEVIATIONS_SETTING_KEY, "{}")
    try:
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def mark_deviation_resolved(
    game_id: Optional[str] = None,
    fen_before: str = "",
    played_uci: str = "",
    prepared_uci: str = "",
    user_color: str = "white"
) -> bool:
    """Marks a game deviation as corrected on first try and remembers it permanently."""
    norm_fen = normalize_fen(fen_before) if fen_before else ""
    g_id = str(game_id).strip() if game_id else None

    if not g_id and not (norm_fen and played_uci):
        return False

    key = g_id if g_id else f"{norm_fen}_{played_uci}"
    resolved = get_resolved_deviations()
    resolved[key] = {
        "game_id": g_id,
        "fen": norm_fen,
        "played_uci": (played_uci or "").strip().lower(),
        "prepared_uci": (prepared_uci or "").strip().lower(),
        "user_color": (user_color or "white").lower(),
        "resolved_at": int(time.time())
    }
    set_setting(RESOLVED_DEVIATIONS_SETTING_KEY, json.dumps(resolved))
    return True


def reset_resolved_deviations() -> None:
    """Resets all resolved deviations history."""
    set_setting(RESOLVED_DEVIATIONS_SETTING_KEY, "{}")


def _clean_san(token: str) -> str:
    """Strip annotations like '?!', '?', '!', '+', '#' for SAN parsing."""
    return re.sub(r"[?!#+]", "", token).strip()


def find_game_deviations(
    color: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    include_resolved: bool = False
) -> Dict[str, Any]:
    """Analyzes stored user games against repertoire_moves to identify positions
    where the user deviated from their prepared repertoire lines.

    Returns:
        total_games_analyzed: int
        total_deviations: int
        compliance_pct: float
        deviations: List[Dict[str, Any]]
    """
    if color and color.lower() in ("white", "black"):
        color = color.lower()
    else:
        color = None

    # 0. Load resolved deviations
    resolved = get_resolved_deviations()
    resolved_game_ids: Set[str] = {str(r["game_id"]) for r in resolved.values() if r.get("game_id")}
    resolved_fen_moves: Set[Tuple[str, str]] = {
        (r["fen"], r["played_uci"])
        for r in resolved.values()
        if r.get("fen") and r.get("played_uci")
    }

    conn = get_db()
    cursor = conn.cursor()

    # 1. Load active user repertoire moves & opponent branches
    rep_query = """
    SELECT repertoire, parent_fen, move_uci, move_san, is_user_move,
           opening_name, eco, comment, arrows
    FROM repertoire_moves
    """
    params: List[Any] = []
    if color:
        rep_query += " WHERE repertoire = ?"
        params.append(color)

    cursor.execute(rep_query, params)
    rep_rows = cursor.fetchall()

    # Map: (repertoire, 4-token parent_fen) -> user move dict
    # And 3-token fallback for transpositions
    user_moves: Dict[Tuple[str, str], Dict[str, Any]] = {}
    user_moves_3: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # Set of opponent branch UCIs: (repertoire, 4-token parent_fen) -> set(uci)
    opp_moves: Dict[Tuple[str, str], Set[str]] = {}
    opp_moves_3: Dict[Tuple[str, str], Set[str]] = {}

    for row in rep_rows:
        r_color = row["repertoire"]
        p_fen = normalize_fen(row["parent_fen"])
        p_fen_3 = " ".join(p_fen.split()[:3])
        m_uci = row["move_uci"]
        is_user = bool(row["is_user_move"])

        if is_user:
            user_moves[(r_color, p_fen)] = dict(row)
            if (r_color, p_fen_3) not in user_moves_3:
                user_moves_3[(r_color, p_fen_3)] = dict(row)
        else:
            opp_key = (r_color, p_fen)
            if opp_key not in opp_moves:
                opp_moves[opp_key] = set()
            opp_moves[opp_key].add(m_uci)

            opp_key_3 = (r_color, p_fen_3)
            if opp_key_3 not in opp_moves_3:
                opp_moves_3[opp_key_3] = set()
            opp_moves_3[opp_key_3].add(m_uci)

    # 2. Query games
    games_query = "SELECT * FROM games"
    game_conditions = []
    game_params: List[Any] = []

    if color:
        game_conditions.append("user_color = ?")
        game_params.append(color)

    if search and search.strip():
        q = f"%{search.strip()}%"
        game_conditions.append(
            "(white_player LIKE ? OR black_player LIKE ? OR opening_name LIKE ? OR eco LIKE ?)"
        )
        game_params.extend([q, q, q, q])

    if game_conditions:
        games_query += " WHERE " + " AND ".join(game_conditions)

    games_query += " ORDER BY played_at DESC"

    cursor.execute(games_query, game_params)
    game_rows = cursor.fetchall()
    conn.close()

    total_games = len(game_rows)
    deviations: List[Dict[str, Any]] = []
    games_with_repertoire_prep = 0
    games_followed_repertoire = 0

    for g in game_rows:
        game = dict(g)
        user_col = (game.get("user_color") or "white").lower()
        if color and user_col != color:
            continue

        moves_san_str = game.get("moves_san") or ""
        if not moves_san_str:
            continue

        tokens = [
            m for m in moves_san_str.strip().split()
            if m and not m.endswith(".") and not m[0].isdigit()
        ]
        if not tokens:
            continue

        board = chess.Board()
        game_has_prep = False
        game_deviated = False

        opp_name = game.get("black_player") if user_col == "white" else game.get("white_player")
        opp_rating = game.get("black_rating") if user_col == "white" else game.get("white_rating")
        prev_move_uci = None
        prev_move_san = None

        for ply, san in enumerate(tokens):
            clean = _clean_san(san)
            if not clean:
                break

            fen_before_norm = normalize_fen(board.fen())
            fen_before_3 = " ".join(fen_before_norm.split()[:3])
            fen_before_full = board.fen()

            is_white = (ply % 2 == 0)
            is_user_turn = (is_white and user_col == "white") or (not is_white and user_col == "black")

            try:
                move = board.parse_san(clean)
            except Exception:
                try:
                    move = chess.Move.from_uci(clean)
                    if move not in board.legal_moves:
                        break
                except Exception:
                    break

            if is_user_turn:
                prep = (
                    user_moves.get((user_col, fen_before_norm))
                    or user_moves_3.get((user_col, fen_before_3))
                )
                if prep:
                    game_has_prep = True
                    prep_uci = prep.get("move_uci", "").strip().lower()
                    played_uci = move.uci().strip().lower()

                    if played_uci != prep_uci:
                        g_id_str = str(game.get("id")) if game.get("id") else None
                        is_resolved = (
                            (g_id_str and g_id_str in resolved_game_ids)
                            or ((fen_before_norm, played_uci) in resolved_fen_moves)
                        )
                        if is_resolved and not include_resolved:
                            # User corrected themselves on this deviation on first try!
                            # Exclude from active deviations and do not propose again.
                            game_deviated = False
                            break

                        # User departed from prepared repertoire!
                        game_deviated = True
                        last_move_obj = None
                        if ply > 0 and prev_move_uci and len(prev_move_uci) >= 4:
                            last_move_obj = {
                                "from": prev_move_uci[:2],
                                "to": prev_move_uci[2:4],
                                "san": prev_move_san or ""
                            }

                        path_parts = []
                        for p_idx in range(ply):
                            if p_idx % 2 == 0:
                                path_parts.append(f"{(p_idx // 2) + 1}. {tokens[p_idx]}")
                            else:
                                path_parts.append(tokens[p_idx])
                        path_formatted = " ".join(path_parts) if path_parts else "Starting position"

                        deviations.append({
                            "game_id": game.get("id"),
                            "played_at": game.get("played_at", 0),
                            "speed": game.get("speed", "rapid"),
                            "user_color": user_col,
                            "opponent_name": opp_name or "Opponent",
                            "opponent_rating": opp_rating or 0,
                            "result": game.get("result"),
                            "move_number": (ply // 2) + 1,
                            "deviation_move_number": (ply // 2) + 1,
                            "ply": ply + 1,
                            "fen_before": fen_before_full,
                            "played_san": san,
                            "played_uci": played_uci,
                            "prepared_san": prep.get("move_san", ""),
                            "prepared_uci": prep_uci,
                            "prepared_comment": prep.get("comment", "") or "",
                            "prepared_arrows": prep.get("arrows", "") or "",
                            "opening_name": prep.get("opening_name") or game.get("opening_name") or "Repertoire Line",
                            "eco": prep.get("eco") or game.get("eco") or "",
                            "game_url": f"https://lichess.org/{game.get('id')}" if game.get("id") else "",
                            "last_move": last_move_obj,
                            "path_formatted": path_formatted,
                            "moves_leading": tokens[:ply],
                            "is_resolved": bool(is_resolved)
                        })
                        break  # Stop checking this game on the first deviation
                    else:
                        # User followed preparation at this step
                        prev_move_uci = move.uci()
                        prev_move_san = san
                        board.push(move)
                else:
                    # End of user's preparation reached
                    break
            else:
                # Opponent's turn: check if opponent stayed in prepared tree
                opp_set = opp_moves.get((user_col, fen_before_norm)) or opp_moves_3.get((user_col, fen_before_3))
                prev_move_uci = move.uci()
                prev_move_san = san
                board.push(move)
                if opp_set and move.uci() not in opp_set:
                    # Opponent left the repertoire first; game is off-book due to opponent
                    break

        if game_has_prep:
            games_with_repertoire_prep += 1
            if not game_deviated:
                games_followed_repertoire += 1

    compliance_pct = (
        round((games_followed_repertoire / games_with_repertoire_prep) * 100, 1)
        if games_with_repertoire_prep > 0 else 100.0
    )

    total_deviations = len(deviations)
    paginated_deviations = deviations[offset: offset + limit]

    return {
        "color": color,
        "total_games": total_games,
        "total_games_analyzed": total_games,
        "games_with_prep": games_with_repertoire_prep,
        "games_followed_repertoire": games_followed_repertoire,
        "games_with_deviations": total_deviations,
        "total_deviations": total_deviations,
        "total_resolved": len(resolved),
        "compliance_pct": compliance_pct,
        "limit": limit,
        "offset": offset,
        "deviations": paginated_deviations
    }


def get_deviation_drill(
    color: str = "white",
    game_id: Optional[str] = None,
    recent_ids: Optional[List[str]] = None,
    index: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Returns a deviation formatted as an interactive drill for RepertoireTrainer."""
    res = find_game_deviations(color=color, limit=500)
    devs = res.get("deviations", [])
    if not devs:
        return None

    target = None
    target_idx = 0

    if game_id:
        for i, d in enumerate(devs):
            if d.get("game_id") == game_id:
                target = d
                target_idx = i
                break

    if target is None and index is not None:
        target_idx = max(0, min(int(index), len(devs) - 1))
        target = devs[target_idx]

    if target is None and recent_ids:
        recent_set = set(str(r) for r in recent_ids)
        for i, d in enumerate(devs):
            if str(d.get("game_id")) not in recent_set:
                target = d
                target_idx = i
                break

    if target is None:
        target = devs[0]
        target_idx = 0

    user_col = target.get("user_color") or ("black" if (target.get("fen_before", "").split()[1:2] == ["b"]) else "white")
    move_history_line = f"vs {target.get('opponent_name', 'Opponent')} ({target.get('speed', 'game')})"
    return {
        "repertoire": user_col,
        "user_color": user_col,
        "parent_fen": target["fen_before"],
        "expected_uci": target["prepared_uci"],
        "expected_san": target["prepared_san"],
        "played_san": target["played_san"],
        "played_uci": target["played_uci"],
        "opening_name": target["opening_name"],
        "eco": target["eco"],
        "is_important": True,
        "comment": target.get("prepared_comment") or f"In your game vs {target.get('opponent_name')}, you played {target.get('played_san')}. Your prepared move is {target.get('prepared_san')}.",
        "arrows": target.get("prepared_arrows") or "",
        "reason": "session_lapse",
        "curriculum_mode": "deviations",
        "drill_reason": "deviations",
        "pedagogical_reason": f"⚠️ Real Game Deviation (Move {target.get('deviation_move_number')})",
        "move_history_sans": target.get("moves_leading") or [target["played_san"]],
        "move_history_line": move_history_line,
        "path_formatted": target.get("path_formatted") or "Starting position",
        "last_move": target.get("last_move"),
        "game_id": target.get("game_id"),
        "game_url": target.get("game_url") or (f"https://lichess.org/{target.get('game_id')}" if target.get("game_id") else ""),
        "opponent_name": target.get("opponent_name"),
        "opponent_rating": target.get("opponent_rating"),
        "speed": target.get("speed"),
        "result": target.get("result"),
        "played_at": target.get("played_at"),
        "deviation_move_number": target.get("deviation_move_number"),
        "ply_depth": target.get("ply"),
        "deviation_index": target_idx + 1,
        "total_deviations": len(devs)
    }

