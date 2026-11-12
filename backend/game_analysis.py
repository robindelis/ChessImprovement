"""
Full-game Stockfish 19 analysis, move classification, opening-trap coaching,
and guided repertoire sequence builder.
"""
import asyncio
import json
import math
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple

import chess
import chess.engine

from backend.analyzer import GameAnalyzer, STOCKFISH_PATH, centipawn_loss, normalize_score
from backend.db import closing_db, get_db, get_setting
from backend.evaluator import (
    MATE_SCORE_CP,
    PV_MAX_PLIES,
    EngineBusy,
    EngineUnavailable,
    format_sse,
    get_sync_engine_pool,
    pv_line,
    sanitize_error,
    sanitize_pv,
)
from backend.openings import OpeningBook
from backend.repertoire import RepertoireManager
from backend.utils import normalize_fen

DEFAULT_ANALYSIS_DEPTH = 19
MIN_ANALYSIS_DEPTH = 10
MAX_ANALYSIS_DEPTH = 26
CRITICAL_MULTIPV = 3
OPENING_TRAP_MAX_PLY = 36


def cp_to_win_pct(cp: float, mate: Optional[int] = None) -> float:
    """Lichess win-probability curve (White POV, 0..100)."""
    if mate is not None:
        if mate > 0:
            return 100.0
        if mate < 0:
            return 0.0
    try:
        val = max(-MATE_SCORE_CP, min(MATE_SCORE_CP, float(cp)))
    except (TypeError, ValueError):
        return 50.0
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368208 * val)) - 1.0)


def move_accuracy_pct(win_before: float, win_after: float) -> float:
    """Lichess single-move accuracy percentage from side-to-move win% delta."""
    drop = max(0.0, float(win_before) - float(win_after))
    raw = 103.1668 * math.exp(-0.04354 * drop) - 3.1669
    return max(0.0, min(100.0, raw))


def compute_game_accuracy(move_accuracies: List[float]) -> Optional[float]:
    """Aggregate move accuracies using harmonic-arithmetic blend."""
    valid = [max(1.0, min(100.0, float(a))) for a in move_accuracies if a is not None]
    if not valid:
        return None
    arith = sum(valid) / len(valid)
    harm = len(valid) / sum(1.0 / a for a in valid)
    return round((arith + harm) / 2.0, 1)


def classify_ply_move(
    *,
    ply: int,
    is_book: bool,
    played_uci: str,
    best_uci: str,
    win_loss: float,
    cpl: int,
    mate_before: Optional[int],
    mate_after: Optional[int],
) -> str:
    """Classify a single move using win-probability drop + centipawn/mate guards."""
    if is_book and ply <= 24 and win_loss < 4.5 and cpl < 50:
        return "book"
    if best_uci and played_uci == best_uci:
        return "best"
    if mate_before is not None and mate_before > 0 and (mate_after is None or mate_after <= 0):
        return "blunder" if win_loss >= 15.0 else "mistake"
    if mate_before is None and mate_after is not None and mate_after < 0 and win_loss >= 12.0:
        return "blunder"
    if win_loss >= 20.0 or cpl >= 220:
        return "blunder"
    if win_loss >= 10.0 or cpl >= 120:
        return "mistake"
    if win_loss >= 5.0 or cpl >= 60:
        return "inaccuracy"
    if win_loss <= 1.5 and cpl <= 15:
        return "best"
    return "good"


def _load_repertoire_lookup(color: str) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Set[str]]]:
    """Load user moves and opponent branches indexed by 4-token and 3-token parent FEN."""
    user_by_fen: Dict[str, Dict[str, Any]] = {}
    opp_by_fen: Dict[str, Set[str]] = {}
    with closing_db() as conn:
        rows = conn.execute(
            """
            SELECT parent_fen, fen, move_uci, move_san, is_user_move, study_name, comment, is_important
            FROM repertoire_moves
            WHERE repertoire = ?
            ORDER BY id ASC
            """,
            (color,),
        ).fetchall()
    for r in rows:
        p4 = normalize_fen(r["parent_fen"])
        p3 = " ".join(p4.split()[:3])
        uci = (r["move_uci"] or "").strip().lower()
        if bool(r["is_user_move"]):
            d = dict(r)
            user_by_fen.setdefault(p4, d)
            user_by_fen.setdefault(p3, d)
        else:
            opp_by_fen.setdefault(p4, set()).add(uci)
            opp_by_fen.setdefault(p3, set()).add(uci)
    return user_by_fen, opp_by_fen


def _lookup_cached_explorer_stats(fen: str) -> Dict[str, Dict[str, Any]]:
    """Fast local lookup from `explorer_cache` (never hits network during engine batch)."""
    norm = normalize_fen(fen)
    out: Dict[str, Dict[str, Any]] = {}
    with closing_db() as conn:
        row = conn.execute(
            "SELECT data_json FROM explorer_cache WHERE fen = ? ORDER BY updated_at DESC LIMIT 1",
            (norm,),
        ).fetchone()
    if not row or not row["data_json"]:
        return out
    try:
        payload = json.loads(row["data_json"])
    except Exception:
        return out
    moves = payload.get("moves") if isinstance(payload, dict) else None
    if not isinstance(moves, list):
        return out
    total_games = sum(max(0, int(m.get("white", 0)) + int(m.get("draws", 0)) + int(m.get("black", 0))) for m in moves if isinstance(m, dict))
    for m in moves:
        if not isinstance(m, dict):
            continue
        uci = str(m.get("uci") or "").strip().lower()
        if not uci:
            continue
        w = max(0, int(m.get("white", 0)))
        d = max(0, int(m.get("draws", 0)))
        b = max(0, int(m.get("black", 0)))
        g = w + d + b
        pop = round((g / total_games) * 100.0, 1) if total_games > 0 else 0.0
        out[uci] = {
            "games": g,
            "popularity_pct": pop,
            "white_pct": round((w / g) * 100.0, 1) if g > 0 else 0.0,
            "draw_pct": round((d / g) * 100.0, 1) if g > 0 else 0.0,
            "black_pct": round((b / g) * 100.0, 1) if g > 0 else 0.0,
        }
    return out


def _parse_san_tokens(moves_san: str) -> List[str]:
    if not moves_san:
        return []
    out: List[str] = []
    for raw in moves_san.strip().split():
        tok = raw.strip()
        if not tok or tok.endswith(".") or tok[0].isdigit():
            continue
        if tok in ("1-0", "0-1", "1/2-1/2", "*"):
            continue
        out.append(tok)
    return out


def _extract_multipv_candidates(
    engine: Any,
    board: chess.Board,
    depth: int,
    user_color: str,
    multipv: int = CRITICAL_MULTIPV,
) -> List[Dict[str, Any]]:
    """Run synchronous MultiPV Stockfish search and enrich with cached 2000+ human stats."""
    if board.is_game_over():
        return []
    limit = chess.engine.Limit(depth=depth)
    try:
        raw = engine.analyse(board, limit, multipv=multipv)
    except TypeError:
        raw = engine.analyse(board, limit)

    infos = raw if isinstance(raw, list) else [raw]
    exp_stats = _lookup_cached_explorer_stats(board.fen())
    user_bool = chess.WHITE if user_color == "white" else chess.BLACK

    candidates: List[Dict[str, Any]] = []
    seen_uci: Set[str] = set()
    for rank_idx, info in enumerate(infos, 1):
        if not isinstance(info, dict) or "score" not in info:
            continue
        pv_moves = info.get("pv") or []
        if not pv_moves:
            continue
        first_move = pv_moves[0]
        if first_move not in board.legal_moves:
            continue
        uci = first_move.uci()
        if uci in seen_uci:
            continue
        seen_uci.add(uci)
        san = board.san(first_move)
        white_cp, white_mate = normalize_score(info["score"], chess.WHITE)
        user_cp, user_mate = normalize_score(info["score"], user_bool)
        line = sanitize_pv(pv_line(board, pv_moves, PV_MAX_PLIES))
        human = exp_stats.get(uci, {})
        win_rate = (
            human.get("white_pct", 0.0) + 0.5 * human.get("draw_pct", 0.0)
            if user_color == "white"
            else human.get("black_pct", 0.0) + 0.5 * human.get("draw_pct", 0.0)
        )
        candidates.append({
            "rank": rank_idx,
            "uci": uci,
            "san": san,
            "score_cp": white_cp,
            "user_cp": user_cp,
            "mate": white_mate,
            "user_mate": user_mate,
            "pv": line,
            "human_games": human.get("games", 0),
            "popularity_pct": human.get("popularity_pct", 0.0),
            "human_score_pct": round(win_rate, 1) if human else None,
        })
    return candidates


def analyze_full_game_sync(
    game_id: str,
    depth: Optional[int] = None,
    force: bool = False,
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """Full-game Stockfish 19 pass with MultiPV-3 trap/mistake coaching."""
    target_depth = max(
        MIN_ANALYSIS_DEPTH,
        min(MAX_ANALYSIS_DEPTH, int(depth or get_setting("analysis_depth", str(DEFAULT_ANALYSIS_DEPTH)) or DEFAULT_ANALYSIS_DEPTH)),
    )

    with closing_db() as conn:
        row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not row:
        raise ValueError(f"Game '{game_id}' not found.")

    game = dict(row)
    if not force and game.get("analysis_json") and int(game.get("analysis_depth") or 0) >= target_depth:
        try:
            cached_payload = json.loads(game["analysis_json"])
            if isinstance(cached_payload, dict) and cached_payload.get("plies") is not None:
                cached_payload["cached"] = True
                return cached_payload
        except Exception:
            pass

    user_color = "black" if (game.get("user_color") or "").lower() == "black" else "white"
    tokens = _parse_san_tokens(game.get("moves_san") or "")
    opening_book = OpeningBook.get_instance()
    user_rep, opp_rep = _load_repertoire_lookup(user_color)

    board = chess.Board()
    parsed_steps: List[Dict[str, Any]] = []
    for idx, tok in enumerate(tokens):
        clean_tok = re.sub(r"[?!#+]", "", tok).strip()
        move_obj = None
        for candidate in (tok, clean_tok):
            if not candidate:
                continue
            try:
                move_obj = board.parse_san(candidate)
                break
            except Exception:
                try:
                    m_uci = chess.Move.from_uci(candidate.lower())
                    if m_uci in board.legal_moves:
                        move_obj = m_uci
                        break
                except Exception:
                    pass
        if move_obj is None:
            break
        san = board.san(move_obj)
        fen_before = board.fen()
        is_book = opening_book.is_known_book_move(board, move_obj)
        board.push(move_obj)
        parsed_steps.append({
            "ply": idx + 1,
            "move_number": (idx // 2) + 1,
            "color": "white" if idx % 2 == 0 else "black",
            "uci": move_obj.uci(),
            "san": san,
            "fen_before": fen_before,
            "fen_after": board.fen(),
            "is_book": is_book,
        })

    total_plies = len(parsed_steps)
    ident = opening_book.identify_game_opening([s["san"] for s in parsed_steps]) if parsed_steps else {}
    opening_name = game.get("opening_name") or ident.get("name") or "Standard Opening"
    eco = game.get("eco") or ident.get("eco") or ""
    theory_ply = int(ident.get("ply") or 0)

    if total_plies == 0:
        empty_report = {
            "game_id": game_id,
            "depth": target_depth,
            "user_color": user_color,
            "opening_name": opening_name,
            "eco": eco,
            "theory_ply": 0,
            "white_accuracy": 100.0,
            "black_accuracy": 100.0,
            "counts": {"white": {}, "black": {}},
            "first_novelty_ply": None,
            "critical_moments": [],
            "plies": [],
            "root_eval_cp": 20,
            "root_win_pct": cp_to_win_pct(20),
            "analyzed_at": int(time.time()),
        }
        return empty_report

    pool = get_sync_engine_pool(STOCKFISH_PATH)
    position_evals: List[Dict[str, Any]] = []
    critical_candidates_by_ply: Dict[int, List[Dict[str, Any]]] = {}

    with pool.engine() as engine:
        # Evaluate starting position + after every ply (total_plies + 1 positions)
        all_fens = [parsed_steps[0]["fen_before"]] + [s["fen_after"] for s in parsed_steps]
        for pos_idx, fen in enumerate(all_fens):
            pos_board = chess.Board(fen)
            if pos_board.is_checkmate():
                w_mate = -1 if pos_board.turn == chess.WHITE else 1
                w_cp = -MATE_SCORE_CP if pos_board.turn == chess.WHITE else MATE_SCORE_CP
                position_evals.append({
                    "fen": fen,
                    "white_cp": w_cp,
                    "white_mate": w_mate,
                    "win_pct": cp_to_win_pct(w_cp, w_mate),
                    "best_uci": "",
                    "best_san": "",
                    "pv": [],
                })
            elif pos_board.is_stalemate() or pos_board.is_insufficient_material():
                position_evals.append({
                    "fen": fen,
                    "white_cp": 0,
                    "white_mate": None,
                    "win_pct": 50.0,
                    "best_uci": "",
                    "best_san": "",
                    "pv": [],
                })
            else:
                info = engine.analyse(pos_board, chess.engine.Limit(depth=target_depth))
                w_cp, w_mate = normalize_score(info["score"], chess.WHITE)
                raw_pv = info.get("pv") or []
                line = sanitize_pv(pv_line(pos_board, raw_pv, PV_MAX_PLIES))
                position_evals.append({
                    "fen": fen,
                    "white_cp": w_cp,
                    "white_mate": w_mate,
                    "win_pct": cp_to_win_pct(w_cp, w_mate),
                    "best_uci": line[0]["uci"] if line else "",
                    "best_san": line[0]["san"] if line else "",
                    "pv": line,
                })

            if progress_cb is not None:
                pct = int(round((pos_idx + 1) / (len(all_fens) + 2) * 85))
                progress_cb({
                    "stage": "analyzing",
                    "ply": pos_idx,
                    "total_plies": total_plies,
                    "depth": target_depth,
                    "percent": max(5, min(88, pct)),
                })

        # Build ply records & identify repertoire alignment + mistakes
        plies_out: List[Dict[str, Any]] = []
        on_repertoire_line = True
        first_novelty_ply: Optional[int] = None
        white_accs: List[float] = []
        black_accs: List[float] = []
        counts: Dict[str, Dict[str, int]] = {
            "white": {"book": 0, "best": 0, "good": 0, "inaccuracy": 0, "mistake": 0, "blunder": 0},
            "black": {"book": 0, "best": 0, "good": 0, "inaccuracy": 0, "mistake": 0, "blunder": 0},
        }
        multipv_targets: List[int] = []

        for idx, step in enumerate(parsed_steps):
            ev_before = position_evals[idx]
            ev_after = position_evals[idx + 1]
            mover = step["color"]
            is_user = mover == user_color

            if mover == "white":
                cp_before = ev_before["white_cp"]
                cp_after = ev_after["white_cp"]
                mate_before = ev_before["white_mate"]
                mate_after = ev_after["white_mate"]
                win_before = ev_before["win_pct"]
                win_after = ev_after["win_pct"]
            else:
                cp_before = -ev_before["white_cp"]
                cp_after = -ev_after["white_cp"]
                mate_before = (-ev_before["white_mate"]) if ev_before["white_mate"] is not None else None
                mate_after = (-ev_after["white_mate"]) if ev_after["white_mate"] is not None else None
                win_before = 100.0 - ev_before["win_pct"]
                win_after = 100.0 - ev_after["win_pct"]

            cpl = centipawn_loss(cp_before, cp_after)
            win_loss = round(max(0.0, win_before - win_after), 2)
            acc = round(move_accuracy_pct(win_before, win_after), 1)

            cls = classify_ply_move(
                ply=step["ply"],
                is_book=step["is_book"],
                played_uci=step["uci"],
                best_uci=ev_before["best_uci"],
                win_loss=win_loss,
                cpl=cpl,
                mate_before=mate_before,
                mate_after=mate_after,
            )
            counts[mover][cls] = counts[mover].get(cls, 0) + 1
            if cls != "book":
                if mover == "white":
                    white_accs.append(acc)
                else:
                    black_accs.append(acc)

            # Repertoire status
            norm4 = normalize_fen(step["fen_before"])
            norm3 = " ".join(norm4.split()[:3])
            prepared = user_rep.get(norm4) or user_rep.get(norm3)
            known_opp = opp_rep.get(norm4) or opp_rep.get(norm3) or set()

            rep_status = "out_of_repertoire"
            prepared_uci = ""
            prepared_san = ""
            prepared_study = ""

            if is_user:
                if prepared:
                    prepared_uci = prepared.get("move_uci") or ""
                    prepared_san = prepared.get("move_san") or ""
                    prepared_study = prepared.get("study_name") or ""
                    if prepared_uci == step["uci"]:
                        rep_status = "repertoire_hit"
                    else:
                        rep_status = "repertoire_deviation"
                        on_repertoire_line = False
                else:
                    rep_status = "unprepared"
                    on_repertoire_line = False
            else:
                if step["uci"] in known_opp:
                    rep_status = "known_reply"
                else:
                    if on_repertoire_line and first_novelty_ply is None:
                        rep_status = "opponent_novelty"
                        first_novelty_ply = step["ply"]
                    else:
                        rep_status = "untracked_reply"
                    on_repertoire_line = False

            ply_entry = {
                "ply": step["ply"],
                "move_number": step["move_number"],
                "color": mover,
                "is_user": is_user,
                "uci": step["uci"],
                "san": step["san"],
                "fen_before": step["fen_before"],
                "fen_after": step["fen_after"],
                "eval_cp": ev_after["white_cp"],
                "eval_before_cp": ev_before["white_cp"],
                "mate": ev_after["white_mate"],
                "mate_before": ev_before["white_mate"],
                "win_pct": round(ev_after["win_pct"], 1),
                "win_loss": win_loss,
                "cpl": cpl,
                "accuracy": acc,
                "classification": cls,
                "is_book": step["is_book"],
                "best_uci": ev_before["best_uci"],
                "best_san": ev_before["best_san"],
                "best_pv": ev_before["pv"],
                "refutation_pv": ev_after["pv"],
                "repertoire_status": rep_status,
                "prepared_uci": prepared_uci,
                "prepared_san": prepared_san,
                "prepared_study": prepared_study,
                "candidates": [],
            }
            plies_out.append(ply_entry)

            # Queue MultiPV-3 on user mistakes/traps and on user's first response after opponent novelty
            if is_user and (
                cls in ("inaccuracy", "mistake", "blunder")
                or rep_status == "repertoire_deviation"
                or (first_novelty_ply is not None and step["ply"] == first_novelty_ply + 1)
            ):
                multipv_targets.append(idx)

        # Cap MultiPV pass to the 6 most critical user turns so games with 20 blunders stay snappy
        multipv_targets = sorted(
            multipv_targets,
            key=lambda i: (
                0 if plies_out[i]["ply"] <= OPENING_TRAP_MAX_PLY else 1,
                -plies_out[i]["win_loss"],
            ),
        )[:6]

        for t_pos, idx in enumerate(multipv_targets):
            p_entry = plies_out[idx]
            b_before = chess.Board(p_entry["fen_before"])
            cands = _extract_multipv_candidates(engine, b_before, target_depth, user_color, CRITICAL_MULTIPV)
            p_entry["candidates"] = cands
            critical_candidates_by_ply[p_entry["ply"]] = cands
            if progress_cb is not None:
                pct = 88 + int(round((t_pos + 1) / max(1, len(multipv_targets)) * 10))
                progress_cb({
                    "stage": "coaching",
                    "ply": p_entry["ply"],
                    "total_plies": total_plies,
                    "depth": target_depth,
                    "percent": min(98, pct),
                })

    # Construct Critical Opening & Trap Moments for Guided Repertoire Building
    critical_moments: List[Dict[str, Any]] = []
    for p in plies_out:
        if not p["is_user"]:
            continue
        is_opening_phase = p["ply"] <= OPENING_TRAP_MAX_PLY
        is_error = p["classification"] in ("inaccuracy", "mistake", "blunder")
        is_dev = p["repertoire_status"] == "repertoire_deviation"
        is_first_unprepared_reply = (
            first_novelty_ply is not None and p["ply"] == first_novelty_ply + 1
        )

        if not (is_error or is_dev or (is_first_unprepared_reply and is_opening_phase)):
            continue

        if is_dev:
            kind = "deviation"
            headline = f"Move {p['move_number']}: Left prepared {p['prepared_san']} (played {p['san']})"
        elif is_opening_phase and p["classification"] in ("mistake", "blunder") and p["repertoire_status"] == "unprepared":
            kind = "opening_trap"
            headline = f"Move {p['move_number']}: Opening trap on {p['san']} (best: {p['best_san']})"
        elif is_first_unprepared_reply and not is_error:
            kind = "novelty_response"
            headline = f"Move {p['move_number']}: Opponent left your repertoire before {p['san']}"
        else:
            kind = p["classification"]
            headline = f"Move {p['move_number']}: {p['classification'].capitalize()} {p['san']} (best: {p['best_san']})"

        critical_moments.append({
            "ply": p["ply"],
            "move_number": p["move_number"],
            "kind": kind,
            "classification": p["classification"],
            "repertoire_status": p["repertoire_status"],
            "played_san": p["san"],
            "played_uci": p["uci"],
            "best_san": p["best_san"],
            "best_uci": p["best_uci"],
            "prepared_san": p["prepared_san"],
            "prepared_uci": p["prepared_uci"],
            "win_loss": p["win_loss"],
            "cpl": p["cpl"],
            "fen_before": p["fen_before"],
            "headline": headline,
            "candidates": p["candidates"],
            "refutation_pv": p["refutation_pv"],
        })

    # Prioritize opening traps & deviations first, then largest win_loss
    kind_rank = {"opening_trap": 0, "deviation": 1, "blunder": 2, "mistake": 3, "inaccuracy": 4, "novelty_response": 5}
    critical_moments.sort(key=lambda m: (0 if m["ply"] <= OPENING_TRAP_MAX_PLY else 1, kind_rank.get(m["kind"], 9), -m["win_loss"]))

    white_accuracy = compute_game_accuracy(white_accs)
    black_accuracy = compute_game_accuracy(black_accs)
    if white_accuracy is None:
        white_accuracy = 100.0
    if black_accuracy is None:
        black_accuracy = 100.0

    # Sync opening blunders into `blunders` table so Builder Gaps & Trainer stay unified
    max_op_ply = max(1, int(get_setting("max_opening_ply", "24") or 24))
    blunder_thresh = max(1, int(get_setting("blunder_threshold_cp", "150") or 150))
    db_blunders: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    for p in plies_out:
        if not p["is_user"] or p["ply"] > max_op_ply:
            continue
        user_before_cp = p["eval_before_cp"] if user_color == "white" else -p["eval_before_cp"]
        if user_before_cp < -200:
            continue
        if p["cpl"] < blunder_thresh:
            continue
        user_after_cp = p["eval_cp"] if user_color == "white" else -p["eval_cp"]
        user_before_mate = p["mate_before"] if user_color == "white" else (-p["mate_before"] if p["mate_before"] is not None else None)
        user_after_mate = p["mate"] if user_color == "white" else (-p["mate"] if p["mate"] is not None else None)
        category = "Blunder" if p["cpl"] >= 200 else ("Mistake" if p["cpl"] >= 100 else "Inaccuracy")
        db_blunders.append({
            "game_id": game_id,
            "move_number": p["move_number"],
            "ply": p["ply"],
            "color": user_color,
            "fen_before": p["fen_before"],
            "fen_after": p["fen_after"],
            "played_move": p["uci"],
            "played_san": p["san"],
            "best_move": p["best_uci"] or p["uci"],
            "best_san": p["best_san"] or p["san"],
            "eval_before": user_before_cp,
            "eval_after": user_after_cp,
            "eval_before_mate": user_before_mate,
            "eval_after_mate": user_after_mate,
            "cpl_loss": p["cpl"],
            "opening_name": opening_name,
            "eco": eco,
            "is_book_move": 1 if p["is_book"] else 0,
            "refutation_pv": json.dumps(p["refutation_pv"]),
            "best_pv": json.dumps(p["best_pv"][1:] if len(p["best_pv"]) > 1 else []),
            "explanation": f"Eval loss: {p['cpl']} centipawns ({user_after_cp / 100.0:+.2f} pawns).",
            "strategic_idea": f"Engine top choice: {p['best_san'] or p['san']}.",
            "category": category,
            "created_at": now_ts,
        })

    analyzer = GameAnalyzer(STOCKFISH_PATH)
    analyzer.save_blunders_to_db(db_blunders, game_ids=[game_id])

    report = {
        "game_id": game_id,
        "depth": target_depth,
        "user_color": user_color,
        "white_player": game.get("white_player") or "White",
        "black_player": game.get("black_player") or "Black",
        "white_rating": game.get("white_rating") or 0,
        "black_rating": game.get("black_rating") or 0,
        "speed": game.get("speed") or "rapid",
        "rated": bool(game.get("rated")),
        "result": game.get("result") or "draw",
        "played_at": game.get("played_at") or 0,
        "opening_name": opening_name,
        "eco": eco,
        "theory_ply": theory_ply,
        "white_accuracy": white_accuracy,
        "black_accuracy": black_accuracy,
        "counts": counts,
        "first_novelty_ply": first_novelty_ply,
        "critical_moments": critical_moments,
        "root_eval_cp": position_evals[0]["white_cp"],
        "root_win_pct": round(position_evals[0]["win_pct"], 1),
        "plies": plies_out,
        "analyzed_at": now_ts,
        "cached": False,
    }

    with closing_db() as conn:
        conn.execute(
            """
            UPDATE games
            SET analysis_json = ?,
                analysis_depth = ?,
                white_accuracy = ?,
                black_accuracy = ?,
                analyzed_at = ?
            WHERE id = ?
            """,
            (json.dumps(report), target_depth, white_accuracy, black_accuracy, now_ts, game_id),
        )
        conn.commit()

    return report


async def stream_game_analysis(
    game_id: str,
    depth: Optional[int] = None,
    force: bool = False,
    request: Optional[Any] = None,
) -> AsyncGenerator[str, None]:
    """Stream real-time SSE progress events while analyzing a full game."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_progress(event: Dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("progress", event))

    def worker() -> None:
        try:
            res = analyze_full_game_sync(game_id=game_id, depth=depth, force=force, progress_cb=on_progress)
            loop.call_soon_threadsafe(queue.put_nowait, ("done", res))
        except EngineBusy as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("error", {"code": "engine_busy", "error": sanitize_error(exc)}))
        except (FileNotFoundError, EngineUnavailable) as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("error", {"code": "engine_unavailable", "error": sanitize_error(exc)}))
        except ValueError as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("error", {"code": "not_found", "error": str(exc)}))
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("error", {"code": "analysis_failed", "error": sanitize_error(exc)}))

    task = asyncio.create_task(asyncio.to_thread(worker))
    try:
        while True:
            if request and await request.is_disconnected():
                break
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if task.done() and queue.empty():
                    break
                continue
            if kind == "progress":
                yield format_sse({"type": "progress", **payload}, event="progress")
            elif kind == "done":
                yield format_sse({"type": "complete", "percent": 100, "analysis": payload}, event="complete")
                break
            elif kind == "error":
                yield format_sse({"type": "error", **payload}, event="error")
                break
    finally:
        if not task.done():
            task.cancel()


def preview_sequence_for_repertoire(
    repertoire: str,
    moves: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Preview exact diff between a proposed move sequence and the user's repertoire."""
    rep = (repertoire or "white").strip().lower()
    if rep not in ("white", "black"):
        raise ValueError("Repertoire must be 'white' or 'black'.")
    if not moves:
        return {
            "repertoire": rep,
            "total_plies": 0,
            "existing_count": 0,
            "new_count": 0,
            "conflict_count": 0,
            "steps": [],
            "conflicts": [],
        }

    user_rep, opp_rep = _load_repertoire_lookup(rep)
    steps: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    existing_count = 0
    new_count = 0

    for idx, m in enumerate(moves):
        fen_before = (m.get("fen_before") or "").strip()
        uci = (m.get("uci") or "").strip().lower()
        san = (m.get("san") or "").strip()
        if not fen_before or (not uci and not san):
            raise ValueError(f"Move at index {idx} is missing position or move.")
        board = chess.Board(fen_before)
        if uci:
            move_obj = chess.Move.from_uci(uci)
            if move_obj not in board.legal_moves:
                raise ValueError(f"Illegal move {uci} at ply {idx + 1}.")
            san = board.san(move_obj)
        else:
            move_obj = board.parse_san(san)
            uci = move_obj.uci()
            san = board.san(move_obj)

        is_user = (board.turn == chess.WHITE and rep == "white") or (board.turn == chess.BLACK and rep == "black")
        norm4 = normalize_fen(board.fen())
        norm3 = " ".join(norm4.split()[:3])

        status = "new"
        existing_san = ""
        existing_uci = ""
        if is_user:
            existing = user_rep.get(norm4) or user_rep.get(norm3)
            if existing:
                existing_uci = existing.get("move_uci") or ""
                existing_san = existing.get("move_san") or ""
                if existing_uci == uci:
                    status = "existing"
                    existing_count += 1
                else:
                    status = "conflict"
                    new_count += 1
                    conflicts.append({
                        "index": idx,
                        "move_number": board.fullmove_number,
                        "fen_before": norm4,
                        "existing_uci": existing_uci,
                        "existing_san": existing_san,
                        "proposed_uci": uci,
                        "proposed_san": san,
                    })
            else:
                new_count += 1
        else:
            known = opp_rep.get(norm4) or opp_rep.get(norm3) or set()
            if uci in known:
                status = "existing"
                existing_count += 1
            else:
                new_count += 1

        steps.append({
            "index": idx,
            "move_number": board.fullmove_number,
            "color": "white" if board.turn == chess.WHITE else "black",
            "is_user_move": is_user,
            "fen_before": norm4,
            "uci": uci,
            "san": san,
            "status": status,
            "existing_san": existing_san,
        })

    return {
        "repertoire": rep,
        "total_plies": len(steps),
        "existing_count": existing_count,
        "new_count": new_count,
        "conflict_count": len(conflicts),
        "steps": steps,
        "conflicts": conflicts,
    }


def save_sequence_to_repertoire(
    repertoire: str,
    moves: List[Dict[str, Any]],
    study_name: Optional[str] = None,
    replace_conflicts: bool = False,
    prioritize_for_training: bool = True,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Save an analyzed/guided sequence into `repertoire_moves` without silent overwrites."""
    preview = preview_sequence_for_repertoire(repertoire, moves)
    if preview["total_plies"] == 0:
        raise ValueError("No moves in sequence to save.")

    if preview["conflict_count"] > 0 and not replace_conflicts:
        return {
            "status": "conflict",
            "message": f"{preview['conflict_count']} move(s) conflict with your existing {repertoire} repertoire.",
            "conflicts": preview["conflicts"],
            "preview": preview,
        }

    rm = RepertoireManager.get_instance()
    added_moves = 0
    updated_moves = 0
    first_new_user_marked = False

    for step in preview["steps"]:
        is_new_or_conflict = step["status"] in ("new", "conflict")
        mark_important: Optional[bool] = None
        move_comment: Optional[str] = None
        if step["is_user_move"] and is_new_or_conflict:
            if prioritize_for_training and not first_new_user_marked:
                mark_important = True
                first_new_user_marked = True
                if note and note.strip():
                    move_comment = note.strip()

        res = rm.add_repertoire_move(
            repertoire=preview["repertoire"],
            parent_fen=step["fen_before"],
            move_uci=step["uci"],
            is_user_move=step["is_user_move"],
            popularity_pct=100.0,
            win_rate=55.0,
            eval_score=0,
            is_important=mark_important,
            comment=move_comment,
            study_name=study_name.strip() if isinstance(study_name, str) and study_name.strip() else None,
            replace_existing=bool(replace_conflicts and step["status"] == "conflict"),
        )
        if res.get("status") != "success":
            raise ValueError(res.get("message") or f"Failed saving move {step['san']}.")
        if step["status"] == "existing":
            updated_moves += 1
        else:
            added_moves += 1

    return {
        "status": "success",
        "repertoire": preview["repertoire"],
        "added_moves": added_moves,
        "existing_moves": updated_moves,
        "total_plies": preview["total_plies"],
        "study_name": study_name or "",
    }
