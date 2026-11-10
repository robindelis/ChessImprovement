"""Lichess game ingestion, SSE progress streaming, game-opening import, and blunder gaps."""
import asyncio
import json
import time
from collections import defaultdict
from typing import Any, AsyncGenerator, Dict, List, Optional

import chess

from backend.db import closing_db, get_db, get_setting
from backend.lichess_client import DEFAULT_PERF_TYPES
from backend.utils import (
    MAX_PLAYED_AT_MS,
    coerce_count,
    coerce_text,
    extract_player_info,
    normalize_fen,
    normalize_game_outcome,
    normalize_played_at,
    number_is_unusable,
)


class GameSyncMixin:
    """Lichess game synchronization, opening blunder analysis, and game-gap detection."""

    def get_total_games_count(self) -> int:
        with closing_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM games")
            return cursor.fetchone()[0]

    @staticmethod
    def _game_row_from_upstream(game: dict, username: str) -> Optional[Dict[str, Any]]:
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
            return None

        return {
            "id": game_id,
            "speed": coerce_text(game.get("speed"), "", 24),
            "rated": 1 if game.get("rated") else 0,
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
        cursor.execute(
            """
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
            """,
            (
                row["id"], row["speed"], row["rated"], row["played_at"],
                row["white_player"], row["black_player"], row["white_rating"], row["black_rating"],
                row["user_color"], row["result"], row["opening_name"], row["eco"], row["moves_san"],
            ),
        )

    @staticmethod
    def repair_poisoned_played_at() -> int:
        with closing_db() as conn:
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
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return 0

    def _fetch_user_games(
        self, username: str, max_games: Optional[int] = None, perf_type: Optional[str] = None
    ) -> List[Dict]:
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
        username = get_setting("lichess_username") or "RobiDeli"
        games = self._fetch_user_games(username, max_games=max_games, perf_type=perf_type)
        if not games:
            return {
                "status": "no_games",
                "synced_games": 0,
                "total_games": self.get_total_games_count(),
            }

        synced = 0
        rejected: List[Dict[str, Any]] = []

        with closing_db() as conn:
            cursor = conn.cursor()
            try:
                for g in games:
                    row = self._game_row_from_upstream(g, username)
                    if row is None:
                        rejected.append({
                            "game_id": str(coerce_text((g or {}).get("id"), "", 64)),
                            "error": "unrecognised or missing game record (rejected, nothing written)",
                        })
                        continue

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
                return {
                    "status": "error",
                    "message": f"Could not save the synced games: {e}",
                    "synced_games": 0,
                    "rejected_games": rejected,
                    "total_games": self.get_total_games_count(),
                }

        repaired = self.repair_poisoned_played_at()
        self._user_stats_cache = {}
        self._user_stats_time = 0

        return {
            "status": "partial" if rejected else "success",
            "synced_games": synced,
            "rejected_games": rejected,
            "repaired_played_at": repaired,
            "total_games": self.get_total_games_count(),
        }

    async def stream_sync_recent_games(
        self,
        max_games: int = 30,
        request: Optional[Any] = None,
        perf_type: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        username = get_setting("lichess_username") or "RobiDeli"

        yield f"data: {json.dumps({'stage': 'connecting', 'message': 'Connecting to Lichess API...', 'percent': 10, 'current': 0, 'total': 0, 'synced_games': 0})}\n\n"
        await asyncio.sleep(0.02)

        yield f"data: {json.dumps({'stage': 'fetching', 'message': f'Downloading latest games for {username}...', 'percent': 25, 'current': 0, 'total': 0, 'synced_games': 0})}\n\n"
        await asyncio.sleep(0.02)

        try:
            games = await asyncio.to_thread(self._fetch_user_games, username, max_games, perf_type)
        except Exception as e:
            yield f"data: {json.dumps({'stage': 'error', 'message': f'Failed to fetch games from Lichess: {str(e)}', 'percent': 0, 'current': 0, 'total': 0, 'synced_games': 0})}\n\n"
            return

        if not games:
            yield f"data: {json.dumps({'stage': 'completed', 'message': 'No recent games found on Lichess or rate limit reached.', 'percent': 100, 'current': 0, 'total': 0, 'synced_games': 0})}\n\n"
            return

        total = len(games)
        yield f"data: {json.dumps({'stage': 'processing_start', 'message': f'Downloaded {total} games. Saving to database...', 'percent': 40, 'current': 0, 'total': total, 'synced_games': 0})}\n\n"
        await asyncio.sleep(0.01)

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
                opponent = row["black_player"] if row["user_color"] == "white" else row["white_player"]
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
                    "synced_games": synced,
                }
                yield f"data: {json.dumps(payload)}\n\n"
                await asyncio.sleep(0.01)

            conn.commit()
        except Exception as e:
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

        yield f"data: {json.dumps({'stage': 'completed', 'message': f'Synced {synced} games from Lichess.', 'percent': 100, 'current': total, 'total': total, 'synced_games': synced, 'rejected_games': rejected, 'repaired_played_at': repaired})}\n\n"

    def _position_is_reachable(self, repertoire: str, fen: str) -> bool:
        norm = normalize_fen(fen)
        if not norm:
            return False
        if norm == normalize_fen(chess.STARTING_FEN):
            return True
        with closing_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM repertoire_moves WHERE repertoire = ? AND fen = ? LIMIT 1",
                (repertoire, norm),
            ).fetchone()
            return row is not None

    def import_game_opening(
        self,
        game_id: str,
        repertoire: str,
        max_ply: Optional[int] = None,
        replace_existing: bool = False,
    ) -> Dict:
        repertoire = (repertoire or "white").lower()
        if repertoire not in ("white", "black"):
            raise ValueError("Repertoire must be 'white' or 'black'")

        with closing_db() as conn:
            game = conn.execute(
                "SELECT id, moves_san, opening_name, eco, user_color FROM games WHERE id = ?",
                (game_id,),
            ).fetchone()

        if not game:
            raise ValueError(f"Game '{game_id}' not found.")

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
            return {
                "status": "error",
                "message": "No moves found in game.",
                "imported_moves": 0,
                "repertoire": repertoire,
                "game_id": game_id,
                "conflicts": [],
            }

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
            is_user_turn = (board.turn == chess.WHITE) == (repertoire == "white")

            if not self._position_is_reachable(repertoire, p_fen):
                skipped_unreachable.append(ply + 1)
                stopped_at_ply = ply + 1
                stop_reason = (
                    f"the position after ply {ply} is not reachable in the {repertoire} repertoire, "
                    "so importing it would graft a floating branch onto the tree"
                )
                break

            res = self.add_repertoire_move(
                repertoire=repertoire,
                parent_fen=p_fen,
                move_uci=uci,
                is_user_move=is_user_turn,
                replace_existing=replace_existing,
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
            message = (
                f"Imported {imported} ply/plies of {op_name} into the {repertoire} repertoire, "
                f"then stopped at ply {stopped_at_ply}: {stop_reason}."
            )
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

    def analyze_user_games(
        self, max_games: Optional[int] = None, max_ply: int = 30, perf_type: Optional[str] = None
    ) -> Dict:
        from backend.analyzer import GameAnalyzer

        username = get_setting("lichess_username") or "RobiDeli"
        games = self._fetch_user_games(username, max_games=max_games, perf_type=perf_type)
        if not games:
            return {
                "status": "no_games",
                "synced_games": 0,
                "new_blunders": 0,
                "total_games": self.get_total_games_count(),
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
                cursor.execute("SELECT id, analyzed_at FROM games WHERE id = ?", (game_id,))
                existing = cursor.fetchone()

                if existing and existing["analyzed_at"] and existing["analyzed_at"] > 0:
                    already_analyzed_count += 1
                    synced_count += 1
                    continue

                g["user_color"] = row["user_color"]

                try:
                    self._upsert_game(cursor, row)
                    conn.commit()

                    blunders = analyzer.analyze_game_openings(g)
                    if blunders:
                        for b in blunders:
                            try:
                                count, known = self.probe_explorer_game_count(b["fen_before"])
                                b["opening_games"] = count if known else None
                            except Exception:
                                b["opening_games"] = None
                        analyzer.save_blunders_to_db(blunders)
                        new_blunders_count += len(blunders)

                    cursor.execute("UPDATE games SET analyzed_at = ? WHERE id = ?", (int(time.time()), game_id))
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
            try:
                analyzer.close()
            except Exception as e:
                errors.append(f"analyzer.close: {e}")
            try:
                conn.close()
            except Exception:
                pass

        repaired = self.repair_poisoned_played_at()
        self._user_stats_cache = {}
        self._user_stats_time = 0

        if synced_count == 0 and failed_games:
            return {
                "status": "error",
                "message": (
                    f"Analysis failed for all {len(failed_games)} game(s). The games stay "
                    "pending and will be retried."
                ),
                "synced_games": 0,
                "new_blunders": new_blunders_count,
                "already_analyzed": already_analyzed_count,
                "failed_games": failed_games,
                "rejected_games": [],
                "repaired_played_at": repaired,
                "errors": errors[:3],
                "total_games": self.get_total_games_count(),
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
            "total_games": self.get_total_games_count(),
        }

    async def stream_analyze_user_games(
        self,
        max_games: Optional[int] = None,
        max_ply: int = 30,
        request: Optional[Any] = None,
        perf_type: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        from backend.analyzer import GameAnalyzer

        username = get_setting("lichess_username") or "RobiDeli"

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
        yield f"data: {json.dumps({'stage': 'analyzing_start', 'message': f'Downloaded {total_games} games. Starting Stockfish analysis...', 'current': 0, 'total': total_games, 'percent': 0, 'new_blunders': 0, 'already_analyzed': 0})}\n\n"
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

                g["user_color"] = row["user_color"]
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

                    cursor.execute("UPDATE games SET analyzed_at = ? WHERE id = ?", (int(time.time()), game_id))
                    conn.commit()
                except Exception:
                    failed_games.append({"game_id": game_id, "error": "Analysis failed for this game."})
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
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
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

        yield f"data: {json.dumps({'stage': 'completed', 'message': f'Analysis complete: {synced_count} games processed, {new_blunders_count} new blunders found.', 'synced_games': synced_count, 'new_blunders': new_blunders_count, 'already_analyzed': already_analyzed_count, 'failed_games': failed_games, 'rejected_games': rejected_games, 'repaired_played_at': repaired, 'total_games': self.get_total_games_count(), 'percent': 100})}\n\n"

    def get_game_gaps(
        self,
        color: str,
        min_games: int = 30,
        max_move: Optional[int] = None,
        include_patched: bool = False,
        max_explorer_lookups: int = 25,
    ) -> Dict:
        """Returns opening blunders from user's actual games in positions with >= min_games in 2000+ DB."""
        color = (color or "white").lower()
        if color not in ("white", "black"):
            color = "white"

        with closing_db() as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT id, fen_before FROM blunders WHERE opening_games IS NULL")
            fen_cache: Dict[str, Optional[int]] = {}
            row_opening_games: Dict[int, int] = {}
            unresolved: List[str] = []
            for r in cursor.fetchall():
                f = r["fen_before"]
                if f not in fen_cache:
                    if len(fen_cache) >= max(0, int(max_explorer_lookups or 0)):
                        break
                    count, known = self.probe_explorer_game_count(f)
                    fen_cache[f] = count if known else None
                    if not known and f not in unresolved:
                        unresolved.append(f)
                if fen_cache[f] is None:
                    continue
                row_opening_games[r["id"]] = fen_cache[f]

            cursor.execute(
                """
                SELECT b.*, g.white_player, g.black_player, g.played_at, g.speed, g.moves_san
                FROM blunders b
                LEFT JOIN games g ON b.game_id = g.id
                WHERE b.color = ? AND b.eval_before >= -200
                  AND (? = 0 OR b.opening_games >= ? OR b.opening_games IS NULL)
                  AND (? IS NULL OR b.move_number <= ?)
                ORDER BY b.cpl_loss DESC
                """,
                (color, min_games, min_games, max_move, max_move),
            )
            rows = [dict(r) for r in cursor.fetchall()]

            resolved_rows = []
            for r in rows:
                raw_count = r.get("opening_games")
                if raw_count is None:
                    if r["id"] not in row_opening_games:
                        continue
                    r["opening_games"] = row_opening_games[r["id"]]
                elif number_is_unusable(raw_count):
                    fen = r.get("fen_before")
                    count, known = self.probe_explorer_game_count(fen) if fen else (0, False)
                    if not known:
                        if fen:
                            unresolved.append(fen)
                        continue
                    r["opening_games"] = count
                safe_count = coerce_count(r.get("opening_games"), 0)
                r["opening_games"] = safe_count
                if min_games and safe_count < min_games:
                    continue
                resolved_rows.append(r)
            rows = resolved_rows

            game_ids = list(set(r["game_id"] for r in rows if r.get("game_id")))
            all_blunders_by_game = defaultdict(list)
            if game_ids:
                placeholders = ",".join("?" for _ in game_ids)
                cursor.execute(
                    f"""
                    SELECT id, game_id, move_number, ply, color, played_san, played_move,
                           best_san, best_move, eval_before, eval_after, cpl_loss, fen_before, opening_games
                    FROM blunders
                    WHERE game_id IN ({placeholders})
                    ORDER BY move_number ASC
                    """,
                    game_ids,
                )
                for b in cursor.fetchall():
                    all_blunders_by_game[b["game_id"]].append(dict(b))

            cursor.execute(
                """
                SELECT parent_fen, move_uci FROM repertoire_moves
                WHERE repertoire = ? AND is_user_move = 1
                """,
                (color,),
            )
            rep_moves = {r["parent_fen"]: r["move_uci"] for r in cursor.fetchall()}

            cursor.execute("SELECT COUNT(*) as cnt FROM games WHERE user_color = ?", (color,))
            total_games_color = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COUNT(*) as cnt FROM games")
            total_games_all = cursor.fetchone()["cnt"]

        gaps = []
        patched_gaps = []
        for r in rows:
            norm_fen = normalize_fen(r["fen_before"])
            is_patched = rep_moves.get(norm_fen) == r["best_move"]
            opp_name = r["black_player"] if color == "white" else r["white_player"]

            g_bls = all_blunders_by_game.get(r["game_id"], [])
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
                "game_url": f"https://lichess.org/{r['game_id']}" if r["game_id"] else "",
            }

            if is_patched:
                patched_gaps.append(gap_item)
                if include_patched:
                    gaps.append(gap_item)
            else:
                gaps.append(gap_item)

        unpatched_n = len([r for r in rows if rep_moves.get(normalize_fen(r["fen_before"])) != r["best_move"]])
        result = {
            "color": color,
            "min_games": min_games,
            "total_games_analyzed": total_games_all,
            "total_games_color": total_games_color,
            "total_gaps": len(rows),
            "unpatched_gaps": unpatched_n,
            "unpatched_count": unpatched_n,
            "patched_count": len(patched_gaps),
            "gaps": gaps,
            "patched_gaps": patched_gaps,
        }
        if unresolved:
            result["unresolved_positions"] = len(unresolved)
            result["opening_games_degraded"] = True
            result["error"] = "explorer_unavailable"
        return result
