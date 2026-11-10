"""
backend/trainer.py — State-of-the-Art Chess Repertoire Training Engine

Combines:
  1. Calibrated FSRS-5 Memory Model (Difficulty, Stability, Retrievability) with non-destructive
     post-lapse stability preservation (`fsrs5_lapse_stability`) and short-term vs long-term spacing.
  2. Implicit 4-Grade Latency Classification (`Again=1, Hard=2, Good=3, Easy=4`) inferred from
     attempt accuracy and response timing (`response_ms`), with an idle-AFK guard (>60s -> Good).
  3. Transposition-Aware DAG Graph (`build_repertoire_tree_graph`) that tracks all move-order paths
     reaching a position so prerequisites unlock via any learned move order and subtree weakness
     propagates up every ancestor path.
  4. Real-Game Lichess Deviation Coupling: practiced/mastered moves forgotten in a subsequent real
     game (`played_at > last_practiced`) are automatically promoted into Adaptive review.
  5. Precomputed 2-Ply Context Lead-In (`lead_in_steps`) for dynamic OTB-realistic visual cueing.
"""
import json
import math
import random
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import chess

from backend.db import closing_db, get_db, get_setting
from backend.deviations import find_game_deviations
from backend.utils import normalize_fen, normalize_fen_3

# --------------------------------------------------------------------------- #
# Line-end semantics
# --------------------------------------------------------------------------- #

LINE_END_REPERTOIRE_EXHAUSTED = "repertoire_exhausted"
LINE_END_UNANSWERED_BRANCHES = "unanswered_opponent_branches"

LINE_POSITION_CONTINUES = "continues"
LINE_POSITION_COMPLETED = "completed_line"
LINE_POSITION_AWAITING_REPLY = "awaiting_user_reply"


def classify_line_position(reached_by_user_move: bool, has_prepared_reply: bool) -> str:
    """Single source of truth for whether a repertoire line is finished."""
    if has_prepared_reply:
        return LINE_POSITION_CONTINUES
    if reached_by_user_move:
        return LINE_POSITION_COMPLETED
    return LINE_POSITION_AWAITING_REPLY


# --------------------------------------------------------------------------- #
# FSRS-5 Memory Model & Implicit Latency Grading
# --------------------------------------------------------------------------- #

FSRS_FACTOR = 19.0 / 81.0
FSRS_DECAY = -0.5
FSRS_TARGET_RETENTION = 0.90
FSRS_DEFAULT_DIFFICULTY = 5.0
FSRS_MIN_DIFFICULTY = 1.0
FSRS_MAX_DIFFICULTY = 10.0

GRADE_AGAIN = 1
GRADE_HARD = 2
GRADE_GOOD = 3
GRADE_EASY = 4

#: Calibrated FSRS-5 benchmark weights (w0..w18) adapted for chess move recall.
FSRS5_W = (
    0.40255,   # w0: S0(Again)
    1.18385,   # w1: S0(Hard)
    3.17300,   # w2: S0(Good)
    15.69105,  # w3: S0(Easy)
    7.19490,   # w4: D0 base
    0.53450,   # w5: D0 grade exponent
    1.46040,   # w6: difficulty delta scale
    0.00460,   # w7: mean reversion weight toward D0(4)
    1.54575,   # w8: recall stability gain exponent
    0.11920,   # w9: stability saturation power
    1.01925,   # w10: retrievability spacing gain
    1.93950,   # w11: post-lapse base scale
    0.11000,   # w12: post-lapse difficulty power
    0.29605,   # w13: post-lapse prior stability power
    2.26980,   # w14: post-lapse retrievability factor
    0.23150,   # w15: Hard stability multiplier
    2.98980,   # w16: Easy stability multiplier
    0.51655,   # w17: same-day short-term stability scale
    0.66210,   # w18: same-day short-term grade offset
)

#: Latency thresholds (milliseconds) for implicit FSRS grading on first-try recalls.
LATENCY_EASY_MAX_MS = 2500
LATENCY_HARD_MIN_MS = 12000
LATENCY_AFK_IGNORE_MS = 60000

SESSION_LAPSE_COOLDOWN_DRILLS = 3
MASTERED_AUDIT_CADENCE = 5
MAX_DAG_PATHS_PER_POSITION = 4


def compute_fsrs_retrievability(
    last_practiced: Any,
    stability_days: Any,
    now: Optional[int] = None,
) -> float:
    """Computes recall probability R(t, S) in [0.01, 1.0] via the FSRS power-law forgetting curve."""
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


def infer_fsrs_grade(
    is_correct: bool,
    is_retry: bool = False,
    response_ms: Optional[int] = None,
    prev_tp: int = 0,
) -> int:
    """Maps a chess board attempt + response time into FSRS grade 1..4 without UI button friction.

    * Mistake or hint/retry -> GRADE_AGAIN (1)
    * Unmeasured or AFK (>60s) -> GRADE_GOOD (3)
    * Hesitant (>12s) -> GRADE_HARD (2)
    * Fluent (<2.5s on a previously seen position) -> GRADE_EASY (4)
    * Normal recall -> GRADE_GOOD (3)
    """
    if not is_correct or is_retry:
        return GRADE_AGAIN
    if response_ms is None:
        return GRADE_GOOD
    try:
        ms = int(response_ms)
    except (TypeError, ValueError):
        return GRADE_GOOD
    if ms <= 0 or ms > LATENCY_AFK_IGNORE_MS:
        return GRADE_GOOD
    if ms > LATENCY_HARD_MIN_MS:
        return GRADE_HARD
    if ms < LATENCY_EASY_MAX_MS and prev_tp >= 1:
        return GRADE_EASY
    return GRADE_GOOD


def fsrs5_initial_difficulty(grade: int) -> float:
    w4, w5 = FSRS5_W[4], FSRS5_W[5]
    d0 = w4 - math.exp(w5 * (grade - 1)) + 1.0
    return round(max(FSRS_MIN_DIFFICULTY, min(FSRS_MAX_DIFFICULTY, d0)), 2)


def fsrs5_next_difficulty(prev_d: float, grade: int) -> float:
    """FSRS-5 linear-damped difficulty update with mean reversion toward D0(Easy)."""
    w6, w7 = FSRS5_W[6], FSRS5_W[7]
    d0_easy = fsrs5_initial_difficulty(GRADE_EASY)
    delta_d = -w6 * (grade - 3)
    d_prime = prev_d + delta_d * ((10.0 - prev_d) / 9.0)
    d_next = w7 * d0_easy + (1.0 - w7) * d_prime
    return round(max(FSRS_MIN_DIFFICULTY, min(FSRS_MAX_DIFFICULTY, d_next)), 2)


def fsrs5_lapse_stability(difficulty: float, prev_stability: float, retrievability: float, prev_tc: int) -> float:
    """FSRS-5 post-lapse stability S'_f(D, S, R).

    Preserves partial long-term memory trace on consolidated veteran cards (`prev_stability >= 10.0`),
    while resetting newly-introduced cards (`prev_stability < 10.0` or `prev_tc == 0`) to 1.0d.
    """
    if prev_tc <= 0 or prev_stability < 10.0:
        return 1.0
    w11, w12, w13, w14 = FSRS5_W[11], FSRS5_W[12], FSRS5_W[13], FSRS5_W[14]
    d = max(FSRS_MIN_DIFFICULTY, min(FSRS_MAX_DIFFICULTY, difficulty))
    r = max(0.01, min(1.0, retrievability))
    sf = w11 * (d ** (-w12)) * (((prev_stability + 1.0) ** w13) - 1.0) * math.exp(w14 * (1.0 - r))
    return round(max(1.0, min(prev_stability * 0.70, sf)), 2)


def fsrs5_recall_stability(
    difficulty: float,
    prev_stability: float,
    retrievability: float,
    grade: int,
    new_streak: int,
    lapses: int,
    elapsed_days: float,
) -> float:
    """FSRS-5 recall stability S'_r(D, S, R, G) with chess-specific consolidation floors."""
    if new_streak == 1:
        base = 0.7 if grade == GRADE_HARD else (3.5 if grade == GRADE_EASY else 1.0)
        return round(max(base, prev_stability * 0.85 if prev_stability > 1.5 else base), 2)

    if new_streak == 2:
        if lapses >= 3:
            floor_s = 2.0
        else:
            floor_s = 2.2 if grade == GRADE_HARD else (4.5 if grade == GRADE_EASY else 3.0)
        if prev_stability > floor_s:
            mult = 1.25 if grade == GRADE_HARD else (1.85 if grade == GRADE_EASY else 1.5)
            return round(min(3650.0, prev_stability * mult), 2)
        return round(floor_s, 2)

    if new_streak == 3 and lapses == 0 and prev_stability <= 4.5:
        # Initial graduation step from learning into multi-day spaced retention
        mult = 1.6 if grade == GRADE_HARD else (2.9 if grade == GRADE_EASY else 2.2)
        return round(prev_stability * mult, 1)

    # Streak >= 4 (or veteran/lapsed cards): full FSRS-5 stability expansion
    d = max(FSRS_MIN_DIFFICULTY, min(FSRS_MAX_DIFFICULTY, difficulty))
    s = max(1.0, prev_stability)
    r = max(0.01, min(1.0, retrievability))

    if elapsed_days < 0.5 and s >= 6.0:
        # FSRS-5 short-term same-day review equation prevents cramming interval explosion
        w17, w18 = FSRS5_W[17], FSRS5_W[18]
        short_mult = math.exp(w17 * (grade - 3 + w18))
        s_next = s * max(1.05 if grade >= GRADE_GOOD else 0.95, short_mult)
    else:
        w8, w9, w10, w15, w16 = FSRS5_W[8], FSRS5_W[9], FSRS5_W[10], FSRS5_W[15], FSRS5_W[16]
        eff_r = min(0.92, r)
        hard_pen = w15 if grade == GRADE_HARD else 1.0
        easy_bon = w16 if grade == GRADE_EASY else 1.0
        inc = math.exp(w8) * (11.0 - d) * (s ** (-w9)) * (math.exp(w10 * (1.0 - eff_r)) - 1.0) * hard_pen * easy_bon
        s_next = s * (1.0 + max(0.20, inc))

    return round(min(3650.0, max(3.0, s_next)), 1)


def infer_move_memory_state(move: Dict[str, Any], now: Optional[int] = None) -> Dict[str, Any]:
    """Normalises a `repertoire_moves` row into FSRS-5 DSR memory variables + streak/leech state."""
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
    """Classifies a user move into `'mastered'`, `'learning'`, or `'unlearned'`."""
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


def compute_lead_in_steps(path_san: List[str], max_plies: int = 2) -> List[Dict[str, str]]:
    """Precomputes the final `max_plies` board transitions leading into the drill position.

    Allows the frontend board to start 1-2 plies earlier and animate dynamically into the
    target position (encoding-specificity / OTB cue chaining) with zero extra user clicks.
    """
    if not path_san:
        return []
    board = chess.Board()
    history: List[Dict[str, str]] = []
    for san in path_san:
        try:
            fen_before = board.fen()
            mv = board.parse_san(san)
            uci = mv.uci()
            clean_san = board.san(mv)
            board.push(mv)
            history.append({
                "fen_before": fen_before,
                "fen_after": board.fen(),
                "from": uci[:2],
                "to": uci[2:4],
                "uci": uci,
                "san": clean_san,
            })
        except Exception:
            return []
    return history[-max_plies:]


class TrainerMixin:
    """FSRS-5 + DAG Tree-Aware + Latency-Graded Repertoire Trainer."""

    @staticmethod
    def format_pgn_moves(path_san: List[str]) -> str:
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

    def get_trainer_progress(self, repertoire: str = "white", study_name: Optional[str] = None) -> Dict[str, Any]:
        repertoire = (repertoire or "white").lower().strip()
        is_mixed = repertoire in ("mixed", "both", "all")
        if not is_mixed and repertoire not in ("white", "black"):
            repertoire = "white"

        with closing_db() as conn:
            cursor = conn.cursor()
            if is_mixed:
                cursor.execute(
                    """
                    SELECT rm.id, rm.parent_fen, rm.fen, rm.move_uci, rm.move_san, rm.times_practiced, rm.times_correct,
                           rm.last_practiced, rm.next_review, rm.interval_days, rm.study_name,
                           rm.consecutive_correct, rm.lapses, rm.difficulty,
                           COALESCE(rs.name, rm.study_name, '') as resolved_study
                    FROM repertoire_moves rm
                    LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                    LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                    WHERE rm.is_user_move = 1
                    """
                )
                rows = [dict(r) for r in cursor.fetchall()]
                cursor.execute("SELECT id, name FROM repertoire_studies")
                study_records = [dict(s) for s in cursor.fetchall()]
            else:
                cursor.execute(
                    """
                    SELECT rm.id, rm.parent_fen, rm.fen, rm.move_uci, rm.move_san, rm.times_practiced, rm.times_correct,
                           rm.last_practiced, rm.next_review, rm.interval_days, rm.study_name,
                           rm.consecutive_correct, rm.lapses, rm.difficulty,
                           COALESCE(rs.name, rm.study_name, '') as resolved_study
                    FROM repertoire_moves rm
                    LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                    LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                    WHERE rm.repertoire = ? AND rm.is_user_move = 1
                    """,
                    (repertoire,),
                )
                rows = [dict(r) for r in cursor.fetchall()]
                cursor.execute("SELECT id, name FROM repertoire_studies WHERE repertoire = ?", (repertoire,))
                study_records = [dict(s) for s in cursor.fetchall()]

        unique_user_moves = {}
        move_studies_map = {}

        for r in rows:
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

        def _moves_key(m):
            return (" ".join((m["parent_fen"] or "").split()[:3]), m["move_uci"])

        def _segment(moves):
            total = len(moves)
            mastered = sum(1 for m in moves if classify_move_mastery(m) == "mastered")
            learning = sum(1 for m in moves if classify_move_mastery(m) == "learning")
            unlearned = total - mastered - learning
            if total > 0:
                return (
                    total,
                    mastered,
                    learning,
                    unlearned,
                    round((mastered / total * 100), 1),
                    round((learning / total * 100), 1),
                    round((unlearned / total * 100), 1),
                )
            return (0, 0, 0, 0, 0.0, 0.0, 0.0)

        filter_clean = (study_name or "").strip()
        target_moves = all_user_moves
        if filter_clean:
            target_moves = [m for m in all_user_moves if filter_clean in move_studies_map.get(_moves_key(m), set())]

        (total, mastered, learning, unlearned, mastered_pct, learning_pct, unlearned_pct) = _segment(target_moves)

        studies_breakdown = []
        for s in study_records:
            s_name = s["name"]
            s_moves = [m for m in all_user_moves if s_name in move_studies_map.get(_moves_key(m), set())]
            (s_tot, s_mast, s_learn, s_unl, s_mast_pct, s_learn_pct, s_unl_pct) = _segment(s_moves)
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
            "studies": studies_breakdown,
        }

    def build_repertoire_tree_graph(self, repertoire: str, study_filter: Optional[str] = None) -> Dict[int, Dict[str, Any]]:
        """Builds a transposition-aware DAG of repertoire moves.

        Unlike a naive single-visit BFS, positions reached by multiple transposing move orders
        (up to `MAX_DAG_PATHS_PER_POSITION` cycle-free paths) merge their ancestor chains and
        unlock `prerequisites_met` whenever *any* valid move order has its prerequisite user moves
        learned. Descendant weakness backpropagates up *every* transposing ancestor branch.
        """
        now = int(time.time())
        with closing_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT sm.parent_fen, sm.move_uci, rs.name
                FROM study_moves sm
                JOIN repertoire_studies rs ON rs.id = sm.study_id
                WHERE sm.repertoire = ?
                """,
                (repertoire,),
            )
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
                cursor.execute(
                    """
                    SELECT DISTINCT rm.*, COALESCE(rs.name, rm.study_name) as effective_study
                    FROM repertoire_moves rm
                    LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                    LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                    WHERE rm.repertoire = ?
                      AND (rs.name = ? OR rm.study_name = ? OR rm.opening_name = ?)
                    """,
                    (repertoire, clean_filter, clean_filter, clean_filter),
                )
            else:
                cursor.execute(
                    """
                    SELECT DISTINCT rm.*, COALESCE(rs.name, rm.study_name) as effective_study
                    FROM repertoire_moves rm
                    LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                    LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                    WHERE rm.repertoire = ?
                    """,
                    (repertoire,),
                )
            all_moves = [dict(r) for r in cursor.fetchall()]

        by_parent: Dict[str, List[Dict]] = {}
        all_fen3 = set()
        # Deduplicate rows multiplied by study joins (preserve distinct move ids)
        unique_moves_by_id: Dict[int, Dict] = {}
        for m in all_moves:
            mid = m["id"]
            if mid not in unique_moves_by_id:
                unique_moves_by_id[mid] = m
                p3 = " ".join(m["parent_fen"].split()[:3])
                m_fen3 = " ".join(m["fen"].split()[:3])
                by_parent.setdefault(p3, []).append(m)
                all_fen3.add(m_fen3)

        start_fen3 = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq"
        root_parents = [start_fen3] if start_fen3 in by_parent else []
        for p3 in by_parent:
            if p3 != start_fen3 and p3 not in all_fen3 and p3 not in root_parents:
                root_parents.append(p3)

        def _compute_node_metrics(m: Dict, node_depth: int, prereqs_met: bool):
            mem = infer_move_memory_state(m, now=now)
            tp = mem["tp"]
            next_rev = m.get("next_review") or 0
            interv = max(1.0, mem["stability"])
            # FSRS due criterion: past explicit next_review OR retrievability decayed to <= 90%
            r_due = tp > 0 and (m.get("last_practiced") or 0) > 0 and mem["retrievability"] <= FSRS_TARGET_RETENTION
            is_due = bool(tp > 0 and ((next_rev > 0 and next_rev <= now) or r_due))
            if is_due:
                raw_urg = (now - next_rev) / (interv * 86400.0) if (next_rev > 0 and next_rev <= now) else 0.0
                urgency = max(raw_urg, (FSRS_TARGET_RETENTION - mem["retrievability"]) * 5.0)
            else:
                urgency = 0.0
            mastery = classify_move_mastery(m)
            is_frontier = (tp == 0 and prereqs_met)

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

            return mem, is_due, urgency, mastery, is_frontier, node_weakness

        graph: Dict[int, Dict[str, Any]] = {}
        visits_per_fen3: Dict[str, int] = defaultdict(int)
        queue: List[Tuple[str, int, List[str], List[Dict], frozenset]] = []
        for rp in root_parents:
            queue.append((rp, 0, [], [], frozenset({rp})))

        while queue:
            curr_fen3, depth, path_san, path_moves, path_fens = queue.pop(0)
            for m in by_parent.get(curr_fen3, []):
                m_fen3 = " ".join(m["fen"].split()[:3])
                if m_fen3 in path_fens:
                    # Cycle guard (threefold repetition loops in PGNs)
                    continue

                new_path_san = path_san + [m["move_san"]]
                new_path_moves = path_moves + [m]

                if m["is_user_move"] == 1:
                    ancestor_user_moves = [p for p in path_moves if p.get("is_user_move") == 1]
                    prereqs_met = all((p.get("times_correct") or 0) >= 1 for p in ancestor_user_moves)
                    node_depth = depth + 1
                    mem, is_due, urgency, mastery, is_frontier, node_weakness = _compute_node_metrics(
                        m, node_depth, prereqs_met
                    )

                    norm_parent = normalize_fen(m["parent_fen"])
                    parent_3 = " ".join(norm_parent.split()[:3])
                    m_studies = list(
                        study_map.get((norm_parent, m["move_uci"]))
                        or study_map.get((parent_3, m["move_uci"]))
                        or []
                    )
                    fallback = (m.get("effective_study") or m.get("study_name") or m.get("opening_name") or "").strip()
                    if fallback and fallback not in m_studies:
                        m_studies.append(fallback)

                    anc_ids = [p["id"] for p in ancestor_user_moves]
                    if m["id"] in graph:
                        existing_node = graph[m["id"]]
                        for s in m_studies:
                            if s not in existing_node["studies"]:
                                existing_node["studies"].append(s)
                        # Record alternative transposing ancestor chain
                        existing_node["ancestor_paths"].append(anc_ids)
                        for aid in anc_ids:
                            if aid not in existing_node["ancestors"]:
                                existing_node["ancestors"].append(aid)
                        # Unlock prerequisites if this transposing path is already learned!
                        if prereqs_met and not existing_node["prerequisites_met"]:
                            existing_node["prerequisites_met"] = True
                            existing_node["is_frontier"] = (mem["tp"] == 0)
                            existing_node["path_san"] = path_san
                            existing_node["path_formatted"] = self.format_pgn_moves(path_san)
                            existing_node["ply_depth"] = node_depth
                            _, _, _, _, _, upd_weak = _compute_node_metrics(m, node_depth, True)
                            existing_node["node_weakness"] = upd_weak
                            existing_node["subtree_weakness"] = max(existing_node["subtree_weakness"], upd_weak)
                    else:
                        graph[m["id"]] = {
                            "move": m,
                            "ply_depth": node_depth,
                            "path_san": path_san,
                            "path_formatted": self.format_pgn_moves(path_san),
                            "ancestors": list(anc_ids),
                            "ancestor_paths": [anc_ids],
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
                            "effective_study": m_studies[0] if m_studies else fallback,
                        }

                if visits_per_fen3[m_fen3] < MAX_DAG_PATHS_PER_POSITION:
                    visits_per_fen3[m_fen3] += 1
                    queue.append((m_fen3, depth + 1, new_path_san, new_path_moves, path_fens | {m_fen3}))

        # Disconnected user moves fallback
        for m in unique_moves_by_id.values():
            if m["is_user_move"] == 1 and m["id"] not in graph:
                mem, is_due, urgency, mastery, is_frontier, node_weakness = _compute_node_metrics(m, 99, True)
                norm_parent = normalize_fen(m["parent_fen"])
                parent_3 = " ".join(norm_parent.split()[:3])
                m_studies = list(
                    study_map.get((norm_parent, m["move_uci"]))
                    or study_map.get((parent_3, m["move_uci"]))
                    or []
                )
                fallback = (m.get("effective_study") or m.get("study_name") or m.get("opening_name") or "").strip()
                if fallback and fallback not in m_studies:
                    m_studies.append(fallback)

                graph[m["id"]] = {
                    "move": m,
                    "ply_depth": 99,
                    "path_san": [],
                    "path_formatted": "",
                    "ancestors": [],
                    "ancestor_paths": [],
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
                    "effective_study": m_studies[0] if m_studies else fallback,
                }

        # DAG-aware backpropagation across every transposing ancestor chain
        for node in graph.values():
            nw = node["node_weakness"]
            for anc_chain in node.get("ancestor_paths") or []:
                total_ancs = len(anc_chain)
                for idx, anc_id in enumerate(anc_chain):
                    if anc_id in graph:
                        dist = max(1, total_ancs - idx)
                        discounted = nw * (0.92 ** dist)
                        if discounted > graph[anc_id]["subtree_weakness"]:
                            graph[anc_id]["subtree_weakness"] = discounted

        return graph

    def _find_post_practice_game_deviations(self, repertoire: str, graph: Dict[int, Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """Finds practiced repertoire moves (`tp > 0`) that the user subsequently forgot in a real game."""
        practiced_by_fen3: Dict[str, Dict[str, Any]] = {}
        for node in graph.values():
            m = node["move"]
            if (m.get("times_practiced") or 0) > 0 and (m.get("last_practiced") or 0) > 0:
                p3 = normalize_fen_3(m.get("parent_fen") or "")
                if p3:
                    practiced_by_fen3[p3] = node
        if not practiced_by_fen3:
            return {}

        try:
            dev_res = find_game_deviations(color=repertoire, limit=100)
        except Exception:
            return {}

        flagged: Dict[int, Dict[str, Any]] = {}
        for d in dev_res.get("deviations") or []:
            p3 = normalize_fen_3(d.get("fen_before") or "")
            node = practiced_by_fen3.get(p3)
            if not node:
                continue
            last_prac = int(node["move"].get("last_practiced") or 0)
            raw_played = d.get("played_at") or 0
            played_sec = int(raw_played // 1000) if raw_played > 100_000_000_000 else int(raw_played)
            if played_sec > last_prac:
                mid = node["move"]["id"]
                if mid not in flagged:
                    flagged[mid] = d
        return flagged

    def get_next_trainer_drill(
        self,
        repertoire: str,
        opening_filter: Optional[str] = None,
        session_mode: str = "adaptive",
        recent_session_lapses: Optional[List[int]] = None,
        recent_drilled_ids: Optional[List[int]] = None,
    ) -> Optional[Dict]:
        repertoire = (repertoire or "").lower().strip()
        if repertoire in ("mixed", "both", "all"):
            last_color = None
            if recent_drilled_ids:
                try:
                    with closing_db() as conn:
                        r_row = conn.execute(
                            "SELECT repertoire FROM repertoire_moves WHERE id = ?",
                            (recent_drilled_ids[-1],),
                        ).fetchone()
                        if r_row:
                            last_color = r_row["repertoire"]
                except Exception:
                    pass

            drill_w = self._get_single_trainer_drill(
                "white", opening_filter, session_mode, recent_session_lapses, recent_drilled_ids
            )
            drill_b = self._get_single_trainer_drill(
                "black", opening_filter, session_mode, recent_session_lapses, recent_drilled_ids
            )

            is_real_w = bool(drill_w and "expected_uci" in drill_w)
            is_real_b = bool(drill_b and "expected_uci" in drill_b)

            if is_real_w and not is_real_b:
                return drill_w
            if is_real_b and not is_real_w:
                return drill_b
            if not is_real_w and not is_real_b:
                return drill_w or drill_b

            reason_priority = {
                "session_lapse": 5,
                "game_deviation": 4,
                "leech_repair": 3,
                "due_review": 3,
                "frontier_new": 2,
                "mastery_audit": 1,
                "maintenance": 1,
                "mastery_refresh": 1,
            }
            p_w = reason_priority.get(drill_w.get("drill_reason", ""), 0)
            p_b = reason_priority.get(drill_b.get("drill_reason", ""), 0)

            if p_w > p_b:
                return drill_w
            if p_b > p_w:
                return drill_b

            if last_color == "white":
                return drill_b
            if last_color == "black":
                return drill_w
            return random.choice([drill_w, drill_b])

        return self._get_single_trainer_drill(
            repertoire, opening_filter, session_mode, recent_session_lapses, recent_drilled_ids
        )

    def _get_single_trainer_drill(
        self,
        repertoire: str,
        opening_filter: Optional[str] = None,
        session_mode: str = "adaptive",
        recent_session_lapses: Optional[List[int]] = None,
        recent_drilled_ids: Optional[List[int]] = None,
    ) -> Optional[Dict]:
        repertoire = (repertoire or "").lower()
        if repertoire not in ("white", "black"):
            return None

        target_study = (opening_filter or "").strip() or None
        graph = self.build_repertoire_tree_graph(repertoire, study_filter=target_study)
        if not graph:
            return None

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
            "total_moves": len(graph),
        }

        recent_list = list(recent_drilled_ids or [])
        recent_ids_set = set(recent_list)
        step_idx = len(recent_list)
        chosen_node = None
        chosen_study = None
        drill_reason = "frontier_new"
        is_mastered_audit = False
        live_deviation_info = None

        def _resolve_study(node):
            if target_study:
                return target_study
            if node.get("studies"):
                return node["studies"][0]
            return node.get("effective_study")

        def _audit_score(n):
            decay = 1.0 - float(n.get("retrievability") or 0.0)
            depth = min(14, int(n.get("ply_depth") or 1))
            depth_bonus = (depth * 0.15) if depth >= 3 else 0.05
            history_risk = float(n.get("lapses") or 0) * 0.6 + (float(n.get("difficulty") or 5.0) - 5.0) * 0.2
            imp = 0.5 if n["move"].get("is_important") else 0.0
            last_p = int(n["move"].get("last_practiced") or 0)
            return (round(decay * 4.0 + depth_bonus + history_risk + imp, 4), -last_p, -int(n["move"]["id"]))

        all_studies = []
        for n in graph.values():
            for s in n.get("studies", []):
                if s and s not in all_studies:
                    all_studies.append(s)
            fallback_s = (n.get("effective_study") or n["move"].get("study_name") or "").strip()
            if fallback_s and fallback_s not in all_studies:
                all_studies.append(fallback_s)

        recent_study_history = []
        for mid in reversed(recent_list):
            if mid in graph:
                for st in graph[mid].get("studies", []):
                    if st and st not in recent_study_history:
                        recent_study_history.append(st)

        unseen_studies = [s for s in all_studies if s not in recent_study_history]
        random.shuffle(unseen_studies)
        seen_studies = list(reversed(recent_study_history))
        candidate_studies = (unseen_studies + seen_studies) or all_studies[:]

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
                    "message": "All missed moves reinforced!",
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
                    "message": "All weak spots consolidated!",
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

        # --- TIER 1.8 (Adaptive): Real-Game Deviation Memory Repair ---
        # Surfaces previously practiced/mastered moves that the user forgot in a subsequent real game.
        if not chosen_node and session_mode == "adaptive":
            live_devs = self._find_post_practice_game_deviations(repertoire, graph)
            avail_dev_ids = [mid for mid in live_devs if mid not in recent_ids_set]
            if avail_dev_ids:
                chosen_id = avail_dev_ids[0]
                chosen_node = graph[chosen_id]
                live_deviation_info = live_devs[chosen_id]
                drill_reason = "game_deviation"
                chosen_study = _resolve_study(chosen_node)

        # --- TIER 2: Spaced Repetition Due Reviews (FSRS Retrievability + Urgency) ---
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
                    reverse=True,
                )
                chosen_node = available_due[0]
                drill_reason = "due_review"
                chosen_study = _resolve_study(chosen_node)
            elif session_mode == "due_only":
                return {
                    "repertoire": repertoire,
                    "all_due_completed": True,
                    "queue_summary": queue_summary,
                    "message": "All reviews are up to date!",
                }

        # --- TIER 2.5 (Adaptive): Weak / Leech Recovery Interleaving ---
        if not chosen_node and session_mode == "adaptive":
            avail_weak = [n for n in weak_nodes if not n["is_due"] and n["move"]["id"] not in recent_ids_set]
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
                        matching_frontier = [
                            n
                            for n in available_frontier
                            if cand_st in n.get("studies", []) or n.get("effective_study") == cand_st
                        ]
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
                    "message": "All repertoire lines have been learned!",
                }

        # --- TIER 3.5 (Adaptive): Learning Consolidation ---
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
                study_nodes = [
                    n
                    for n in graph.values()
                    if chosen_study in n.get("studies", []) or n.get("effective_study") == chosen_study
                ]
            if not study_nodes:
                study_nodes = list(graph.values())

            unseen_study_nodes = [n for n in study_nodes if n["move"]["id"] not in recent_ids_set]
            if unseen_study_nodes:
                study_nodes = unseen_study_nodes

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
            ply_depth = int(chosen_node.get("ply_depth") or 0)
            fullmove = max(1, (ply_depth + 1) // 2)
            p_fen += f" 0 {fullmove}"
        elif len(parts) == 5:
            p_fen += " 1"

        last_move = None
        parent_3 = " ".join(p_fen.split()[:3])
        norm_p4 = " ".join(p_fen.split()[:4])
        with closing_db() as conn:
            prev_row = conn.execute(
                """
                SELECT * FROM repertoire_moves
                WHERE repertoire = ? AND (fen = ? OR fen = ? OR fen LIKE ?)
                ORDER BY (fen = ?) DESC, is_important DESC, id ASC
                LIMIT 1
                """,
                (repertoire, norm_p4, parent_3, f"{parent_3} %", norm_p4),
            ).fetchone()
        if prev_row:
            uci = prev_row["move_uci"]
            last_move = {"from": uci[:2], "to": uci[2:4], "san": prev_row["move_san"]}

        effective_study = (
            target_study
            or chosen_study
            or chosen_node.get("effective_study")
            or chosen.get("study_name")
            or chosen.get("opening_name", "")
        ).strip()
        opening_display = (
            target_study
            or chosen_study
            or chosen_node.get("effective_study")
            or chosen.get("study_name")
            or chosen.get("opening_name")
            or effective_study
        )

        tp = chosen.get("times_practiced") or 0
        tc = chosen.get("times_correct") or 0
        mastery = classify_move_mastery(chosen)
        path_san = chosen_node["path_san"]

        payload = {
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
            "lead_in_steps": compute_lead_in_steps(path_san, max_plies=2),
            "mastery_status": mastery,
            "times_practiced": tp,
            "times_correct": tc,
            "consecutive_correct": chosen_node["streak"],
            "lapses": chosen_node["lapses"],
            "difficulty": chosen_node["difficulty"],
            "stability": chosen_node["stability"],
            "retrievability": round(chosen_node["retrievability"], 3),
            "is_leech": chosen_node["is_leech"],
            "is_mastered_audit": is_mastered_audit,
            "path_san": path_san,
            "path_formatted": chosen_node["path_formatted"],
            "drill_reason": drill_reason,
            "ply_depth": chosen_node["ply_depth"],
            "queue_summary": queue_summary,
        }
        if live_deviation_info:
            payload["game_id"] = live_deviation_info.get("game_id")
            payload["played_san"] = live_deviation_info.get("played_san")
            payload["played_uci"] = live_deviation_info.get("played_uci")
            payload["opponent_name"] = live_deviation_info.get("opponent_name")
        return payload

    def get_repertoire_openings(self, repertoire: Optional[str] = None) -> Dict[str, Any]:
        clean_rep = (
            None
            if (not repertoire or repertoire.lower().strip() in ("mixed", "both", "all"))
            else repertoire.lower().strip()
        )
        studies = self.get_studies(clean_rep)
        openings = []
        for s in studies:
            openings.append({
                "opening_name": s["name"],
                "study_id": s["id"],
                "eco": "",
                "move_count": s["user_moves"] or 0,
                "repertoire": s["repertoire"],
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
            "recent_opening": recent_opening,
        }

    def evaluate_trainer_move(
        self,
        repertoire: str,
        current_fen: str,
        attempted_uci: str,
        study_filter: Optional[str] = None,
        is_retry: bool = False,
        response_ms: Optional[int] = None,
    ) -> Dict:
        """Grades a repertoire attempt with FSRS-5 + implicit response-latency grading and picks the next opponent branch."""
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

        attempted_uci = (attempted_uci or "").strip().lower()
        if len(attempted_uci) == 5:
            try:
                from_sq = chess.parse_square(attempted_uci[:2])
                piece = board.piece_at(from_sq)
                is_promotion = bool(piece and piece.piece_type == chess.PAWN and attempted_uci[3] in ("1", "8"))
            except Exception:
                is_promotion = False
            if not is_promotion:
                return {"correct": False, "message": f"Invalid move format: {attempted_uci}."}

        try:
            move_obj = chess.Move.from_uci(attempted_uci)
            if move_obj not in board.legal_moves:
                return {"correct": False, "message": f"Illegal move: {attempted_uci} is not legal in this position."}
        except Exception:
            return {"correct": False, "message": f"Invalid move format: {attempted_uci}."}

        conn = get_db()
        cursor = conn.cursor()
        in_txn = False
        try:
            norm_3 = " ".join(norm_fen.split()[:3])
            cursor.execute("BEGIN IMMEDIATE")
            in_txn = True

            cursor.execute(
                """
                SELECT rm.*, COALESCE(rs.name, rm.study_name) as effective_study FROM repertoire_moves rm
                LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 1
                ORDER BY (rm.parent_fen = ?) DESC, rm.is_important DESC, id ASC
                LIMIT 1
                """,
                (repertoire, norm_fen, norm_3, f"{norm_3} %", norm_fen),
            )
            prepared = cursor.fetchone()

            if not prepared:
                conn.rollback()
                in_txn = False
                return {
                    "correct": False,
                    "message": "This position is not yet in your repertoire tree.",
                    "is_in_tree": False,
                }

            prepared = dict(prepared)
            expected_uci = prepared["move_uci"]
            expected_san = prepared["move_san"]
            active_study = (
                (study_filter or "").strip()
                or prepared.get("effective_study")
                or prepared.get("study_name")
                or ""
            )

            is_match = attempted_uci == expected_uci
            now = int(time.time())

            mem_state = infer_move_memory_state(prepared, now=now)
            prev_tp = mem_state["tp"]
            prev_tc = mem_state["tc"]
            prev_lapses = mem_state["lapses"]
            prev_streak = mem_state["streak"]
            prev_diff = mem_state["difficulty"]
            prev_interval = max(1.0, mem_state["stability"])
            last_prac = max(0, int(prepared.get("last_practiced") or 0))
            elapsed_days = max(0.0, (now - last_prac) / 86400.0) if last_prac > 0 else 0.0
            r_now = compute_fsrs_retrievability(last_prac, prev_interval, now=now) if last_prac > 0 else 1.0

            grade = infer_fsrs_grade(is_match, is_retry=bool(is_retry), response_ms=response_ms, prev_tp=prev_tp)

            if not is_match:
                new_tp = min(9223372036854775807, prev_tp + 1)
                new_tc = prev_tc
                new_lapses = min(9223372036854775807, prev_lapses + 1)
                new_diff = (
                    fsrs5_initial_difficulty(GRADE_AGAIN)
                    if prev_tp == 0
                    else fsrs5_next_difficulty(prev_diff, GRADE_AGAIN)
                )
                # FSRS-5 post-lapse stability: veteran cards retain partial stability rather than dropping to 1.0d!
                new_stability = fsrs5_lapse_stability(new_diff, prev_interval, r_now, prev_tc)
                # Always schedule next review within 24h for immediate next-day verification
                next_review_ts = now + 86400

                cursor.execute(
                    """
                    UPDATE repertoire_moves
                    SET times_practiced = ?,
                        interval_days = ?,
                        last_practiced = ?,
                        next_review = ?,
                        consecutive_correct = 0,
                        lapses = ?,
                        difficulty = ?
                    WHERE id = ?
                    """,
                    (new_tp, new_stability, now, next_review_ts, new_lapses, new_diff, prepared["id"]),
                )
                conn.commit()
                in_txn = False

                updated_row = dict(prepared)
                updated_row.update({
                    "times_practiced": new_tp,
                    "times_correct": new_tc,
                    "interval_days": new_stability,
                    "consecutive_correct": 0,
                    "lapses": new_lapses,
                    "difficulty": new_diff,
                })
                mastery = classify_move_mastery(updated_row)
                return {
                    "correct": False,
                    "fsrs_grade": GRADE_AGAIN,
                    "is_retry": bool(is_retry),
                    "prepared_id": prepared["id"],
                    "expected_san": expected_san,
                    "expected_uci": expected_uci,
                    "study_name": active_study,
                    "comment": prepared.get("comment", "") or "",
                    "mastery_status": mastery,
                    "times_practiced": new_tp,
                    "times_correct": new_tc,
                    "consecutive_correct": 0,
                    "lapses": new_lapses,
                    "difficulty": new_diff,
                    "stability": new_stability,
                    "message": f"Not in your repertoire. Your prepared move is {expected_san}.",
                }

            if is_retry:
                new_tp = prev_tp
                new_tc = prev_tc
                new_lapses = prev_lapses
                new_streak = prev_streak
                new_diff = prev_diff
                new_interval = prev_interval
                conn.commit()
                in_txn = False
                mastery = classify_move_mastery(prepared)
            else:
                new_tp = min(9223372036854775807, prev_tp + 1)
                new_tc = min(9223372036854775807, prev_tc + 1)
                new_lapses = prev_lapses
                new_streak = min(9223372036854775807, prev_streak + 1)

                new_diff = (
                    fsrs5_initial_difficulty(grade)
                    if prev_tp == 0
                    else fsrs5_next_difficulty(prev_diff, grade)
                )
                new_interval = fsrs5_recall_stability(
                    difficulty=new_diff,
                    prev_stability=prev_interval,
                    retrievability=r_now,
                    grade=grade,
                    new_streak=new_streak,
                    lapses=new_lapses,
                    elapsed_days=elapsed_days,
                )

                max_future_seconds = int(3650.0 * 86400)
                review_delta = min(max_future_seconds, int(new_interval * 86400))
                next_review_ts = min(9223372036854775807, now + review_delta)

                cursor.execute(
                    """
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
                    """,
                    (new_tp, new_tc, new_interval, now, next_review_ts, new_streak, new_lapses, new_diff, prepared["id"]),
                )
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

            # Opponent continuation selection
            after_fen = prepared.get("fen") or ""
            after_3 = " ".join(after_fen.split()[:3]) if after_fen else ""
            opp_replies = []
            study_scoped = False
            if active_study:
                clean_study = active_study.strip()
                cursor.execute(
                    """
                    SELECT DISTINCT rm.* FROM repertoire_moves rm
                    LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                    LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                    WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 0
                      AND (rs.name = ? OR rm.study_name = ?)
                    """,
                    (repertoire, after_fen, after_3, f"{after_3} %", clean_study, clean_study),
                )
                opp_replies = [dict(r) for r in cursor.fetchall()]
                study_scoped = True

            if not opp_replies:
                study_scoped = False
                cursor.execute(
                    """
                    SELECT * FROM repertoire_moves
                    WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND is_user_move = 0
                    """,
                    (repertoire, after_fen, after_3, f"{after_3} %"),
                )
                opp_replies = [dict(r) for r in cursor.fetchall()]

            opponent_reply = None
            next_expected_uci = None
            next_expected_san = None
            next_opening_name = None
            next_prep = None
            next_study = active_study
            unanswered_branches = 0
            unanswered_branch_sans: List[str] = []

            if opp_replies:
                replies_with_followup = []
                for opp in opp_replies:
                    opp_fen = opp["fen"]
                    opp_fen_3 = " ".join(opp_fen.split()[:3])
                    prep_cand = None
                    if active_study:
                        clean_study = active_study.strip()
                        cursor.execute(
                            """
                            SELECT DISTINCT rm.* FROM repertoire_moves rm
                            LEFT JOIN study_moves sm ON sm.repertoire = rm.repertoire AND sm.parent_fen = rm.parent_fen AND sm.move_uci = rm.move_uci
                            LEFT JOIN repertoire_studies rs ON rs.id = sm.study_id
                            WHERE rm.repertoire = ? AND (rm.parent_fen = ? OR rm.parent_fen = ? OR rm.parent_fen LIKE ?) AND rm.is_user_move = 1
                              AND (rs.name = ? OR rm.study_name = ?)
                            ORDER BY (rm.parent_fen = ?) DESC, rm.is_important DESC, id ASC
                            LIMIT 1
                            """,
                            (repertoire, opp_fen, opp_fen_3, f"{opp_fen_3} %", clean_study, clean_study, opp_fen),
                        )
                        prep_cand = cursor.fetchone()

                    if not prep_cand:
                        cursor.execute(
                            """
                            SELECT * FROM repertoire_moves
                            WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND is_user_move = 1
                            ORDER BY (parent_fen = ?) DESC, is_important DESC, id ASC
                            LIMIT 1
                            """,
                            (repertoire, opp_fen, opp_fen_3, f"{opp_fen_3} %", opp_fen),
                        )
                        prep_cand = cursor.fetchone()

                    if prep_cand:
                        replies_with_followup.append((opp, dict(prep_cand)))
                    else:
                        unanswered_branches += 1
                        unanswered_branch_sans.append(opp.get("move_san") or opp.get("move_uci") or "?")

                if replies_with_followup:
                    def _branch_pedagogical_weight(opp_row: Dict[str, Any], user_followup: Dict[str, Any]) -> float:
                        f_mem = infer_move_memory_state(user_followup, now=now)
                        f_mast = classify_move_mastery(user_followup)
                        f_next_rev = int(user_followup.get("next_review") or 0)
                        f_is_due = f_mem["tp"] > 0 and (
                            (f_next_rev > 0 and f_next_rev <= now)
                            or f_mem["retrievability"] <= FSRS_TARGET_RETENTION
                        )

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

                        deep_fen = user_followup.get("fen") or ""
                        if deep_fen:
                            deep_3 = " ".join(deep_fen.split()[:3])
                            cursor.execute(
                                """
                                SELECT u.* FROM repertoire_moves o
                                JOIN repertoire_moves u ON u.repertoire = o.repertoire
                                  AND (u.parent_fen = o.fen OR u.parent_fen LIKE substr(o.fen, 1, length(o.fen) - 4) || '%')
                                WHERE o.repertoire = ? AND o.is_user_move = 0 AND u.is_user_move = 1
                                  AND (o.parent_fen = ? OR o.parent_fen = ? OR o.parent_fen LIKE ?)
                                LIMIT 6
                                """,
                                (repertoire, deep_fen, deep_3, f"{deep_3} %"),
                            )
                            for d_row in cursor.fetchall():
                                d_mem = infer_move_memory_state(dict(d_row), now=now)
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
                    next_study = (
                        (next_prep.get("study_name") or "").strip()
                        or active_study
                        or next_prep.get("opening_name")
                        or ""
                    )
                    next_opening_name = next_study or next_prep.get("opening_name") or ""
        except Exception:
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
            line_end_reason = (
                LINE_END_UNANSWERED_BRANCHES if unanswered_branches else LINE_END_REPERTOIRE_EXHAUSTED
            )
        else:
            is_line_ended = False
            line_end_reason = None

        grade_label = {GRADE_AGAIN: "again", GRADE_HARD: "hard", GRADE_GOOD: "good", GRADE_EASY: "easy"}.get(grade, "good")

        return {
            "correct": True,
            "fsrs_grade": grade,
            "fsrs_grade_label": grade_label,
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
            "stability": new_interval,
            "difficulty": new_diff,
            "is_important": bool(prepared.get("is_important")),
            "comment": prepared.get("comment", "") or "",
            "arrows": prepared.get("arrows", "") or "",
            "opponent_reply": {
                "san": opponent_reply["move_san"],
                "uci": opponent_reply["move_uci"],
                "fen": opponent_reply["fen"],
            }
            if opponent_reply
            else None,
            "next_expected_uci": next_expected_uci,
            "next_expected_san": next_expected_san,
            "next_opening_name": next_opening_name,
            "next_study_name": next_study,
            "next_is_important": bool(next_prep.get("is_important")) if next_prep else False,
            "next_comment": (next_prep.get("comment", "") or "") if next_prep else "",
            "next_arrows": (next_prep.get("arrows", "") or "") if next_prep else "",
            "is_line_end": is_line_ended,
            "line_position": line_position,
            "line_end_reason": line_end_reason,
            "unanswered_branches": unanswered_branches,
            "unanswered_branch_sans": unanswered_branch_sans[:8],
        }
