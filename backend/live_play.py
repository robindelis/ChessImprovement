"""
Lichess Board API live game manager (seeks, AI/friend challenges, board+clock stream,
fair-play lock, and automatic persistence of finished games).
"""
import asyncio
import json
import logging
import threading
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple

import chess
import requests

from backend.db import closing_db, get_setting
from backend.evaluator import format_sse
from backend.lichess_client import LichessClient, redact
from backend.openings import OpeningBook
from backend.repertoire import RepertoireManager
from backend.utils import normalize_game_outcome

logger = logging.getLogger("chess.live_play")

STREAM_CONNECT_TIMEOUT = 10
STREAM_READ_TIMEOUT = 65  # Lichess sends a newline keepalive every ~7s
ACTION_TIMEOUT = 10

ACTIVE_STATUSES = frozenset({"created", "started"})


class LivePlayError(RuntimeError):
    """User-facing error raised on invalid Board API operations."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class LivePlayManager:
    """Singleton state machine bridging Lichess Board API NDJSON streams to local SSE."""

    _instance: Optional["LivePlayManager"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._lock = threading.RLock()
        self._seeking: bool = False
        self._seek_info: Optional[Dict[str, Any]] = None
        self._seek_cancel = threading.Event()
        self._seek_resp: Optional[Any] = None

        self._event_thread: Optional[threading.Thread] = None
        self._event_stop = threading.Event()
        self._event_token: Optional[str] = None

        self._game_threads: Dict[str, threading.Thread] = {}
        self._active_game: Optional[Dict[str, Any]] = None
        self._incoming_challenges: List[Dict[str, Any]] = []
        self._outgoing_challenge: Optional[Dict[str, Any]] = None

        self._subscribers_lock = threading.Lock()
        self._subscribers: Set[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()

    @classmethod
    def get_instance(cls) -> "LivePlayManager":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------ #
    # Fair-Play Guard
    # ------------------------------------------------------------------ #

    def is_live_game_active(self) -> bool:
        """True when a real-time Lichess game is currently in progress on the board."""
        with self._lock:
            g = self._active_game
            if not g:
                return False
            return str(g.get("status") or "") in ACTIVE_STATUSES

    def active_game_id(self) -> Optional[str]:
        with self._lock:
            return self._active_game.get("id") if self._active_game else None

    # ------------------------------------------------------------------ #
    # Snapshot & SSE Pub/Sub
    # ------------------------------------------------------------------ #

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            game_copy = dict(self._active_game) if self._active_game else None
            if game_copy and "moves" in game_copy:
                game_copy["moves"] = list(game_copy["moves"])
            return {
                "seeking": self._seeking,
                "seek": dict(self._seek_info) if self._seek_info else None,
                "outgoing_challenge": dict(self._outgoing_challenge) if self._outgoing_challenge else None,
                "incoming_challenges": [dict(c) for c in self._incoming_challenges],
                "game": game_copy,
                "fair_play_locked": self.is_live_play_locked_locked(),
                "server_now_ms": int(time.time() * 1000),
            }

    def is_live_play_locked_locked(self) -> bool:
        g = self._active_game
        return bool(g and str(g.get("status") or "") in ACTIVE_STATUSES)

    def _broadcast(self, event_type: str = "state") -> None:
        payload = {"type": event_type, **self.snapshot()}
        with self._subscribers_lock:
            subs = list(self._subscribers)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, payload)
            except Exception:
                pass

    async def stream_events(self, request: Optional[Any] = None) -> AsyncGenerator[str, None]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        sub = (loop, q)
        with self._subscribers_lock:
            self._subscribers.add(sub)
        try:
            self._ensure_event_stream()
            yield format_sse({"type": "state", **self.snapshot()}, event="state")
            while True:
                if request and await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield format_sse(msg, event=msg.get("type", "state"))
                except asyncio.TimeoutError:
                    yield format_sse({"type": "ping", "server_now_ms": int(time.time() * 1000)}, event="ping")
        finally:
            with self._subscribers_lock:
                self._subscribers.discard(sub)

    # ------------------------------------------------------------------ #
    # Lichess Client Helper
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require_client() -> LichessClient:
        client = LichessClient()
        if not client.token:
            raise LivePlayError("Connect your Lichess account in Settings to play online.", 401)
        return client

    def _ensure_event_stream(self) -> None:
        client = LichessClient()
        if not client.token:
            return
        with self._lock:
            if (
                self._event_thread is not None
                and self._event_thread.is_alive()
                and self._event_token == client.token
            ):
                return
            self._event_stop.set()
            self._event_stop = threading.Event()
            self._event_token = client.token
            stop_ev = self._event_stop
            t = threading.Thread(
                target=self._run_event_stream,
                args=(client, stop_ev),
                name="lichess-event-stream",
                daemon=True,
            )
            self._event_thread = t
            t.start()

    def shutdown(self) -> None:
        self._event_stop.set()
        self.cancel_seek()

    # ------------------------------------------------------------------ #
    # Event Stream (`/api/stream/event`)
    # ------------------------------------------------------------------ #

    def _run_event_stream(self, client: LichessClient, stop_ev: threading.Event) -> None:
        headers = {**client.headers, "Accept": "application/x-ndjson"}
        url = f"{client.base_url}/api/stream/event"
        while not stop_ev.is_set():
            resp = None
            try:
                resp = requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(STREAM_CONNECT_TIMEOUT, STREAM_READ_TIMEOUT),
                )
                if resp.status_code != 200:
                    logger.warning("Lichess event stream status %s", resp.status_code)
                    stop_ev.wait(5.0)
                    continue
                for line in resp.iter_lines(decode_unicode=True):
                    if stop_ev.is_set():
                        break
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(ev, dict):
                        self._handle_upstream_event(client, ev)
            except Exception as exc:
                if not stop_ev.is_set():
                    logger.debug("Lichess event stream interrupted: %s", redact(exc, client.token))
                    stop_ev.wait(3.0)
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass

    def _handle_upstream_event(self, client: LichessClient, ev: Dict[str, Any]) -> None:
        ev_type = ev.get("type")
        if ev_type == "gameStart":
            game_obj = ev.get("game") or {}
            game_id = str(game_obj.get("gameId") or game_obj.get("id") or "").strip()
            if game_id:
                with self._lock:
                    self._seeking = False
                    self._seek_info = None
                    self._outgoing_challenge = None
                self.attach_game_stream(game_id, client=client, seed=game_obj)
        elif ev_type == "gameFinish":
            game_obj = ev.get("game") or {}
            game_id = str(game_obj.get("gameId") or game_obj.get("id") or "").strip()
            with self._lock:
                if self._active_game and self._active_game.get("id") == game_id:
                    if self._active_game.get("status") in ACTIVE_STATUSES:
                        self._active_game["status"] = str(game_obj.get("status", {}).get("name") if isinstance(game_obj.get("status"), dict) else (game_obj.get("status") or "finished"))
                        if "winner" in game_obj:
                            self._active_game["winner"] = game_obj.get("winner")
                        self._persist_completed_game(self._active_game)
            self._broadcast("game_finished")
        elif ev_type == "challenge":
            ch = ev.get("challenge") or {}
            ch_id = str(ch.get("id") or "").strip()
            challenger = (ch.get("challenger") or {}).get("name") or ""
            my_name = (get_setting("lichess_username") or "").lower()
            if ch_id and challenger.lower() != my_name:
                tc = ch.get("timeControl") or {}
                item = {
                    "id": ch_id,
                    "challenger": challenger,
                    "rating": (ch.get("challenger") or {}).get("rating") or 0,
                    "rated": bool(ch.get("rated")),
                    "speed": ch.get("speed") or "rapid",
                    "time_text": tc.get("show") or "",
                    "color": ch.get("color") or "random",
                }
                with self._lock:
                    self._incoming_challenges = [c for c in self._incoming_challenges if c["id"] != ch_id] + [item]
                self._broadcast("challenge")
        elif ev_type in ("challengeCanceled", "challengeDeclined"):
            ch = ev.get("challenge") or {}
            ch_id = str(ch.get("id") or "").strip()
            with self._lock:
                self._incoming_challenges = [c for c in self._incoming_challenges if c["id"] != ch_id]
                if self._outgoing_challenge and self._outgoing_challenge.get("id") == ch_id:
                    self._outgoing_challenge = None
            self._broadcast("challenge_removed")

    # ------------------------------------------------------------------ #
    # Matchmaking Seek & Challenges
    # ------------------------------------------------------------------ #

    def start_seek(
        self,
        minutes: int = 10,
        increment: int = 0,
        rated: bool = True,
        color: str = "random",
    ) -> Dict[str, Any]:
        """Create a Lichess Board API lobby seek (`POST /api/board/seek`)."""
        client = self._require_client()
        if self.is_live_game_active():
            raise LivePlayError("A live game is already in progress.", 409)

        # Board API matchmaking restriction: Rapid (total estimated >= 480s) & Classical
        est_seconds = int(minutes) * 60 + 40 * int(increment)
        if est_seconds < 480:
            raise LivePlayError(
                "Lichess's official Board API restricts random pool seeks to Rapid (10+0, 10+5, 15+10) and Classical. "
                "Use Direct Challenge or Play vs AI for Blitz.",
                400,
            )

        self.cancel_seek()
        self._ensure_event_stream()

        cancel_ev = threading.Event()
        seek_meta = {
            "minutes": int(minutes),
            "increment": int(increment),
            "rated": bool(rated),
            "color": color if color in ("white", "black", "random") else "random",
            "started_at": int(time.time() * 1000),
        }
        with self._lock:
            self._seeking = True
            self._seek_info = seek_meta
            self._seek_cancel = cancel_ev
        self._broadcast("seek_started")

        t = threading.Thread(
            target=self._run_seek_stream,
            args=(client, seek_meta, cancel_ev),
            name="lichess-seek",
            daemon=True,
        )
        t.start()
        return self.snapshot()

    def _run_seek_stream(self, client: LichessClient, seek_meta: Dict[str, Any], cancel_ev: threading.Event) -> None:
        form: Dict[str, Any] = {
            "rated": "true" if seek_meta["rated"] else "false",
            "time": str(seek_meta["minutes"]),
            "increment": str(seek_meta["increment"]),
            "variant": "standard",
        }
        if seek_meta["color"] in ("white", "black"):
            form["color"] = seek_meta["color"]

        resp = None
        try:
            resp = requests.post(
                f"{client.base_url}/api/board/seek",
                headers={**client.headers, "Accept": "text/plain"},
                data=form,
                stream=True,
                timeout=(STREAM_CONNECT_TIMEOUT, STREAM_READ_TIMEOUT),
            )
            with self._lock:
                self._seek_resp = resp
            if resp.status_code != 200:
                err_txt = (resp.text or "")[:180]
                logger.warning("Seek rejected (%s): %s", resp.status_code, redact(err_txt, client.token))
                with self._lock:
                    if self._seek_cancel is cancel_ev:
                        self._seeking = False
                        self._seek_info = None
                        self._last_error = f"Lichess rejected seek ({resp.status_code}): {err_txt}"
                self._broadcast("seek_error")
                return

            for _ in resp.iter_lines():
                if cancel_ev.is_set():
                    break
        except Exception as exc:
            if not cancel_ev.is_set():
                logger.debug("Seek connection closed: %s", redact(exc, client.token))
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            with self._lock:
                if self._seek_resp is resp:
                    self._seek_resp = None
                if self._seek_cancel is cancel_ev and self._seeking:
                    self._seeking = False
                    self._seek_info = None
            self._broadcast("seek_ended")

    def cancel_seek(self) -> Dict[str, Any]:
        with self._lock:
            self._seeking = False
            self._seek_info = None
            self._seek_cancel.set()
            resp = self._seek_resp
            self._seek_resp = None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        self._broadcast("seek_canceled")
        return self.snapshot()

    def create_challenge(
        self,
        mode: str = "ai",
        ai_level: int = 5,
        username: str = "",
        clock_limit_sec: int = 600,
        increment_sec: int = 0,
        rated: bool = False,
        color: str = "random",
    ) -> Dict[str, Any]:
        """Challenge Lichess Stockfish AI (`mode='ai'`) or a Lichess user (`mode='user'`)."""
        client = self._require_client()
        if self.is_live_game_active():
            raise LivePlayError("A live game is already in progress.", 409)

        self.cancel_seek()
        self._ensure_event_stream()

        clean_color = color if color in ("white", "black", "random") else "random"
        if mode == "ai":
            form: Dict[str, Any] = {
                "level": str(max(1, min(8, int(ai_level)))),
                "clock.limit": str(max(60, min(10800, int(clock_limit_sec)))),
                "clock.increment": str(max(0, min(180, int(increment_sec)))),
                "color": clean_color,
                "variant": "standard",
            }
            url = f"{client.base_url}/api/challenge/ai"
        else:
            target = (username or "").strip()
            if not target:
                raise LivePlayError("Enter a Lichess username to challenge.", 400)
            form = {
                "rated": "true" if rated else "false",
                "clock.limit": str(max(60, min(10800, int(clock_limit_sec)))),
                "clock.increment": str(max(0, min(180, int(increment_sec)))),
                "color": clean_color,
                "variant": "standard",
            }
            url = f"{client.base_url}/api/challenge/{target}"

        try:
            resp = requests.post(url, headers=client.headers, data=form, timeout=ACTION_TIMEOUT)
        except Exception as exc:
            raise LivePlayError(f"Network error creating challenge: {redact(exc, client.token)}", 502)

        if resp.status_code not in (200, 201):
            detail = ""
            try:
                j = resp.json()
                detail = str(j.get("error") or j)
            except Exception:
                detail = (resp.text or "")[:140]
            raise LivePlayError(f"Lichess rejected challenge ({resp.status_code}): {detail}", 400)

        data = resp.json() if resp.content else {}
        if mode == "ai":
            game_id = str(data.get("id") or "").strip()
            if game_id:
                self.attach_game_stream(game_id, client=client, seed=data)
        else:
            ch = data.get("challenge") or data
            with self._lock:
                self._outgoing_challenge = {
                    "id": str(ch.get("id") or ""),
                    "dest_user": username.strip(),
                    "clock_limit_sec": clock_limit_sec,
                    "increment_sec": increment_sec,
                    "rated": bool(rated),
                }
            self._broadcast("challenge_sent")
        return self.snapshot()

    def respond_challenge(self, challenge_id: str, accept: bool) -> Dict[str, Any]:
        client = self._require_client()
        verb = "accept" if accept else "decline"
        try:
            resp = requests.post(
                f"{client.base_url}/api/challenge/{challenge_id}/{verb}",
                headers=client.headers,
                timeout=ACTION_TIMEOUT,
            )
        except Exception as exc:
            raise LivePlayError(f"Network error responding to challenge: {redact(exc, client.token)}", 502)
        if resp.status_code != 200:
            raise LivePlayError(f"Could not {verb} challenge (HTTP {resp.status_code}).", 400)
        with self._lock:
            self._incoming_challenges = [c for c in self._incoming_challenges if c["id"] != challenge_id]
        if accept:
            self.attach_game_stream(challenge_id, client=client)
        self._broadcast("challenge_responded")
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Game Stream (`/api/board/game/stream/{gameId}`)
    # ------------------------------------------------------------------ #

    def attach_game_stream(
        self,
        game_id: str,
        client: Optional[LichessClient] = None,
        seed: Optional[Dict[str, Any]] = None,
    ) -> None:
        client = client or self._require_client()
        with self._lock:
            existing = self._game_threads.get(game_id)
            if existing is not None and existing.is_alive():
                return
            if seed and (not self._active_game or self._active_game.get("id") != game_id):
                my_color = str(seed.get("color") or "white").lower()
                opp = seed.get("opponent") or {}
                my_name = get_setting("lichess_username") or "You"
                self._active_game = {
                    "id": game_id,
                    "speed": seed.get("speed") or "rapid",
                    "rated": bool(seed.get("rated")),
                    "user_color": "black" if my_color == "black" else "white",
                    "white_name": my_name if my_color != "black" else (opp.get("username") or "Opponent"),
                    "black_name": (opp.get("username") or "Opponent") if my_color != "black" else my_name,
                    "white_rating": opp.get("rating") if my_color == "black" else None,
                    "black_rating": opp.get("rating") if my_color != "black" else None,
                    "initial_fen": seed.get("fen") or chess.STARTING_FEN,
                    "fen": seed.get("fen") or chess.STARTING_FEN,
                    "turn": "white",
                    "moves": [],
                    "wtime_ms": 600_000,
                    "btime_ms": 600_000,
                    "winc_ms": 0,
                    "binc_ms": 0,
                    "clock_synced_at_ms": int(time.time() * 1000),
                    "status": "started",
                    "winner": None,
                    "draw_offer_from": None,
                    "opponent_gone": False,
                    "claim_win_in_sec": None,
                    "persisted": False,
                }
                self._broadcast("game_started")

            t = threading.Thread(
                target=self._run_game_stream,
                args=(client, game_id),
                name=f"lichess-game-{game_id}",
                daemon=True,
            )
            self._game_threads[game_id] = t
            t.start()

    def _run_game_stream(self, client: LichessClient, game_id: str) -> None:
        headers = {**client.headers, "Accept": "application/x-ndjson"}
        url = f"{client.base_url}/api/board/game/stream/{game_id}"
        resp = None
        try:
            resp = requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=(STREAM_CONNECT_TIMEOUT, STREAM_READ_TIMEOUT),
            )
            if resp.status_code != 200:
                logger.warning("Game stream %s returned %s", game_id, resp.status_code)
                return
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if isinstance(ev, dict):
                    self.apply_game_stream_frame(game_id, ev)
        except Exception as exc:
            logger.debug("Game stream %s ended: %s", game_id, redact(exc, client.token))
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            with self._lock:
                self._game_threads.pop(game_id, None)

    @staticmethod
    def _player_label(player_obj: Dict[str, Any], fallback: str) -> Tuple[str, int, bool]:
        if not isinstance(player_obj, dict):
            return fallback, 0, False
        if "aiLevel" in player_obj:
            lvl = int(player_obj.get("aiLevel") or 1)
            return f"Stockfish Lv.{lvl}", 0, True
        name = str(player_obj.get("name") or player_obj.get("id") or fallback)
        rating = int(player_obj.get("rating") or 0)
        return name, rating, False

    def apply_game_stream_frame(self, game_id: str, ev: Dict[str, Any]) -> None:
        """Process a `gameFull`, `gameState`, or `opponentGone` NDJSON frame."""
        ev_type = ev.get("type")
        now_ms = int(time.time() * 1000)

        with self._lock:
            if ev_type == "gameFull":
                white_obj = ev.get("white") or {}
                black_obj = ev.get("black") or {}
                w_name, w_rating, w_ai = self._player_label(white_obj, "White")
                b_name, b_rating, b_ai = self._player_label(black_obj, "Black")
                my_username = (get_setting("lichess_username") or "").strip().lower()
                if my_username and str(black_obj.get("id") or black_obj.get("name") or "").lower() == my_username:
                    user_color = "black"
                elif my_username and str(white_obj.get("id") or white_obj.get("name") or "").lower() == my_username:
                    user_color = "white"
                elif w_ai and not b_ai:
                    user_color = "black"
                else:
                    user_color = (self._active_game or {}).get("user_color") or "white"

                clock_cfg = ev.get("clock") or {}
                init_fen = ev.get("initialFen")
                if not init_fen or init_fen == "startpos":
                    init_fen = chess.STARTING_FEN

                self._active_game = {
                    "id": game_id,
                    "speed": ev.get("speed") or "rapid",
                    "rated": bool(ev.get("rated")),
                    "created_at": int(ev.get("createdAt") or now_ms),
                    "user_color": user_color,
                    "white_name": w_name,
                    "black_name": b_name,
                    "white_rating": w_rating,
                    "black_rating": b_rating,
                    "is_ai": bool(w_ai or b_ai),
                    "initial_fen": init_fen,
                    "fen": init_fen,
                    "turn": "white",
                    "moves": [],
                    "wtime_ms": int(clock_cfg.get("initial") or 600_000),
                    "btime_ms": int(clock_cfg.get("initial") or 600_000),
                    "winc_ms": int(clock_cfg.get("increment") or 0),
                    "binc_ms": int(clock_cfg.get("increment") or 0),
                    "clock_synced_at_ms": now_ms,
                    "clocks_by_ply": [],
                    "status": "started",
                    "winner": None,
                    "draw_offer_from": None,
                    "opponent_gone": False,
                    "claim_win_in_sec": None,
                    "persisted": False,
                }
                state = ev.get("state")
                if isinstance(state, dict):
                    self._apply_state_dict_locked(self._active_game, state, now_ms)
            elif ev_type == "gameState":
                if self._active_game and self._active_game.get("id") == game_id:
                    self._apply_state_dict_locked(self._active_game, ev, now_ms)
            elif ev_type == "opponentGone":
                if self._active_game and self._active_game.get("id") == game_id:
                    self._active_game["opponent_gone"] = bool(ev.get("gone"))
                    self._active_game["claim_win_in_sec"] = ev.get("claimWinInSeconds")

        self._broadcast("game_update")

    def _apply_state_dict_locked(self, game: Dict[str, Any], state: Dict[str, Any], now_ms: int) -> None:
        raw_moves = str(state.get("moves") or "").strip()
        uci_tokens = [m for m in raw_moves.split() if m]
        board = chess.Board(game.get("initial_fen") or chess.STARTING_FEN)
        structured_moves: List[Dict[str, Any]] = []
        for idx, uci in enumerate(uci_tokens):
            try:
                m = chess.Move.from_uci(uci)
                if m not in board.legal_moves:
                    break
                san = board.san(m)
                board.push(m)
                structured_moves.append({
                    "ply": idx + 1,
                    "move_number": (idx // 2) + 1,
                    "color": "white" if idx % 2 == 0 else "black",
                    "uci": uci,
                    "san": san,
                    "fen": board.fen(),
                })
            except Exception:
                break

        game["moves"] = structured_moves
        game["fen"] = board.fen()
        game["turn"] = "white" if board.turn == chess.WHITE else "black"
        game["in_check"] = board.is_check()

        if "wtime" in state and state["wtime"] is not None:
            game["wtime_ms"] = max(0, int(state["wtime"]))
        if "btime" in state and state["btime"] is not None:
            game["btime_ms"] = max(0, int(state["btime"]))
        if "winc" in state and state["winc"] is not None:
            game["winc_ms"] = max(0, int(state["winc"]))
        if "binc" in state and state["binc"] is not None:
            game["binc_ms"] = max(0, int(state["binc"]))
        game["clock_synced_at_ms"] = now_ms

        clocks_list: List[int] = list(game.get("clocks_by_ply") or [])
        if len(structured_moves) > len(clocks_list):
            just_moved = structured_moves[-1]["color"]
            rem = game["wtime_ms"] if just_moved == "white" else game["btime_ms"]
            while len(clocks_list) < len(structured_moves):
                clocks_list.append(rem)
            game["clocks_by_ply"] = clocks_list

        status_val = state.get("status")
        if isinstance(status_val, dict):
            status_str = str(status_val.get("name") or "started")
        else:
            status_str = str(status_val or game.get("status") or "started")
        game["status"] = status_str
        if "winner" in state:
            game["winner"] = state.get("winner")

        if state.get("wdraw"):
            game["draw_offer_from"] = "white"
        elif state.get("bdraw"):
            game["draw_offer_from"] = "black"
        else:
            game["draw_offer_from"] = None

        if status_str not in ACTIVE_STATUSES and not game.get("persisted"):
            self._persist_completed_game(game)

    def _persist_completed_game(self, game: Dict[str, Any]) -> None:
        """Save completed live game directly into SQLite `games` so Analysis opens immediately."""
        moves = game.get("moves") or []
        if not moves:
            return
        result_token = normalize_game_outcome(game.get("winner"), game.get("status"), game.get("user_color") or "white")
        if result_token is None:
            return

        sans = [m["san"] for m in moves]
        moves_san = " ".join(sans)
        ident = OpeningBook.get_instance().identify_game_opening(sans)
        row = {
            "id": game["id"],
            "speed": game.get("speed") or "rapid",
            "rated": 1 if game.get("rated") else 0,
            "played_at": int(game.get("created_at") or int(time.time() * 1000)),
            "white_player": game.get("white_name") or "White",
            "black_player": game.get("black_name") or "Black",
            "white_rating": int(game.get("white_rating") or 0),
            "black_rating": int(game.get("black_rating") or 0),
            "user_color": game.get("user_color") or "white",
            "result": result_token,
            "opening_name": ident.get("name") or "Standard Opening",
            "eco": ident.get("eco") or "",
            "moves_san": moves_san,
        }
        try:
            with closing_db() as conn:
                cur = conn.cursor()
                RepertoireManager._upsert_game(cur, row)
                if game.get("clocks_by_ply"):
                    cur.execute(
                        "UPDATE games SET clocks_json = ? WHERE id = ?",
                        (json.dumps(game["clocks_by_ply"]), game["id"]),
                    )
                conn.commit()
            game["persisted"] = True
            RepertoireManager.get_instance().close()
        except Exception:
            logger.exception("Could not persist completed live game %s", game.get("id"))

    # ------------------------------------------------------------------ #
    # Move & Game Actions
    # ------------------------------------------------------------------ #

    def submit_move(self, game_id: str, move_uci: str, offer_draw: bool = False) -> Dict[str, Any]:
        client = self._require_client()
        uci = (move_uci or "").strip().lower()
        with self._lock:
            g = self._active_game
            if g and g.get("id") == game_id and g.get("status") in ACTIVE_STATUSES:
                if g.get("turn") != g.get("user_color"):
                    raise LivePlayError("It is not your turn.", 400)
                b = chess.Board(g["fen"])
                try:
                    mv = chess.Move.from_uci(uci)
                except Exception:
                    raise LivePlayError("Invalid UCI move format.", 400)
                if mv not in b.legal_moves:
                    raise LivePlayError("Illegal move.", 400)

        params = {"offeringDraw": "true"} if offer_draw else None
        try:
            resp = requests.post(
                f"{client.base_url}/api/board/game/{game_id}/move/{uci}",
                headers=client.headers,
                params=params,
                timeout=ACTION_TIMEOUT,
            )
        except Exception as exc:
            raise LivePlayError(f"Network error sending move: {redact(exc, client.token)}", 502)

        if resp.status_code != 200:
            detail = ""
            try:
                detail = str((resp.json() or {}).get("error") or "")
            except Exception:
                detail = (resp.text or "")[:120]
            raise LivePlayError(detail or f"Move rejected by Lichess ({resp.status_code}).", 400)
        return {"status": "ok"}

    def game_action(self, game_id: str, action: str) -> Dict[str, Any]:
        client = self._require_client()
        routes = {
            "abort": f"/api/board/game/{game_id}/abort",
            "resign": f"/api/board/game/{game_id}/resign",
            "draw_offer": f"/api/board/game/{game_id}/draw/yes",
            "draw_accept": f"/api/board/game/{game_id}/draw/yes",
            "draw_decline": f"/api/board/game/{game_id}/draw/no",
            "claim_victory": f"/api/board/game/{game_id}/claim-victory",
        }
        if action == "dismiss":
            with self._lock:
                if self._active_game and self._active_game.get("id") == game_id and self._active_game.get("status") not in ACTIVE_STATUSES:
                    self._active_game = None
            self._broadcast("game_dismissed")
            return self.snapshot()

        path = routes.get(action)
        if not path:
            raise LivePlayError(f"Unsupported game action: {action}", 400)

        try:
            resp = requests.post(
                f"{client.base_url}{path}",
                headers=client.headers,
                timeout=ACTION_TIMEOUT,
            )
        except Exception as exc:
            raise LivePlayError(f"Network error sending action: {redact(exc, client.token)}", 502)

        if resp.status_code != 200:
            raise LivePlayError(f"Lichess rejected {action} (HTTP {resp.status_code}).", 400)
        return {"status": "ok"}
