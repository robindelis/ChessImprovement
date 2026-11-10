"""
backend/repertoire.py — Repertoire Tree, Studies & PGN Import Manager

Composes `ExplorerMixin`, `GameSyncMixin`, and `TrainerMixin` into the process-wide
`RepertoireManager` singleton while keeping tree CRUD, studies, overlap checks, and PGN import
cleanly isolated here.
"""
import io
import json
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import chess
import chess.pgn
import requests  # kept exported on module for offline test stubbing compatibility

from backend.db import closing_db, get_db, get_setting, set_setting
from backend.evaluator import EngineBusy, EngineUnavailable, get_sync_engine_pool
from backend.explorer import ExplorerMixin, sanitize_explorer_payload
from backend.game_sync import GameSyncMixin
from backend.lichess_client import DEFAULT_PERF_TYPES, LichessClient
from backend.openings import OpeningBook
from backend.trainer import (
    FSRS_DECAY,
    FSRS_DEFAULT_DIFFICULTY,
    FSRS_FACTOR,
    FSRS_MAX_DIFFICULTY,
    FSRS_MIN_DIFFICULTY,
    LINE_END_REPERTOIRE_EXHAUSTED,
    LINE_END_UNANSWERED_BRANCHES,
    LINE_POSITION_AWAITING_REPLY,
    LINE_POSITION_COMPLETED,
    LINE_POSITION_CONTINUES,
    MASTERED_AUDIT_CADENCE,
    SESSION_LAPSE_COOLDOWN_DRILLS,
    TrainerMixin,
    classify_line_position,
    classify_move_mastery,
    compute_fsrs_retrievability,
    infer_fsrs_grade,
    infer_move_memory_state,
)
from backend.utils import (
    EXPLORER_NEGATIVE_TTL_SECONDS,
    MAX_EXPLORER_COUNT,
    MAX_PLAYED_AT_MS,
    MS_EPOCH_THRESHOLD,
    RESULT_ABANDONED,
    RESULT_BLACK,
    RESULT_DRAW,
    RESULT_WHITE,
    UNFINISHED_STATUSES,
    classify_stored_result,
    coerce_count,
    coerce_text,
    extract_player_info,
    normalize_fen,
    normalize_game_outcome,
    normalize_played_at,
)

STOCKFISH_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin",
    "stockfish",
    "stockfish-linux-x86-64-universal",
)

REPLACE_PRUNE_MAX_DEPTH = 1
OVERLAP_INDEX_TTL_SECONDS = 5.0
MAX_IMPORT_MOVES = 200


class RepertoireManager(ExplorerMixin, GameSyncMixin, TrainerMixin):
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self.lichess_client = LichessClient()
        self.opening_book = OpeningBook.get_instance()
        self._user_stats_cache = {}
        self._user_stats_time = 0
        self._explorer_count_memo = {}
        self._seed_lock = threading.Lock()
        self._overlap_index: Dict[str, Tuple[Dict, Dict, float]] = {}
        self._explorer_flight: Dict[Tuple[str, str], threading.Event] = {}
        self._explorer_negative: Dict[Tuple[str, str], float] = {}
        self._explorer_flight_lock = threading.Lock()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @staticmethod
    def _engine_pool():
        return get_sync_engine_pool(STOCKFISH_PATH)

    def close(self):
        self._user_stats_cache = {}
        self._user_stats_time = 0
        if hasattr(self, "_user_stats_by_fen"):
            self._user_stats_by_fen = {}
        self._invalidate_overlap_index()

    def _invalidate_overlap_index(self):
        self._overlap_index = {}

    # ----------------------------------------------------------------------- #
    # Repertoire Tree CRUD
    # ----------------------------------------------------------------------- #

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
        conn: Optional[Any] = None,
    ) -> Dict:
        repertoire = (repertoire or "").lower()
        if repertoire not in ("white", "black"):
            return {"status": "error", "message": f"Invalid repertoire color: {repertoire}. Must be 'white' or 'black'."}

        is_user_move = True if is_user_move is None else bool(is_user_move)

        parts = (parent_fen or "").strip().split()
        if len(parts) < 4 or parts[0].count("/") != 7:
            return {"status": "error", "message": "Invalid FEN string: must contain at least 4 fields"}

        try:
            board = chess.Board(parent_fen)
            if not board.is_valid():
                return {"status": "error", "message": "Invalid FEN string"}
        except Exception as e:
            return {"status": "error", "message": f"Invalid FEN string: {e}"}

        if repertoire == "white":
            if is_user_move and board.turn != chess.WHITE:
                return {"status": "error", "message": "User moves for white repertoire must be on White's turn"}
            if not is_user_move and board.turn != chess.BLACK:
                return {"status": "error", "message": "Opponent moves for white repertoire must be on Black's turn"}
        elif repertoire == "black":
            if is_user_move and board.turn != chess.BLACK:
                return {"status": "error", "message": "User moves for black repertoire must be on Black's turn"}
            if not is_user_move and board.turn != chess.WHITE:
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
        opening_info = self.opening_book.get_opening_for_fen(board.fen()) or {"eco": "", "name": ""}

        own_conn = conn is None
        if own_conn:
            conn = get_db()
        try:
            cursor = conn.cursor()

            if is_user_move:
                norm_parent_3 = " ".join(norm_parent.split()[:3])
                cursor.execute(
                    """
                    SELECT id, parent_fen, fen, move_uci, move_san, study_name FROM repertoire_moves
                    WHERE repertoire = ? AND is_user_move = 1
                      AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?)
                    ORDER BY (parent_fen = ?) DESC, id ASC
                    """,
                    (repertoire, norm_parent, norm_parent_3, f"{norm_parent_3} %", norm_parent),
                )
                existing_user_move = cursor.fetchone()
                if existing_user_move and existing_user_move["move_uci"] != move_uci:
                    if not replace_existing:
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
                    studies_before = self._studies_with_membership(cursor)
                    cursor.execute(
                        """
                        DELETE FROM repertoire_moves
                        WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1
                        """,
                        (repertoire, existing_user_move["parent_fen"]),
                    )
                    cursor.execute(
                        """
                        DELETE FROM study_moves
                        WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?
                        """,
                        (repertoire, existing_user_move["parent_fen"], existing_user_move["move_uci"]),
                    )
                    self._prune_orphaned_children(
                        cursor,
                        repertoire,
                        [existing_user_move["fen"]],
                        max_depth=REPLACE_PRUNE_MAX_DEPTH,
                    )
                    self._drop_emptied_studies(cursor, studies_before)
                elif existing_user_move and existing_user_move["parent_fen"] != norm_parent:
                    return {
                        "status": "success",
                        "code": "already_prepared_at_transposition",
                        "already_present": True,
                        "transposed": True,
                        "message": (
                            f"{existing_user_move['move_san']} is already your prepared move for this "
                            f"position (reached by a different move order). Nothing was duplicated."
                        ),
                        "repertoire": repertoire,
                        "parent_fen": existing_user_move["parent_fen"],
                        "is_user_move": True,
                        "move_uci": existing_user_move["move_uci"],
                        "move_san": existing_user_move["move_san"],
                        "id": existing_user_move["id"],
                        "deleted_moves": 0,
                        "pruned_moves": 0,
                    }

            cursor.execute(
                """
                SELECT is_important, comment, arrows, study_name FROM repertoire_moves
                WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?
                """,
                (repertoire, norm_parent, move_uci),
            )
            existing_meta = cursor.fetchone()

            cursor.execute(
                """
                SELECT s.name FROM study_moves sm
                JOIN repertoire_studies s ON s.id = sm.study_id
                WHERE sm.repertoire = ? AND sm.parent_fen = ? AND sm.move_uci = ?
                """,
                (repertoire, norm_parent, move_uci),
            )
            member_studies = sorted({(r["name"] or "").strip() for r in cursor.fetchall() if (r["name"] or "").strip()})
            cached_study = (existing_meta["study_name"] if existing_meta else "") or ""
            requested_study = (study_name or "").strip()

            final_important = (
                int(is_important)
                if is_important is not None
                else (existing_meta["is_important"] if existing_meta and existing_meta["is_important"] is not None else 0)
            )
            final_comment = (
                comment.strip()
                if comment is not None
                else (existing_meta["comment"] if existing_meta and existing_meta["comment"] else "")
            )
            final_arrows = (
                arrows.strip()
                if arrows is not None
                else (existing_meta["arrows"] if existing_meta and existing_meta["arrows"] else "")
            )
            if requested_study:
                if not cached_study or cached_study == requested_study or not member_studies:
                    final_study = requested_study
                else:
                    final_study = cached_study
            else:
                final_study = cached_study

            now_ts = int(time.time())
            cursor.execute(
                """
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
                """,
                (
                    repertoire,
                    norm_parent,
                    norm_after,
                    move_uci,
                    san,
                    1 if is_user_move else 0,
                    safe_popularity,
                    safe_win_rate,
                    safe_eval,
                    opening_info.get("name", ""),
                    opening_info.get("eco", ""),
                    final_important,
                    final_comment,
                    final_arrows,
                    final_study,
                    now_ts,
                ),
            )

            link_study = requested_study or final_study
            if link_study:
                cursor.execute(
                    "INSERT OR IGNORE INTO repertoire_studies (name, repertoire, description, created_at, updated_at) VALUES (?, ?, '', ?, ?)",
                    (link_study, repertoire, now_ts, now_ts),
                )
                cursor.execute(
                    "SELECT id FROM repertoire_studies WHERE name = ? AND repertoire = ?",
                    (link_study, repertoire),
                )
                st_row = cursor.fetchone()
                if st_row:
                    cursor.execute(
                        "INSERT OR IGNORE INTO study_moves (study_id, repertoire, parent_fen, move_uci) VALUES (?, ?, ?, ?)",
                        (st_row["id"], repertoire, norm_parent, move_uci),
                    )
                    cursor.execute("UPDATE repertoire_studies SET updated_at = ? WHERE id = ?", (now_ts, st_row["id"]))

            if own_conn:
                conn.commit()
        finally:
            if own_conn:
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
            "studies": member_studies + ([final_study] if final_study and final_study not in member_studies else []),
        }

    def delete_repertoire_branch(self, repertoire: str, parent_fen: str, move_uci: str):
        repertoire = (repertoire or "").lower()
        if repertoire not in ("white", "black"):
            return
        parts = (parent_fen or "").strip().split()
        if len(parts) < 4 or parts[0].count("/") != 7:
            return
        norm_parent = normalize_fen(parent_fen)
        norm_3 = " ".join(norm_parent.split()[:3])
        with closing_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, parent_fen, fen FROM repertoire_moves
                WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND move_uci = ?
                ORDER BY (parent_fen = ?) DESC, is_important DESC, id ASC
                LIMIT 1
                """,
                (repertoire, norm_parent, norm_3, f"{norm_3} %", move_uci, norm_parent),
            )
            row = cursor.fetchone()
            if not row:
                return

            child_fen = row["fen"]
            studies_before = self._studies_with_membership(cursor)

            cursor.execute("DELETE FROM repertoire_moves WHERE id = ?", (row["id"],))
            cursor.execute(
                """
                DELETE FROM study_moves
                WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?
                """,
                (repertoire, row["parent_fen"], move_uci),
            )
            self._prune_orphaned_children(cursor, repertoire, [child_fen])
            self._drop_emptied_studies(cursor, studies_before)
            conn.commit()
        self._invalidate_overlap_index()

    @staticmethod
    def _studies_with_membership(cursor) -> set:
        return {r["study_id"] for r in cursor.execute("SELECT DISTINCT study_id FROM study_moves").fetchall()}

    @staticmethod
    def _drop_emptied_studies(cursor, previously_populated) -> int:
        dropped = 0
        for study_id in sorted(previously_populated or ()):
            if cursor.execute("SELECT 1 FROM study_moves WHERE study_id = ? LIMIT 1", (study_id,)).fetchone() is not None:
                continue
            row = cursor.execute("SELECT name, repertoire FROM repertoire_studies WHERE id = ?", (study_id,)).fetchone()
            if row is None:
                continue
            if cursor.execute("SELECT 1 FROM repertoire_moves WHERE study_name = ? LIMIT 1", (row["name"],)).fetchone() is not None:
                continue
            cursor.execute("DELETE FROM repertoire_studies WHERE id = ?", (study_id,))
            dropped += 1
        return dropped

    def _prune_orphaned_children(
        self, cursor, repertoire: str, start_fens: List[str], max_depth: Optional[int] = None
    ) -> int:
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
            if norm_curr == start_norm or curr_3 == start_3:
                continue
            if norm_curr in visited:
                continue
            visited.add(norm_curr)

            cursor.execute(
                """
                SELECT COUNT(*) as parent_count FROM repertoire_moves
                WHERE repertoire = ? AND (fen = ? OR fen = ? OR fen LIKE ?)
                """,
                (repertoire, norm_curr, curr_3, f"{curr_3} %"),
            )
            if cursor.fetchone()["parent_count"] > 0:
                continue

            cursor.execute(
                """
                SELECT fen FROM repertoire_moves
                WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?)
                """,
                (repertoire, norm_curr, curr_3, f"{curr_3} %"),
            )
            next_fens = [r["fen"] for r in cursor.fetchall()]
            cursor.execute(
                """
                DELETE FROM repertoire_moves
                WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?)
                """,
                (repertoire, norm_curr, curr_3, f"{curr_3} %"),
            )
            cursor.execute(
                """
                DELETE FROM study_moves
                WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?)
                """,
                (repertoire, norm_curr, curr_3, f"{curr_3} %"),
            )
            removed += len(next_fens)
            to_delete.extend((f, depth + 1) for f in next_fens)
        return removed

    def clear_repertoire_database(self, repertoire: Optional[str] = None) -> int:
        repertoire_clean = (repertoire or "").lower().strip()
        with closing_db() as conn:
            cursor = conn.cursor()
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
        self._user_stats_cache = {}
        self._user_stats_time = 0
        self._invalidate_overlap_index()
        return count

    # ----------------------------------------------------------------------- #
    # Studies Management
    # ----------------------------------------------------------------------- #

    def get_studies(self, repertoire: Optional[str] = None) -> List[Dict[str, Any]]:
        repertoire = (repertoire or "").lower().strip()
        with closing_db() as conn:
            cursor = conn.cursor()
            if repertoire in ("white", "black"):
                cursor.execute(
                    """
                    SELECT s.id, s.name, s.repertoire, s.description, s.created_at, s.updated_at,
                           COUNT(DISTINCT sm.parent_fen || ':' || sm.move_uci) as total_moves,
                           SUM(CASE WHEN rm.is_user_move = 1 THEN 1 ELSE 0 END) as user_moves
                    FROM repertoire_studies s
                    LEFT JOIN study_moves sm ON sm.study_id = s.id
                    LEFT JOIN repertoire_moves rm ON rm.repertoire = sm.repertoire AND rm.parent_fen = sm.parent_fen AND rm.move_uci = sm.move_uci
                    WHERE s.repertoire = ?
                    GROUP BY s.id
                    ORDER BY s.updated_at DESC, s.name ASC
                    """,
                    (repertoire,),
                )
            else:
                cursor.execute(
                    """
                    SELECT s.id, s.name, s.repertoire, s.description, s.created_at, s.updated_at,
                           COUNT(DISTINCT sm.parent_fen || ':' || sm.move_uci) as total_moves,
                           SUM(CASE WHEN rm.is_user_move = 1 THEN 1 ELSE 0 END) as user_moves
                    FROM repertoire_studies s
                    LEFT JOIN study_moves sm ON sm.study_id = s.id
                    LEFT JOIN repertoire_moves rm ON rm.repertoire = sm.repertoire AND rm.parent_fen = sm.parent_fen AND rm.move_uci = sm.move_uci
                    GROUP BY s.id
                    ORDER BY s.updated_at DESC, s.name ASC
                    """
                )
            rows = [dict(r) for r in cursor.fetchall()]
        for r in rows:
            r["total_moves"] = r["total_moves"] or 0
            r["user_moves"] = r["user_moves"] or 0
        return rows

    def get_study_by_name(self, name: str, repertoire: str) -> Optional[Dict[str, Any]]:
        with closing_db() as conn:
            row = conn.execute(
                "SELECT * FROM repertoire_studies WHERE name = ? AND repertoire = ?",
                (name.strip(), repertoire.lower().strip()),
            ).fetchone()
            return dict(row) if row else None

    def create_study(self, name: str, repertoire: str, description: str = "") -> Dict[str, Any]:
        name = re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", " ", name or "").strip()
        repertoire = (repertoire or "").lower().strip()
        if not name:
            return {"status": "error", "message": "Study name cannot be empty"}
        if repertoire not in ("white", "black"):
            return {"status": "error", "message": "Repertoire must be 'white' or 'black'"}

        now_ts = int(time.time())
        try:
            with closing_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO repertoire_studies (name, repertoire, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, repertoire, description or "", now_ts, now_ts),
                )
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
                        "user_moves": 0,
                    },
                }
        except sqlite3.IntegrityError:
            existing = self.get_study_by_name(name, repertoire)
            return {"status": "exists", "study": existing, "message": f"Study '{name}' already exists."}

    def delete_study(self, study_id: int, delete_moves: bool = False) -> Dict[str, Any]:
        with closing_db() as conn:
            cursor = conn.cursor()
            row = cursor.execute("SELECT * FROM repertoire_studies WHERE id = ?", (study_id,)).fetchone()
            if not row:
                return {"status": "error", "message": "Study not found"}

            study = dict(row)
            study_name = study["name"]

            cursor.execute(
                "SELECT sm.repertoire, sm.parent_fen, sm.move_uci FROM study_moves sm WHERE sm.study_id = ?",
                (study_id,),
            )
            linked = [(r["repertoire"], r["parent_fen"], r["move_uci"]) for r in cursor.fetchall()]

            deleted_moves = 0
            kept_moves = 0
            orphaned_fens: Dict[str, List[str]] = defaultdict(list)

            for rep, p_fen, uci in linked:
                move_row = cursor.execute(
                    "SELECT fen, study_name FROM repertoire_moves WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?",
                    (rep, p_fen, uci),
                ).fetchone()

                cursor.execute(
                    """
                    SELECT s.name FROM study_moves sm
                    JOIN repertoire_studies s ON s.id = sm.study_id
                    WHERE sm.repertoire = ? AND sm.parent_fen = ? AND sm.move_uci = ? AND sm.study_id != ?
                    """,
                    (rep, p_fen, uci, study_id),
                )
                other_studies = [r["name"] for r in cursor.fetchall()]
                column_owner = (move_row["study_name"] or "").strip() if move_row else ""

                if delete_moves and move_row is not None and not other_studies and (
                    not column_owner or column_owner == study_name
                ):
                    if move_row["fen"]:
                        orphaned_fens[rep].append(move_row["fen"])
                    cursor.execute(
                        "DELETE FROM repertoire_moves WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?",
                        (rep, p_fen, uci),
                    )
                    deleted_moves += 1
                    continue

                kept_moves += 1
                if move_row is not None and column_owner == study_name:
                    cursor.execute(
                        "UPDATE repertoire_moves SET study_name = ? WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?",
                        (other_studies[0] if other_studies else "", rep, p_fen, uci),
                    )

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
            "kept_moves": kept_moves,
        }

    def group_subtree_into_study(
        self,
        repertoire: str,
        start_fen: str,
        study_name: str,
        include_path_from_root: bool = True,
        path_uci: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        repertoire = (repertoire or "").lower().strip()
        if repertoire not in ("white", "black"):
            return {"status": "error", "message": "Repertoire must be 'white' or 'black'"}

        cleaned_study = re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", " ", study_name or "").strip()
        if not cleaned_study:
            return {"status": "error", "message": "Study name cannot be empty"}

        try:
            chess.Board(start_fen)
        except Exception:
            return {"status": "error", "message": "Invalid starting FEN position"}

        norm_start = normalize_fen(start_fen)
        norm_start_3 = " ".join(norm_start.split()[:3])

        study_res = self.create_study(cleaned_study, repertoire)
        if study_res.get("status") not in ("success", "exists") or not study_res.get("study"):
            return {"status": "error", "message": study_res.get("message", "Failed to create or access study")}
        study_id = study_res["study"]["id"]

        with closing_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, parent_fen, fen, move_uci, move_san, is_user_move, study_name FROM repertoire_moves WHERE repertoire = ?",
                (repertoire,),
            )
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
                            found_path = normalize_fen(b.fen()) == norm_start or " ".join(normalize_fen(b.fen()).split()[:3]) == norm_start_3
                        except Exception:
                            found_path = False

                    if not found_path:
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

            now_ts = int(time.time())
            for m in prelude_moves + descendant_moves:
                p_norm = normalize_fen(m["parent_fen"])
                cursor.execute(
                    "INSERT OR IGNORE INTO study_moves (study_id, repertoire, parent_fen, move_uci) VALUES (?, ?, ?, ?)",
                    (study_id, repertoire, p_norm, m["move_uci"]),
                )

            for m in descendant_moves:
                p_norm = normalize_fen(m["parent_fen"])
                cursor.execute(
                    "UPDATE repertoire_moves SET study_name = ? WHERE repertoire = ? AND parent_fen = ? AND move_uci = ?",
                    (cleaned_study, repertoire, p_norm, m["move_uci"]),
                )

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
            "total_study_moves": len(descendant_moves) + len(prelude_moves),
        }

    def update_move_metadata(
        self,
        repertoire: str,
        parent_fen: str,
        move_uci: str,
        is_important: Optional[bool] = None,
        comment: Optional[str] = None,
        arrows: Optional[str] = None,
        study_name: Optional[str] = None,
    ) -> Dict:
        repertoire = (repertoire or "").lower().strip()
        if repertoire not in ("white", "black"):
            return {"status": "error", "message": "Invalid repertoire"}
        norm_parent = normalize_fen(parent_fen)
        norm_3 = " ".join(norm_parent.split()[:3])

        with closing_db() as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                """
                SELECT * FROM repertoire_moves
                WHERE repertoire = ? AND (parent_fen = ? OR parent_fen = ? OR parent_fen LIKE ?) AND move_uci = ?
                ORDER BY (parent_fen = ?) DESC, is_important DESC, id ASC
                LIMIT 1
                """,
                (repertoire, norm_parent, norm_3, f"{norm_3} %", move_uci, norm_parent),
            ).fetchone()
            if not row:
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
                other_members = cursor.execute(
                    """
                    SELECT s.name FROM study_moves sm JOIN repertoire_studies s ON s.id = sm.study_id
                    WHERE sm.repertoire = ? AND sm.parent_fen = ? AND sm.move_uci = ? AND s.name != ?
                    """,
                    (repertoire, row["parent_fen"], move_uci, st_name),
                ).fetchall()
                cached = (row["study_name"] or "").strip()
                if not other_members or not cached or cached == st_name:
                    updates.append("study_name = ?")
                    params.append(st_name)

            if updates:
                params.append(row["id"])
                cursor.execute(f"UPDATE repertoire_moves SET {', '.join(updates)} WHERE id = ?", params)

            if st_name:
                now_ts = int(time.time())
                cursor.execute(
                    "INSERT OR IGNORE INTO repertoire_studies (name, repertoire, description, created_at, updated_at) VALUES (?, ?, '', ?, ?)",
                    (st_name, repertoire, now_ts, now_ts),
                )
                st_row = cursor.execute(
                    "SELECT id FROM repertoire_studies WHERE name = ? AND repertoire = ?",
                    (st_name, repertoire),
                ).fetchone()
                if st_row:
                    cursor.execute(
                        "INSERT OR IGNORE INTO study_moves (study_id, repertoire, parent_fen, move_uci) VALUES (?, ?, ?, ?)",
                        (st_row["id"], repertoire, row["parent_fen"], move_uci),
                    )
                    cursor.execute("UPDATE repertoire_studies SET updated_at = ? WHERE id = ?", (now_ts, st_row["id"]))

            conn.commit()
            updated_row = dict(cursor.execute("SELECT * FROM repertoire_moves WHERE id = ?", (row["id"],)).fetchone())
            member_studies = sorted({
                (r["name"] or "").strip()
                for r in cursor.execute(
                    """
                    SELECT s.name FROM study_moves sm JOIN repertoire_studies s ON s.id = sm.study_id
                    WHERE sm.repertoire = ? AND sm.parent_fen = ? AND sm.move_uci = ?
                    """,
                    (repertoire, row["parent_fen"], move_uci),
                ).fetchall()
                if (r["name"] or "").strip()
            })

        self._invalidate_overlap_index()
        return {
            "status": "success",
            "move": updated_row,
            "is_important": updated_row.get("is_important", 0),
            "study_name": updated_row.get("study_name", "") or "",
            "studies": member_studies,
            "comment": updated_row.get("comment", "") or "",
            "arrows": updated_row.get("arrows", "") or "",
        }

    # ----------------------------------------------------------------------- #
    # Overlap Detection & Whole-Line Import
    # ----------------------------------------------------------------------- #

    def _load_overlap_index(self, repertoire: str) -> Tuple[Dict[str, List[Dict]], Dict[str, List[Dict]]]:
        cached = self._overlap_index.get(repertoire)
        now = time.time()
        if cached is not None and (now - cached[2]) < OVERLAP_INDEX_TTL_SECONDS:
            return cached[0], cached[1]

        with closing_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT parent_fen, fen, move_uci, move_san, is_user_move, opening_name, eco
                FROM repertoire_moves
                WHERE repertoire = ?
                ORDER BY id ASC
                """,
                (repertoire,),
            )
            db_rows = [dict(r) for r in cursor.fetchall()]

        moves_by_parent: Dict[str, List[Dict]] = {}
        moves_by_parent_3: Dict[str, List[Dict]] = {}
        for r in db_rows:
            p = r["parent_fen"]
            moves_by_parent.setdefault(p, []).append(r)
            moves_by_parent_3.setdefault(" ".join(p.split()[:3]), []).append(r)

        self._overlap_index[repertoire] = (moves_by_parent, moves_by_parent_3, now)
        return moves_by_parent, moves_by_parent_3

    def check_line_overlap(self, repertoire: str, moves: List[Dict]) -> Dict:
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
                "message": "No moves provided to check.",
            }

        moves_by_parent, moves_by_parent_3 = self._load_overlap_index(repertoire)
        return self._evaluate_line_against_index(repertoire, moves, moves_by_parent, moves_by_parent_3)

    def _evaluate_line_against_index(
        self,
        repertoire: str,
        moves: List[Dict],
        moves_by_parent: Dict[str, List[Dict]],
        moves_by_parent_3: Dict[str, List[Dict]],
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

            is_user_turn = (repertoire == "white" and curr_board.turn == chess.WHITE) or (
                repertoire == "black" and curr_board.turn == chess.BLACK
            )

            matching_db_move = None
            existing_at_parent = moves_by_parent.get(norm_before) or moves_by_parent_3.get(norm_before_3) or []

            if still_matching:
                for em in existing_at_parent:
                    if (uci and em.get("move_uci") == uci) or (san and em.get("move_san") == san):
                        matching_db_move = em
                        break

                if matching_db_move:
                    matched_plies += 1
                    matched_moves.append({
                        "ply": idx + 1,
                        "uci": uci or matching_db_move.get("move_uci"),
                        "san": san or matching_db_move.get("move_san") or uci,
                        "is_user_move": is_user_turn,
                    })
                    last_matched_fen = matching_db_move.get("fen")
                else:
                    still_matching = False
                    if existing_at_parent:
                        if is_user_turn:
                            existing_user_move = next(
                                (x for x in existing_at_parent if x.get("is_user_move")),
                                existing_at_parent[0],
                            )
                            divergence_info = {
                                "ply": idx + 1,
                                "type": "user_move_alternative",
                                "is_user_turn": True,
                                "existing_uci": existing_user_move.get("move_uci"),
                                "existing_san": existing_user_move.get("move_san"),
                                "new_uci": uci,
                                "new_san": san or uci,
                            }
                        else:
                            divergence_info = {
                                "ply": idx + 1,
                                "type": "opponent_branch",
                                "is_user_turn": False,
                                "existing_moves": [x.get("move_san") or x.get("move_uci") for x in existing_at_parent],
                                "new_uci": uci,
                                "new_san": san or uci,
                            }
                    new_moves.append({"ply": idx + 1, "uci": uci, "san": san or uci, "is_user_move": is_user_turn})
            else:
                new_moves.append({"ply": idx + 1, "uci": uci, "san": san or uci, "is_user_move": is_user_turn})

            if move_obj and move_obj in curr_board.legal_moves:
                curr_board.push(move_obj)
            elif uci:
                try:
                    curr_board.push(chess.Move.from_uci(uci))
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
                return max(get_max_depth(c["fen"], depth + 1, visited) for c in children)

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
                    f"Already present in {rep_cap} repertoire! All {total_plies} plies are in your repertoire tree "
                    f"(which extends {diff} {diff_word} deeper to ply {repertoire_deepest_ply})."
                )
            else:
                message = f"Already present in {rep_cap} repertoire! All {total_plies} plies are already in your opening tree."
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
                    f"({matched_plies}/{total_plies} plies exist). {new_cnt} new {move_word} ({sample_new}) will extend this line."
                )
        else:
            move_word = "move" if total_plies == 1 else "moves"
            if divergence_info and divergence_info.get("type") == "user_move_alternative":
                exist_san = divergence_info.get("existing_san") or divergence_info.get("existing_uci")
                new_san = divergence_info.get("new_san") or divergence_info.get("new_uci")
                message = f"At move 1, {new_san} replaces your existing prepared {exist_san} for {rep_cap}."
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
            "message": message,
        }

    def check_lines_overlap(self, repertoire: str, lines: List[Dict]) -> List[Dict]:
        repertoire = (repertoire or "white").lower().strip()
        if repertoire not in ("white", "black"):
            repertoire = "white"

        moves_by_parent, moves_by_parent_3 = self._load_overlap_index(repertoire)
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
                is_user_ply = (board.turn == chess.WHITE) == (repertoire == "white")
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
                            "line_id": (
                                lines[other_idx].get("id") or lines[other_idx].get("idx")
                                if isinstance(lines[other_idx], dict)
                                else other_idx
                            ),
                            "this_line_uci": mine[parent],
                            "other_line_uci": uci,
                        })
            res["batch_conflicts"] = siblings
            results.append((depth_of.get(idx, 0), first_position.get(idx, ""), idx, res))

        results.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in results]

    def import_line_to_repertoire(
        self, repertoire: str, moves: List[Dict], study_name: Optional[str] = None
    ) -> Dict:
        """Imports a sequence of board moves directly into the user's repertoire tree."""
        repertoire = (repertoire or "").lower().strip()
        if repertoire not in ("white", "black"):
            raise ValueError(f"Invalid repertoire color: {repertoire}. Must be 'white' or 'black'.")
        if not isinstance(moves, list) or not moves:
            raise ValueError("No moves provided to import.")
        if len(moves) > MAX_IMPORT_MOVES:
            raise ValueError(f"Too many moves in one import ({len(moves)}); the limit is {MAX_IMPORT_MOVES} plies.")

        overlap = self.check_line_overlap(repertoire=repertoire, moves=moves)
        is_already_present = overlap.get("status") == "already_present"
        imported_count = 0
        inserted_count = 0
        errors = []
        seen_moves = set()

        if not is_already_present:
            batch_user_parents = set()
            rejected_fens = set()
            for m in moves:
                if not isinstance(m, dict):
                    errors.append("Move entry is not an object.")
                    continue
                parent_fen = m.get("fen_before")
                move_uci = m.get("uci")
                san = m.get("san")
                if not parent_fen or (not move_uci and not san):
                    errors.append("Move is missing 'fen_before' and 'uci'/'san'.")
                    continue

                try:
                    board = chess.Board(parent_fen)
                except Exception as e:
                    errors.append(f"Invalid parent position: {e}")
                    continue

                if not move_uci:
                    try:
                        parsed = board.parse_san(str(san).strip())
                    except Exception as e:
                        errors.append(f"Could not resolve SAN {san!r}: {e}")
                        continue
                    if parsed not in board.legal_moves:
                        errors.append(f"Illegal move for the side to move: {san}")
                        continue
                    move_uci = parsed.uci()

                norm_parent = normalize_fen(board.fen())
                pair = (norm_parent, move_uci)
                if pair in seen_moves:
                    continue
                seen_moves.add(pair)

                if norm_parent in rejected_fens:
                    try:
                        b_skip = board.copy()
                        b_skip.push(chess.Move.from_uci(move_uci))
                        rejected_fens.add(normalize_fen(b_skip.fen()))
                    except Exception:
                        pass
                    continue

                with closing_db() as conn:
                    existed = (
                        conn.execute(
                            "SELECT 1 FROM repertoire_moves WHERE repertoire=? AND parent_fen=? AND move_uci=? LIMIT 1",
                            (repertoire, pair[0], move_uci),
                        ).fetchone()
                        is not None
                    )

                try:
                    is_user_move = (board.turn == chess.WHITE) if repertoire == "white" else (board.turn == chess.BLACK)
                    replace_first = False
                    if is_user_move and norm_parent not in batch_user_parents:
                        replace_first = True
                        batch_user_parents.add(norm_parent)

                    res = self.add_repertoire_move(
                        repertoire=repertoire,
                        parent_fen=parent_fen,
                        move_uci=move_uci,
                        is_user_move=is_user_move,
                        popularity_pct=100.0,
                        win_rate=55.0,
                        eval_score=0,
                        is_important=m.get("is_important"),
                        comment=m.get("comment"),
                        arrows=m.get("arrows"),
                        study_name=study_name,
                        replace_existing=replace_first,
                    )
                    if res.get("status") == "success":
                        imported_count += 1
                        if not existed:
                            inserted_count += 1
                    else:
                        try:
                            b_rej = board.copy()
                            b_rej.push(chess.Move.from_uci(move_uci))
                            rejected_fens.add(normalize_fen(b_rej.fen()))
                        except Exception:
                            pass
                        errors.append(res.get("message", "Unknown error"))
                except Exception as e:
                    errors.append(str(e))

        if imported_count:
            for m in reversed(moves):
                if not isinstance(m, dict):
                    continue
                op_name = study_name or m.get("opening_name")
                if op_name:
                    set_setting("recent_learned_opening", json.dumps({"name": op_name, "eco": m.get("eco", "")}))
                    break

        msg = overlap.get("message") or "Import completed."
        if is_already_present:
            msg = f"{msg} (No changes needed)"
            status = "already_present"
        elif imported_count:
            status = "success"
            msg = f"✓ {msg}" if overlap.get("status") == "partially_present" else f"✓ Added {imported_count} moves to {repertoire.capitalize()} repertoire."
        else:
            status = "error"
            msg = "✗ No moves were stored: " + ("; ".join(errors[:3]) if errors else "invalid move list")

        return {
            "status": status,
            "already_present": is_already_present,
            "overlap_status": overlap.get("status", "new"),
            "total_moves": overlap.get("total_plies", len(moves)),
            "matched_count": overlap.get("matched_plies", 0),
            "new_count": overlap.get("new_plies", 0),
            "imported_count": imported_count,
            "inserted_count": inserted_count,
            "message": msg,
            "overlap_details": overlap,
            "repertoire": repertoire,
            "study_name": study_name or "",
            "errors": errors[:3] if errors else [],
        }

    # ----------------------------------------------------------------------- #
    # Full Tree & Coverage Analysis
    # ----------------------------------------------------------------------- #

    def get_repertoire_tree(
        self,
        repertoire: str = "white",
        gap_threshold: float = 5.0,
        min_games: int = 30,
        include_patched: bool = False,
    ) -> Dict:
        repertoire = (repertoire or "white").lower()
        if repertoire not in ("white", "black"):
            repertoire = "white"

        with closing_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM repertoire_moves WHERE repertoire = ? ORDER BY id ASC", (repertoire,))
            moves = [dict(r) for r in cursor.fetchall()]

        tree_by_parent = {}
        for m in moves:
            p = m["parent_fen"]
            tree_by_parent.setdefault(p, []).append(m)
            p3 = " ".join(p.split()[:3])
            if p3 != p:
                lst3 = tree_by_parent.setdefault(p3, [])
                if m not in lst3:
                    lst3.append(m)

        gaps = []
        is_user_white = repertoire == "white"

        opp_positions = []
        if not is_user_white and moves:
            opp_positions.append((normalize_fen(chess.STARTING_FEN), "Starting Position", ""))

        for m in moves:
            try:
                board = chess.Board(m["fen"])
            except Exception:
                continue
            is_opponent_turn = (board.turn == chess.BLACK and is_user_white) or (
                board.turn == chess.WHITE and not is_user_white
            )
            if is_opponent_turn:
                opp_positions.append((m["fen"], m.get("opening_name") or "Repertoire Line", m.get("eco", "")))

        cand_cache: Dict[str, Dict] = {}
        unavailable_positions: List[str] = []
        total_opp_weight = 0.0
        covered_opp_weight = 0.0

        user_move_parents3 = {
            " ".join((m.get("parent_fen") or "").split()[:3])
            for m in moves
            if m.get("is_user_move")
        }
        unanswered_branch_count = 0
        counted_positions = set()

        for pos_fen, op_name, eco in opp_positions:
            if pos_fen not in cand_cache:
                try:
                    cand_cache[pos_fen] = self.get_candidate_moves_2000(pos_fen, color=repertoire)
                except Exception:
                    cand_cache[pos_fen] = {
                        "moves": [],
                        "degraded": True,
                        "error": "explorer_unavailable",
                        "total_games": None,
                    }
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
                    key = (pos_fen, uci)
                    if key not in counted_positions:
                        counted_positions.add(key)
                        unanswered_branch_count += 1

            for opp_m in cand_moves:
                if not isinstance(opp_m, dict):
                    continue
                opp_san = opp_m.get("san") or ""
                if not opp_san:
                    continue
                pop_pct = float(opp_m.get("popularity_pct") or 0.0)
                cand_uci = opp_m.get("uci", "")
                total_opp_weight += pop_pct
                if cand_uci in covered_replies:
                    covered_opp_weight += pop_pct

                if pop_pct >= gap_threshold and cand_uci not in covered_replies:
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
        game_gaps_data = self.get_game_gaps(repertoire, min_games=min_games, include_patched=include_patched)

        result = {
            "repertoire": repertoire,
            "total_moves": len(moves),
            "unanswered_branches": unanswered_branch_count,
            "coverage_pct": min(100.0, max(0.0, coverage_pct)),
            "gap_threshold": gap_threshold,
            "opening_threshold": min_games,
            "gaps": sorted(gaps, key=lambda x: float(x.get("popularity_pct") or 0.0), reverse=True)[:20],
            "game_gaps": game_gaps_data,
            "moves_by_parent": tree_by_parent,
        }
        if unavailable_positions:
            result["coverage_degraded"] = True
            result["coverage_positions_checked"] = len(opp_positions) - len(unavailable_positions)
            result["coverage_positions_total"] = len(opp_positions)
            result["unavailable_positions"] = unavailable_positions[:20]
            result["error"] = "explorer_unavailable"
        return result

    # ----------------------------------------------------------------------- #
    # Seed & PGN Repertoire Import
    # ----------------------------------------------------------------------- #

    def seed_default_repertoires(self, force: bool = False) -> Dict[str, Any]:
        with self._seed_lock:
            with closing_db() as conn:
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

            if count > 0 and not force:
                return {
                    "status": "skipped",
                    "message": "Repertoire already contains moves. Seeding skipped to protect user data.",
                    "existing_moves": count,
                }

            white_lines = [
                ("Italian Game", ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "c2c3", "g8f6", "d2d3", "d7d6", "e1g1", "e8g8", "b1d2", "a7a6", "f1e1", "c8e6", "c4b3", "f8e8", "d2f1"]),
                ("Italian Game", ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "d2d3", "f8c5", "c2c3", "d7d6", "e1g1", "e8g8"]),
                ("Open Sicilian", ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4", "g8f6", "b1c3"]),
                ("French Defense", ["e2e4", "e7e6", "d2d4", "d7d5", "b1c3", "g8f6", "e4e5", "f6d7"]),
            ]
            black_lines = [
                ("Sicilian Dragon", ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4", "g8f6", "b1c3", "g7g6", "c1e3", "f8g7", "f2f3", "e8g8", "d1d2", "b8c6", "f1c4", "c8d7"]),
                ("King's Indian Defense", ["d2d4", "g8f6", "c2c4", "g7g6", "b1c3", "f8g7", "e2e4", "d7d6", "g1f3", "e8g8", "f1e2", "e7e5", "e1g1", "b8c6", "d4d5", "c6e7"]),
            ]

            plan: List[Tuple[str, str, str, str, bool]] = []
            conflicts: List[str] = []
            preserved: List[str] = []
            lines_total = 0
            lines_skipped = 0

            with closing_db() as probe:
                probe_cursor = probe.cursor()
                for repertoire, parity, seed_lines in (("white", 0, white_lines), ("black", 1, black_lines)):
                    for study_name, line in seed_lines:
                        lines_total += 1
                        b = chess.Board()
                        abandoned = False
                        for ply, uci in enumerate(line):
                            p_fen = b.fen()
                            is_user = ply % 2 == parity
                            if is_user:
                                existing = probe_cursor.execute(
                                    "SELECT move_uci FROM repertoire_moves WHERE repertoire = ? AND parent_fen = ? AND is_user_move = 1",
                                    (repertoire, normalize_fen(p_fen)),
                                ).fetchone()
                                if existing:
                                    if existing["move_uci"] != uci:
                                        conflicts.append(
                                            f"{repertoire}: {study_name} abandoned at {p_fen} "
                                            f"(user prepared {existing['move_uci']}, seed wanted {uci})"
                                        )
                                        abandoned = True
                                        break
                                    preserved.append(f"{repertoire}: {study_name} kept user move {uci} at {p_fen}")
                                    b.push(chess.Move.from_uci(uci))
                                    continue
                            plan.append((repertoire, study_name, p_fen, uci, is_user))
                            b.push(chess.Move.from_uci(uci))
                        if abandoned:
                            lines_skipped += 1

            seeded = 0
            with closing_db() as write_conn:
                for repertoire, study_name, p_fen, uci, is_user in plan:
                    res = self.add_repertoire_move(
                        repertoire,
                        p_fen,
                        uci,
                        is_user_move=is_user,
                        study_name=study_name,
                        replace_existing=False,
                        conn=write_conn,
                    )
                    if res.get("status") == "success":
                        seeded += 1
                write_conn.commit()

            return {
                "status": "success",
                "message": f"Seeded {seeded} move(s) from {lines_total - lines_skipped}/{lines_total} example line(s).",
                "moves_added": seeded,
                "lines_total": lines_total,
                "lines_skipped": lines_skipped,
                "user_moves_preserved": len(preserved),
                "conflicts": conflicts,
            }

    @staticmethod
    def _detect_pgn_repertoire_color(games: List[Any]) -> str:
        for game in games:
            event = (game.headers.get("Event") or "").lower()
            if "white" in event:
                return "white"
            if "black" in event:
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
        if white_branch_points > black_branch_points:
            return "black"
        return "white"

    def preview_pgn_repertoire(self, pgn_text: str) -> Dict[str, Any]:
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

        if clean_repertoire:
            self.clear_repertoire_database(rep_clean)

        imported_count = 0
        user_count = 0
        opp_count = 0
        studies_created = set()
        conflicts = []
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
            "message": f"Imported {imported_count} moves ({user_count} user, {opp_count} opponent) into {rep_clean.capitalize()} repertoire.",
        }
