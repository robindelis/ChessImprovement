"""2000+ Lichess Opening Explorer cache, Stockfish candidate scoring, and personal threat stats."""
import json
import math
import re
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import chess
import chess.engine
import requests

from backend.db import closing_db
from backend.evaluator import EngineBusy, EngineUnavailable
from backend.utils import (
    EXPLORER_NEGATIVE_TTL_SECONDS,
    RESULT_ABANDONED,
    RESULT_DRAW,
    classify_stored_result,
    coerce_count,
    coerce_text,
    normalize_fen,
    number_is_unusable,
)


def sanitize_explorer_payload(payload: Any, is_white_turn: Optional[bool] = None) -> Optional[Dict]:
    """Validate + coerce one 2000+ explorer payload (raw reply or cached formatted row)."""
    if not isinstance(payload, dict):
        return None

    raw_moves = payload.get("moves", [])
    if raw_moves is None:
        raw_moves = []
    if not isinstance(raw_moves, list):
        return None

    if "total_games" in payload:
        if number_is_unusable(payload.get("total_games")):
            return None
        total_games = coerce_count(payload.get("total_games"))
    else:
        total_games = (
            coerce_count(payload.get("white"))
            + coerce_count(payload.get("draws"))
            + coerce_count(payload.get("black"))
        )

    moves: List[Dict[str, Any]] = []
    for entry in raw_moves:
        if not isinstance(entry, dict):
            continue
        san = coerce_text(entry.get("san"), "", 32)
        uci = coerce_text(entry.get("uci"), "", 8)
        if not san and not uci:
            continue
        if any(number_is_unusable(entry.get(k)) for k in ("white", "draws", "black", "games")):
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
            win_rate = float(coerce_count(entry.get("win_rate"), 0.0))
        else:
            win_rate = w_pct if is_white_turn else b_pct

        rating = entry.get("averageRating", entry.get("average_rating", 2000))
        if number_is_unusable(rating):
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
            try:
                eval_value = float(entry.get("eval"))
            except (TypeError, ValueError):
                eval_value = None
            if eval_value is not None and math.isfinite(eval_value):
                move["eval"] = int(eval_value)
        moves.append(move)

    if raw_moves and not moves:
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


class ExplorerMixin:
    """2000+ Lichess Explorer queries, caching, candidate Stockfish evals, and personal threat stats."""

    def get_user_position_stats(self, color: str = "white") -> Dict[Tuple[str, str], Dict]:
        """Indexes user's games -> {(norm_fen, move_uci): {games, wins, draws, losses, win_rate, threat_score}}."""
        color = (color or "white").lower()
        if color not in ("white", "black"):
            color = "white"
        now = time.time()
        if color in self._user_stats_cache and (now - self._user_stats_time < 60):
            return self._user_stats_cache[color]

        with closing_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, user_color, result, moves_san FROM games WHERE user_color = ?", (color,))
            games = cursor.fetchall()

        stats = defaultdict(lambda: {"games": 0, "wins": 0, "draws": 0, "losses": 0})

        for g in games:
            moves_san = g["moves_san"]
            if not moves_san:
                continue
            u_color = (g["user_color"] or "white").strip().lower()
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
            # Threat Score invariant: N * (L^2 + 0.02)
            threat = round(g_cnt * ((loss_rate ** 2) + 0.02), 2)
            result[(p_fen, uci)] = {
                "games": g_cnt,
                "wins": wins,
                "draws": draws,
                "losses": losses,
                "win_rate": win_pct,
                "threat_score": threat,
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

    def _read_explorer_cache(self, norm_fen: str, ratings: str) -> Optional[Dict]:
        with closing_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT data_json, updated_at FROM explorer_cache WHERE fen = ? AND ratings = ?",
                (norm_fen, ratings),
            )
            row = cursor.fetchone()
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
        return sanitize_explorer_payload(parsed)

    def _fetch_explorer(self, fen: str, norm_fen: str, ratings: str) -> Optional[Dict]:
        url = "https://explorer.lichess.org/lichess"
        params = {"fen": fen, "ratings": ratings, "speeds": "blitz,rapid,classical"}
        try:
            resp = requests.get(url, params=params, headers=dict(self.lichess_client.headers), timeout=10)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            return sanitize_explorer_payload(resp.json())
        except Exception:
            return None

    def _store_explorer_payload(self, norm_fen: str, ratings: str, fen: str, payload: Dict) -> None:
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
            with closing_db() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO explorer_cache (fen, ratings, data_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (norm_fen, ratings, json.dumps(result_payload), int(time.time())),
                )
                conn.commit()
        except Exception:
            pass

    def _explorer_single_flight(self, fen: str, ratings: str, producer) -> Tuple[bool, Any]:
        key = (normalize_fen(fen), ratings)
        with self._explorer_flight_lock:
            event = self._explorer_flight.get(key)
            owner = event is None
            if owner:
                event = threading.Event()
                self._explorer_flight[key] = event
        if not owner:
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
        self.get_explorer_game_count(fen, ratings)
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
        return (
            coerce_count(data.get("white"))
            + coerce_count(data.get("draws"))
            + coerce_count(data.get("black"))
        )

    def get_candidate_moves_2000(
        self,
        fen: str,
        ratings: str = "2000,2200,2500",
        color: str = "white",
        evaluate: bool = True,
    ) -> Dict:
        """Queries 2000+ Lichess explorer (cached, single-flight), evaluates top moves, and attaches personal stats."""
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

        cached_payload = self._read_explorer_cache(norm_fen, ratings)
        if cached_payload is not None:
            cached_payload = sanitize_explorer_payload(cached_payload, is_white_turn) or cached_payload
            if (cached_payload.get("moves") or cached_payload.get("total_games") == 0) is False:
                cached_payload = None

        degraded = False
        error_code: Optional[str] = None

        if cached_payload is not None:
            if cached_payload.get("eval_convention") != "white":
                parts = fen.strip().split()
                if len(parts) >= 2 and parts[1] == "b":
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
            def produce():
                recheck = self._read_explorer_cache(norm_fen, ratings)
                if recheck is not None:
                    return recheck
                fetched_inner = self._fetch_explorer(fen, norm_fen, ratings)
                if fetched_inner is None:
                    self._note_explorer_failure(norm_fen, ratings)
                    return None
                self._clear_explorer_backoff(norm_fen, ratings)
                payload = sanitize_explorer_payload(fetched_inner, is_white_turn) or {"total_games": 0, "moves": []}
                for m in payload.get("moves", []):
                    if not m.get("uci") and m.get("san"):
                        try:
                            m["uci"] = board.parse_san(m["san"]).uci()
                        except Exception:
                            pass
                self._store_explorer_payload(norm_fen, ratings, fen, payload)
                if evaluate:
                    self._evaluate_candidate_moves(board, payload.get("moves", []))
                    self._store_explorer_payload(norm_fen, ratings, fen, payload)
                return payload

            if self._explorer_backoff_active(norm_fen, ratings):
                fetched = {"unavailable": True}
            else:
                _owner, fetched = self._explorer_single_flight(fen, ratings, produce)
                if fetched is None:
                    re_read = self._read_explorer_cache(norm_fen, ratings)
                    fetched = re_read if re_read is not None else {"unavailable": True}

            if isinstance(fetched, dict) and fetched.get("unavailable"):
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
                    "threat_score": 0.0,
                }
            moves_with_stats.append(m_copy)

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
                            "my_stats": my_st,
                        })
                    except Exception:
                        pass
        except Exception:
            pass

        moves_with_stats.sort(
            key=lambda x: (
                x.get("my_stats", {}).get("threat_score", 0.0),
                float(x.get("popularity_pct") or 0.0),
                x.get("games", 0),
            ),
            reverse=True,
        )

        result = {
            "fen": fen,
            "total_games": total_games,
            "white_total": white_total,
            "draws_total": draws_total,
            "black_total": black_total,
            "user_games_total": self.get_total_games_count(),
            "moves": moves_with_stats,
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
        depth: int = 10,
    ) -> Optional[str]:
        targets = [c for c in (candidate_moves or []) if c.get("uci")][:max_moves]
        if not targets:
            return None

        pool = self._engine_pool()
        try:
            engine = pool.acquire()
        except (EngineBusy, EngineUnavailable, FileNotFoundError) as exc:
            return str(exc)

        healthy = True
        try:
            for cand in targets:
                try:
                    test_b = board.copy()
                    test_b.push(chess.Move.from_uci(cand["uci"]))
                    info = engine.analyse(test_b, chess.engine.Limit(depth=depth))
                    cand["eval"] = info["score"].white().score(mate_score=10000)
                except Exception:
                    cand["eval"] = None
        except BaseException:
            healthy = False
            raise
        finally:
            if healthy:
                pool.release(engine)
            else:
                pool.discard(engine)
        return None
