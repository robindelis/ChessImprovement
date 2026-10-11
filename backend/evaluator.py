"""
Stockfish engine lifecycle (bounded pools) + the SSE evaluation-stream contract.

================================================================================
SSE PAYLOAD CONTRACT  --  ``GET /api/repertoire/eval_stream``  (version ``v = 1``)
================================================================================
Every frame is a single SSE event block.  The block is rendered by
:func:`format_sse` and is *always* shaped like this::

    data: {"v":1,"type":"eval",...}      <-- MUST stay the first line of the block
    event: eval                          <-- optional event name, never first

``data:`` is deliberately emitted **first** because both existing consumers
parse the block positionally:

* ``frontend/repertoire_builder.js`` does ``block.trim().startsWith('data: ')``
* ``frontend/scout.js`` does ``block.split('\\n').find(l => l.startsWith('data: '))``

An ``event:`` line placed *before* ``data:`` silently breaks the Repertoire
Builder.  Field order inside an SSE block is not significant per the spec, so
``data:``-first is safe for every conforming client *and* keeps the two
hand-rolled parsers working.  Do not reorder.

Fields (always present, never absent -- this is the whole point of the schema):

=============  ======  =========================================================
field          type    meaning
=============  ======  =========================================================
``v``          int     contract version, currently ``2``.  Bump on any change.
                       (``2`` added ``pv``; ``1`` frames are indistinguishable
                       from ``2`` frames to every current consumer, which reads
                       only the fields it knows.)
``type``       str     ``"eval"`` for progress/terminal positions, ``"error"``
                       for a terminal failure.  Discriminating field.
``fen``        str     normalized FEN the frame describes ("" if unknown).
``depth``      int     search depth reached (``0`` on errors, ``99`` for a
                       position that is already over, ``0`` on a cache miss
                       replay).  Always an int, never missing.
``score_cp``   int|None centipawns, **White's point of view**, saturated to
                       +/-10000.  ``None`` only if the engine gave nothing.
``eval``       int|None **Documented alias of ``score_cp``.**  Emitted so that
                       ``frontend/scout.js`` (``data.eval``) and
                       ``frontend/repertoire_builder.js`` (``data.score_cp``)
                       can both read the same frame.  The two keys are always
                       equal; neither is optional.
``mate``       int|None mate distance in moves, White POV, else ``null``.
``is_mate``    bool    ``True`` iff ``mate`` is not null.  Redundant with
                       ``mate`` so consumers that cannot compare against
                       ``null`` still get a safe boolean.
``best_move``  str     best move in UCI, ``""`` when unknown.  Always
                       ``pv[0]["uci"]`` when ``pv`` is non-empty.
``best_san``   str     best move in SAN, ``""`` when unknown.  Always
                       ``pv[0]["san"]`` when ``pv`` is non-empty.
``pv``         list    The principal variation **at this frame's depth**, i.e.
                       it is re-sent on every progress frame and *evolves* as
                       the search deepens -- the same progressive behaviour
                       lichess.com and chess.com show.  A list of at most
                       ``PV_MAX_PLIES`` objects ``{"uci": "e2e4", "san": "e4"}``;
                       ``[]`` when the engine reported no line, on a terminal
                       position, and on every ``error`` frame.  Both keys are
                       validated server-side (:func:`sanitize_pv`) because the
                       consumer turns them into board arrows and DOM text.
``final``      bool    ``True`` on the last frame of a successful stream, and
                       also on an ``error`` frame so a client gating on
                       ``final`` stops waiting instead of hanging.
``cached``     bool    ``True`` when the frame was replayed from the
                       server-side position cache.
``code``       str     error frames only: stable machine-readable code
                       (``invalid_fen``/``engine_unavailable``/
                       ``engine_busy``/``engine_error``).
``error``      str     error frames only: **sanitized** human message.  Never
                       contains a filesystem path.
=============  ======  =========================================================

Mate scores: when ``is_mate`` is true, ``score_cp`` is only a *saturated
stand-in* (+/-10000).  Always read ``mate`` for the distance -- never derive
"M3" from ``score_cp``.

Centipawn -> eval-bar height (implemented identically in both frontends,
``AGENTS.md`` section 4C)::

    height% = 50 + 50 * (2 / (1 + exp(-0.004 * cp)) - 1)
            = 100 / (1 + exp(-0.004 * cp))

Verified in :func:`centipawn_to_height` below (cp=0 -> 50%, cp->+inf -> 100%).
Both frontends additionally clamp the *height* (builder 3..97, scout 5..95);
the server never clamps more tightly than +/-10000, so the two clamps agree
on the sign and on saturation.
"""

import os
import re
import json
import time
import atexit
import signal
import asyncio
import logging
import threading
import contextlib
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from collections import OrderedDict
from starlette.requests import Request

import chess
import chess.engine

logger = logging.getLogger("chess.evaluator")

STOCKFISH_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "stockfish", "stockfish-linux-x86-64-universal"
)

#: Single source of truth for engine tuning (previously duplicated verbatim in
#: ``analyzer.py``, ``repertoire.py`` and ``evaluator.py``).
ENGINE_OPTIONS: Dict[str, int] = {"Threads": 2, "Hash": 64}


def _env_int(name: str, default: int, lo: int = 1, hi: int = 256) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, lo: float = 0.0) -> float:
    try:
        return max(lo, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


#: Maximum number of Stockfish processes the *synchronous* pool may keep alive.
#: Bound the previous behaviour where N concurrent requests spawned N processes
#: (each 2 threads + 64 MB hash) -- see audit finding CODE-02-09.
SYNC_POOL_MAX_SIZE = _env_int("CHESS_ENGINE_MAX_PROCS", 2)
#: How long a synchronous caller waits for a free engine slot before giving up
#: with :class:`EngineBusy`.  ``api.py`` maps that to HTTP 503 + a
#: ``Retry-After`` derived from this value, so the advertised retry and the real
#: wait cannot drift apart.
#:
#: Audit ABUSE-22: this was 300 s.  That is not a bounded wait, it is a hang --
#: a caller that honoured the advertised ``Retry-After: 5`` and retried then
#: measured 176 s of tail latency behind a saturated pool.  20 s matches the
#: async/SSE pool below; a Stockfish analysis of one opening position is a few
#: seconds, so a 20 s wait covers a legitimately busy pool and still fails fast
#: on a wedged one.  Overridable with ``CHESS_ENGINE_WAIT_TIMEOUT``.
SYNC_POOL_WAIT_TIMEOUT = _env_float("CHESS_ENGINE_WAIT_TIMEOUT", 20.0)
#: The async (SSE) pool keeps its historical single-engine behaviour by default
#: (max_size=1) so eval-bar latency is unchanged; the cap is now explicit and
#: testable, and the wait is bounded so a stalled reader can never hang a queue.
ASYNC_POOL_MAX_SIZE = _env_int("CHESS_ENGINE_ASYNC_MAX", 1)
ASYNC_POOL_WAIT_TIMEOUT = _env_float("CHESS_ENGINE_ASYNC_WAIT_TIMEOUT", 20.0)
#: Engines idle for longer than this are discarded on the next checkout, so a
#: pooled process never pins its 64 MB hash forever.
ENGINE_IDLE_SECONDS = _env_float("CHESS_ENGINE_IDLE_SECONDS", 600.0)

#: Liveness ping budget when re-using a pooled engine.
PING_TIMEOUT = 2.0
#: Grace period for a polite ``quit`` before we give up and hard-close.
QUIT_TIMEOUT = 5.0
#: Saturated centipawn value used for mate scores and for clamping the bar.
MATE_SCORE_CP = 10000
#: How many plies of the principal variation are published per frame.  Bounded
#: on purpose: the line is re-sent on *every* progress frame (6 of them for a
#: depth-16 search), it is drawn as board arrows, and an unbounded PV would make
#: one frame's payload as big as the whole search.  12 plies == 6 full moves,
#: which is what a human reads off an analysis board before it stops being a
#: "line" and starts being a score.
PV_MAX_PLIES = 12


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class EngineBusy(RuntimeError):
    """The bounded engine pool is saturated and the bounded wait expired."""


class EngineUnavailable(RuntimeError):
    """An engine process could not be started or died while being checked out."""


# ---------------------------------------------------------------------------
# SSE contract helpers
# ---------------------------------------------------------------------------
SSE_CONTRACT_VERSION = 2

TYPE_EVAL = "eval"
TYPE_ERROR = "error"

ERR_INVALID_FEN = "invalid_fen"
ERR_ENGINE_UNAVAILABLE = "engine_unavailable"
ERR_ENGINE_BUSY = "engine_busy"
ERR_ENGINE_ERROR = "engine_error"

#: The stable field list of an eval frame.  ``repertoire_builder.js`` reads
#: ``score_cp``; ``scout.js`` reads ``eval``.  Both are emitted, always equal.
EVAL_STREAM_FIELDS: Tuple[str, ...] = (
    "v", "type", "fen", "depth", "score_cp", "eval", "mate", "is_mate",
    "best_move", "best_san", "pv", "final", "cached",
)
ERROR_STREAM_FIELDS: Tuple[str, ...] = EVAL_STREAM_FIELDS + ("code", "error")

#: A UCI move is ``<file><rank><file><rank>`` plus an optional promotion piece.
#: Anything else is not a move and must never reach a consumer that will draw it
#: on a board.
UCI_MOVE_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$")

#: Longest SAN the sanitizer keeps.  Real SAN tops out around ``O-O-O+`` / ``Qxg7#``
#: (6 chars); 12 leaves headroom without letting a hostile engine line become an
#: arbitrary string in the DOM.  The character class is chess notation plus ``=``
#: (promotion, ``e8=Q+``) and ``-`` (castling, ``O-O``).
SAN_MAX_LEN = 12


def eval_stream_contract() -> Dict[str, Any]:
    """Machine-readable description of the SSE payload (for docs / future route)."""
    return {
        "version": SSE_CONTRACT_VERSION,
        "scores_are_white_pov": True,
        "saturated_score_cp": MATE_SCORE_CP,
        "max_pv_plies": PV_MAX_PLIES,
        "event_names": {"eval": TYPE_EVAL, "error": TYPE_ERROR},
        "error_codes": [
            ERR_INVALID_FEN, ERR_ENGINE_UNAVAILABLE, ERR_ENGINE_BUSY, ERR_ENGINE_ERROR,
        ],
        "eval_fields": list(EVAL_STREAM_FIELDS),
        "error_fields": list(ERROR_STREAM_FIELDS),
        "notes": [
            "`eval` is a documented alias of `score_cp`; the two are always equal.",
            "`data:` is always the first line of an event block.",
            "An error frame always carries `final: true` and terminates the stream.",
            "`pv` is re-sent on every progress frame, so the line evolves with depth.",
        ],
    }


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


def centipawn_to_height(cp: float) -> float:
    """Reference implementation of the eval-bar height mapping (AGENTS.md section 4C).

    ``height% = 50 + 50 * (2 / (1 + exp(-0.004 * cp)) - 1)`` which is
    algebraically identical to ``100 / (1 + exp(-0.004 * cp))``: 50% at cp=0,
    ->100% as cp->+inf, ->0% as cp->-inf.  Both frontends implement this exact
    curve; the server does not apply a second clamp so the sign and the
    saturation are decided in exactly one place per consumer.
    """
    try:
        cp = float(cp)
    except (TypeError, ValueError):
        return 50.0
    if cp != cp:  # NaN
        return 50.0
    cp = max(-MATE_SCORE_CP, min(MATE_SCORE_CP, cp))
    try:
        height = 50.0 + 50.0 * (2.0 / (1.0 + math_exp(-0.004 * cp)) - 1.0)
    except OverflowError:
        height = 100.0 if cp > 0 else 0.0
    return max(0.0, min(100.0, height))


def math_exp(x: float) -> float:
    """``math.exp`` with OverflowError -> +/-inf (the frontend uses Math.exp)."""
    import math
    try:
        return math.exp(x)
    except OverflowError:
        return float("inf") if x > 0 else 0.0


def _clamp_cp(score_cp: Optional[float]) -> Optional[int]:
    if score_cp is None:
        return None
    try:
        return int(max(-MATE_SCORE_CP, min(MATE_SCORE_CP, round(float(score_cp)))))
    except (TypeError, ValueError):
        return None


def sanitize_pv(pv: Any) -> List[Dict[str, str]]:
    """Bound and validate a principal variation for the wire.

    The PV is drawn as board arrows and printed as text, so it is the one field
    whose *contents* matter to the client rather than just its presence.  This is
    the trust boundary: a UCI move must look like a UCI move, SAN is stripped to
    the characters chess notation actually uses and truncated, and the whole
    list is capped at :data:`PV_MAX_PLIES`.  Malformed entries are dropped, not
    passed through, so a truncated or hostile engine line degrades to a shorter
    line instead of an exception in the browser.
    """
    if not isinstance(pv, (list, tuple)):
        return []
    out: List[Dict[str, str]] = []
    for item in list(pv)[:PV_MAX_PLIES]:
        if isinstance(item, str):
            uci, san = item, ""
        elif isinstance(item, dict):
            uci, san = item.get("uci"), item.get("san")
        elif hasattr(item, "uci"):          # a bare chess.Move, for direct callers
            try:
                uci, san = item.uci(), None
            except Exception:
                continue
        else:
            continue
        if not isinstance(uci, str) or not UCI_MOVE_RE.match(uci):
            continue
        clean_san = ""
        if isinstance(san, str):
            clean_san = re.sub(r"[^A-Za-z0-9+#=\-]", "", san)[:SAN_MAX_LEN]
        out.append({"uci": uci, "san": clean_san})
    return out


def pv_line(board: chess.Board, raw_pv: Any, limit: int = PV_MAX_PLIES) -> List[Dict[str, str]]:
    """Turn a raw engine PV into a bounded, SAN-annotated ``[{uci, san}, ...]`` list.

    SAN has to be produced by *replaying* the line, not by stringifying move
    objects: ``board.san(pv[1])`` would need the position after ``pv[0]``, which
    is precisely what a search that has not finished playing does not give us.
    The replay also stops at the first move that is not legal in the position it
    claims to come from, so a truncated or garbled PV yields a short line rather
    than a wrong one.
    """
    if not raw_pv:
        return []
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        return []
    if limit == 0:
        return []
    walker = board.copy(stack=True)
    out: List[Dict[str, str]] = []
    for move in list(raw_pv)[:limit]:
        try:
            if move not in walker.legal_moves:
                break
            san = walker.san(move)
        except Exception:
            break
        out.append({"uci": move.uci(), "san": san})
        walker.push(move)
    return out


def build_eval_event(
    depth: int,
    score_cp: Optional[float],
    mate: Optional[int] = None,
    best_move: str = "",
    best_san: str = "",
    final: bool = False,
    cached: bool = False,
    fen: str = "",
    pv: Any = None,
) -> Dict[str, Any]:
    """Build one ``type="eval"`` payload honouring the contract above."""
    is_mate = mate is not None
    if score_cp is None and is_mate:
        # A mate score must never be lost: publish a saturated stand-in, but the
        # authoritative distance is `mate`.
        score_cp = MATE_SCORE_CP if mate > 0 else -MATE_SCORE_CP
    cp = _clamp_cp(score_cp)
    line = sanitize_pv(pv)
    return {
        "v": SSE_CONTRACT_VERSION,
        "type": TYPE_EVAL,
        "fen": fen,
        "depth": int(depth),
        "score_cp": cp,
        "eval": cp,  # documented alias -- frontend/scout.js reads this key
        "mate": int(mate) if is_mate else None,
        "is_mate": is_mate,
        "best_move": best_move or "",
        "best_san": best_san or "",
        "pv": line,
        "final": bool(final),
        "cached": bool(cached),
    }


def sanitize_error(exc: Any) -> str:
    """Collapse an exception to a short single-line string with no filesystem paths."""
    try:
        message = str(exc) or exc.__class__.__name__
    except Exception:  # pragma: no cover - defensive
        message = "unknown error"
    message = message.replace(STOCKFISH_PATH, "<stockfish>")
    message = re.sub(r"(?:/[^\s'\"]+)+", "<path>", message)
    message = " ".join(message.split())
    return message[:200] or "unknown error"


def build_error_event(code: str, message: str, fen: str = "", depth: int = 0) -> Dict[str, Any]:
    """Build one terminal ``type="error"`` payload.

    ``final`` is forced to ``True`` so a client that waits for a final frame
    stops instead of hanging on a stream that will never progress.
    """
    payload = build_eval_event(depth=depth, score_cp=None, mate=None, final=True, fen=fen)
    payload["type"] = TYPE_ERROR
    payload["code"] = code
    payload["error"] = message
    return payload


def format_sse(payload: Dict[str, Any], event: Optional[str] = None) -> str:
    """Render a payload as one SSE event block.

    ``data:`` is always first on purpose -- see the module docstring.
    """
    data = json.dumps(payload, separators=(",", ":"))
    lines = ["data: " + data]
    if event:
        lines.append("event: " + event)
    return "\n".join(lines) + "\n\n"


def _frame(payload: Dict[str, Any]) -> str:
    """Render an eval/error payload with the matching SSE event name."""
    name = TYPE_ERROR if payload.get("type") == TYPE_ERROR else TYPE_EVAL
    if name == TYPE_EVAL and payload.get("final"):
        name = "eval_final"
    return format_sse(payload, event=name)


# ---------------------------------------------------------------------------
# Shared low level helpers
# ---------------------------------------------------------------------------
def _process_alive(transport: Any) -> bool:
    """True when the child process behind a UCI transport looks usable."""
    if transport is None:
        return False
    try:
        if transport.is_closing():
            return False
    except Exception:
        return False
    try:
        return transport.get_returncode() is None
    except Exception:
        return False


def _child_pid(engine: Any) -> Optional[int]:
    transport = getattr(engine, "transport", None)
    if transport is None:
        return None
    try:
        return transport.get_pid()
    except Exception:
        return None


#: Every child PID this module has spawned.  Used by the atexit net so that even
#: a process that dies (or whose event loop is already gone) cannot leave a
#: Stockfish orphan behind.
_SPAWNED_PIDS: set = set()
_SPAWNED_LOCK = threading.Lock()


def _record_pid(engine: Any) -> None:
    pid = _child_pid(engine)
    if pid:
        with _SPAWNED_LOCK:
            _SPAWNED_PIDS.add(pid)


def _forget_pid(pid: Optional[int]) -> None:
    if not pid:
        return
    with _SPAWNED_LOCK:
        _SPAWNED_PIDS.discard(pid)


def outstanding_engine_pids() -> List[int]:
    """PIDs of engine processes this process has spawned and not yet terminated.

    A non-empty result at shutdown (or after ``close()``) means an orphan.
    """
    with _SPAWNED_LOCK:
        return sorted(_SPAWNED_PIDS)


def _force_kill_process(engine: Any) -> bool:
    """Loop-independent kill of an engine child process.

    Needed because a graceful ``quit()`` requires a *live* event loop: if the
    loop that created the engine has already been closed (e.g. a second
    ``asyncio.run()`` reuses a pool built by the first one) the polite path can
    never complete and the process would be orphaned.  We are the child's
    parent, so ``SIGKILL`` + ``waitpid`` guarantees no zombie and no orphan.
    """
    pid = _child_pid(engine)
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        _forget_pid(pid)
        return False
    for _ in range(100):
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            break
        if done == pid:
            break
        time.sleep(0.01)
    _forget_pid(pid)
    logger.warning("force-killed engine process pid=%s (no live event loop to quit it)", pid)
    return True


def kill_stray_child_engines() -> List[int]:
    """Last-resort net: SIGKILL any Stockfish child *of this interpreter*.

    ``chess.engine`` starts one **non-daemon** thread per engine process, so a
    single engine that is never ``quit()`` makes ``threading._shutdown()`` block
    forever and the interpreter hangs on exit (no traceback, no cleanup).  Engine
    lifecycle is centralised in this module, but a caller that spawns
    ``SimpleEngine`` directly would deadlock the whole process -- so on the way
    out we sweep our own children.  Only direct children whose cmdline mentions
    stockfish are touched, and only at interpreter exit.
    """
    killed: List[int] = []
    try:
        parent = os.getpid()
    except Exception:  # pragma: no cover
        return killed
    try:
        entries = os.listdir("/proc")
    except OSError:  # pragma: no cover - non-Linux
        return killed
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry, "rb") as fh:
                # "pid (comm) state ppid ..." -- comm may contain spaces/parens.
                fields = fh.read().rsplit(b")", 1)[1].split()
            if int(fields[1]) != parent:
                continue
            with open("/proc/%s/cmdline" % entry, "rb") as fh:
                cmdline = fh.read().decode("utf-8", "replace").lower()
        except (OSError, IndexError, ValueError):
            continue
        if "stockfish" not in cmdline:
            continue
        pid = int(entry)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        _forget_pid(pid)
        killed.append(pid)
        logger.warning("force-killed stray engine child pid=%s at interpreter exit", pid)
    return killed


# ---------------------------------------------------------------------------
# Async engine pool (SSE / eval bar)
# ---------------------------------------------------------------------------
class AsyncEnginePool:
    """Bounded, reusable pool of ``chess.engine.UciProtocol`` processes.

    * at most ``max_size`` engine processes exist at any time;
    * a slot is held for the whole duration of a search, so one engine is never
      driven by two coroutines at once;
    * waiting for a slot is **bounded** -- saturation raises
      :class:`EngineBusy` instead of queueing forever;
    * slots are released **synchronously** in ``finally`` blocks, so a cancelled
      or abandoned generator can never leak a slot;
    * ``stop``/``kill`` happens on a best-effort async path, and anything that
      could not be reaped synchronously is reaped by the next ``acquire()``.
    """

    def __init__(
        self,
        stockfish_path: str = STOCKFISH_PATH,
        max_size: Optional[int] = None,
        wait_timeout: Optional[float] = None,
        idle_seconds: Optional[float] = None,
        options: Optional[Dict[str, int]] = None,
    ):
        self.stockfish_path = stockfish_path
        self.max_size = max(1, int(max_size if max_size is not None else ASYNC_POOL_MAX_SIZE))
        self.wait_timeout = ASYNC_POOL_WAIT_TIMEOUT if wait_timeout is None else float(wait_timeout)
        self.idle_seconds = ENGINE_IDLE_SECONDS if idle_seconds is None else float(idle_seconds)
        self.options = dict(options or ENGINE_OPTIONS)
        self._sem = asyncio.Semaphore(self.max_size)
        # engine -> (engine, released_at, event_loop_it_belongs_to)
        self._free: "OrderedDict[int, Tuple[Any, float, Any]]" = OrderedDict()
        self._retired: List[Any] = []
        self._in_use = 0
        self._peak_in_use = 0
        self._spawned = 0
        self._closed = False

    # -- introspection (used by tests + diagnostics) -------------------------
    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def peak_in_use(self) -> int:
        return self._peak_in_use

    @property
    def live(self) -> int:
        """Number of engine processes this pool is currently responsible for."""
        return self._in_use + len(self._free) + len(self._retired)

    @property
    def spawned(self) -> int:
        return self._spawned

    # -- public API ----------------------------------------------------------
    async def acquire(self, timeout: Optional[float] = None) -> Any:
        """Take exclusive ownership of an engine, or raise EngineBusy/Unavailable."""
        if self._closed:
            raise EngineUnavailable("engine pool is closed")
        wait = self.wait_timeout if timeout is None else float(timeout)
        await self._reap_retired()
        try:
            await asyncio.wait_for(self._sem.acquire(), wait)
        except (asyncio.TimeoutError, TimeoutError):
            raise EngineBusy(
                f"engine pool saturated (max_size={self.max_size}, "
                f"waited {wait:.1f}s)"
            )
        self._in_use += 1
        if self._in_use > self._peak_in_use:
            self._peak_in_use = self._in_use
        try:
            return await self._checkout()
        except BaseException:
            self._in_use = max(0, self._in_use - 1)
            self._sem.release()
            raise

    def release_slot(self, engine: Any) -> None:
        """Give a slot back.  Fully synchronous: safe from ``finally``/GeneratorExit."""
        try:
            if engine is not None:
                already_retired = any(e is engine for e in self._retired)
                if not already_retired:
                    if _process_alive(getattr(engine, "transport", None)):
                        try:
                            loop = asyncio.get_event_loop()
                        except RuntimeError:
                            loop = None
                        self._free[id(engine)] = (engine, time.monotonic(), loop)
                    else:
                        self._retired.append(engine)
        finally:
            self._in_use = max(0, self._in_use - 1)
            self._sem.release()

    def retire(self, engine: Any) -> None:
        """Mark an engine unusable (mid-search / crashed).  Synchronous."""
        if engine is None:
            return
        self._free.pop(id(engine), None)
        if not any(e is engine for e in self._retired):
            self._retired.append(engine)

    async def stop_and_retire(self, engine: Any) -> None:
        """Best-effort ``stop`` + retire (used on client disconnect)."""
        if engine is None:
            return
        self.retire(engine)
        try:
            protocol = getattr(engine, "protocol", None)
            stop = getattr(protocol, "stop", None)
            if stop is not None:
                await asyncio.wait_for(stop(), timeout=QUIT_TIMEOUT)
        except Exception as exc:
            logger.debug("engine stop() failed (will be reaped): %s", exc)

    async def close(self) -> None:
        """Kill every engine this pool owns."""
        self._closed = True
        for engine, _ts, _loop in list(self._free.values()):
            await self._kill(engine)
        self._free.clear()
        await self._reap_retired()
        self._in_use = 0

    # -- internals -----------------------------------------------------------
    async def _checkout(self) -> Any:
        """Take a healthy idle engine, or spawn one.

        An engine is bound to the event loop that created it (its UCI reader task
        and queues live there), so an engine whose loop is gone is never reused --
        it is terminated instead.  That is what keeps ``asyncio.run()``-per-call
        callers (scripts, the test suite) from orphaning processes.
        """
        now = time.monotonic()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - always inside a loop
            running = None
        while self._free:
            _key, (engine, ts, loop) = self._free.popitem(last=False)
            if loop is not running:
                self._retired.append(engine)
                await self._kill(engine)
                continue
            if self.idle_seconds > 0 and (now - ts) >= self.idle_seconds:
                await self._kill(engine)
                continue
            if await self._is_alive(engine):
                return engine
            await self._kill(engine)
        return await self._spawn()

    async def _spawn(self) -> Any:
        if not os.path.exists(self.stockfish_path):
            raise FileNotFoundError(f"Stockfish binary not found at {self.stockfish_path}")
        try:
            _, engine = await chess.engine.popen_uci(self.stockfish_path)
        except Exception as exc:
            raise EngineUnavailable(sanitize_error(exc)) from exc
        self._spawned += 1
        _record_pid(engine)
        try:
            await engine.configure(self.options)
        except Exception as exc:
            await self._kill(engine)
            raise EngineUnavailable(sanitize_error(exc)) from exc
        return engine

    async def _is_alive(self, engine: Any) -> bool:
        if engine is None:
            return False
        if not _process_alive(getattr(engine, "transport", None)):
            return False
        try:
            await asyncio.wait_for(engine.ping(), timeout=PING_TIMEOUT)
            return True
        except Exception:
            return False

    async def _reap_retired(self) -> None:
        while self._retired:
            engine = self._retired.pop()
            await self._kill(engine)

    async def _kill(self, engine: Any) -> None:
        if engine is None:
            return
        pid = _child_pid(engine)
        for name in ("quit", "close"):
            fn = getattr(engine, name, None)
            if fn is None:
                continue
            try:
                await asyncio.wait_for(fn(), timeout=QUIT_TIMEOUT)
                _forget_pid(pid)
                return
            except Exception as exc:
                logger.debug("engine %s() failed: %s", name, exc)
        # No live event loop (or the process ignores UCI): never leave it behind.
        _force_kill_process(engine)


# ---------------------------------------------------------------------------
# Sync engine pool (analyzer / sync routes)
# ---------------------------------------------------------------------------
class SyncEnginePool:
    """Bounded, reusable pool of ``chess.engine.SimpleEngine`` processes.

    Before this existed every ``GameAnalyzer`` built its own process, so N
    concurrent ``/api/sync`` or ``/api/train/evaluate_move`` calls meant N
    Stockfish processes (N x 2 threads + N x 64 MB hash).  With this pool the
    number of live processes is hard-capped at ``max_size`` and engines are
    recycled between requests.
    """

    def __init__(
        self,
        stockfish_path: str = STOCKFISH_PATH,
        max_size: Optional[int] = None,
        wait_timeout: Optional[float] = None,
        idle_seconds: Optional[float] = None,
        options: Optional[Dict[str, int]] = None,
    ):
        self.stockfish_path = stockfish_path
        self.max_size = max(1, int(max_size if max_size is not None else SYNC_POOL_MAX_SIZE))
        self.wait_timeout = SYNC_POOL_WAIT_TIMEOUT if wait_timeout is None else float(wait_timeout)
        self.idle_seconds = ENGINE_IDLE_SECONDS if idle_seconds is None else float(idle_seconds)
        self.options = dict(options or ENGINE_OPTIONS)
        self._sem = threading.BoundedSemaphore(self.max_size)
        self._lock = threading.Lock()
        self._free: "OrderedDict[int, Tuple[Any, float]]" = OrderedDict()
        self._in_use = 0
        self._peak_in_use = 0
        self._spawned = 0
        self._closed = False

    # -- introspection (used by tests + diagnostics) -------------------------
    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    @property
    def peak_in_use(self) -> int:
        with self._lock:
            return self._peak_in_use

    @property
    def idle(self) -> int:
        with self._lock:
            return len(self._free)

    @property
    def live(self) -> int:
        with self._lock:
            return self._in_use + len(self._free)

    @property
    def spawned(self) -> int:
        with self._lock:
            return self._spawned

    @property
    def pids(self) -> List[int]:
        with self._lock:
            engines = [e for e, _ in self._free.values()]
        return [pid for pid in (_child_pid(e) for e in engines) if pid]

    # -- public API ----------------------------------------------------------
    def acquire(self, timeout: Optional[float] = None) -> Any:
        """Take exclusive ownership of a synchronous engine.

        Raises ``FileNotFoundError`` (unchanged legacy contract) if the binary
        is missing, or :class:`EngineBusy` if the pool stays saturated for
        ``timeout`` seconds.
        """
        if self._closed:
            raise EngineUnavailable("engine pool is closed")
        wait = self.wait_timeout if timeout is None else float(timeout)
        if not self._sem.acquire(timeout=wait):
            raise EngineBusy(
                f"engine pool saturated (max_size={self.max_size}, waited {wait:.1f}s)"
            )
        with self._lock:
            self._in_use += 1
            if self._in_use > self._peak_in_use:
                self._peak_in_use = self._in_use
        try:
            return self._checkout()
        except BaseException:
            with self._lock:
                self._in_use = max(0, self._in_use - 1)
            self._sem.release()
            raise

    def release(self, engine: Any) -> None:
        """Return a healthy engine to the pool for reuse."""
        self._give_back(engine, keep=True)

    def discard(self, engine: Any) -> None:
        """Terminate an engine instead of recycling it."""
        self._give_back(engine, keep=False)

    @contextlib.contextmanager
    def engine(self, timeout: Optional[float] = None):
        engine = self.acquire(timeout=timeout)
        try:
            yield engine
        except BaseException:
            self.discard(engine)
            raise
        else:
            self.release(engine)

    def close_all(self) -> None:
        """Terminate every idle engine (safe to call more than once)."""
        with self._lock:
            self._closed = True
            free = [e for e, _ in self._free.values()]
            self._free.clear()
        for engine in free:
            _quit_sync_engine(engine)

    # -- internals -----------------------------------------------------------
    def _checkout(self) -> Any:
        now = time.monotonic()
        while True:
            with self._lock:
                entry = self._free.popitem(last=False) if self._free else None
            if entry is None:
                break
            _key, (engine, ts) = entry
            if self.idle_seconds > 0 and (now - ts) >= self.idle_seconds:
                _quit_sync_engine(engine)
                continue
            if _sync_engine_healthy(engine):
                return engine
            _quit_sync_engine(engine)
        return self._spawn()

    def _spawn(self) -> Any:
        if not os.path.exists(self.stockfish_path):
            raise FileNotFoundError(f"Stockfish binary not found at {self.stockfish_path}")
        try:
            engine = chess.engine.SimpleEngine.popen_uci(self.stockfish_path)
        except Exception as exc:
            raise EngineUnavailable(sanitize_error(exc)) from exc
        _record_pid(engine)
        try:
            engine.configure(self.options)
        except Exception as exc:
            _quit_sync_engine(engine)
            raise EngineUnavailable(sanitize_error(exc)) from exc
        with self._lock:
            self._spawned += 1
        return engine

    def _give_back(self, engine: Any, keep: bool) -> None:
        try:
            with self._lock:
                self._in_use = max(0, self._in_use - 1)
                if keep and engine is not None and len(self._free) < self.max_size:
                    self._free[id(engine)] = (engine, time.monotonic())
                    engine = None
        finally:
            self._sem.release()
        if engine is not None:
            _quit_sync_engine(engine)


def _sync_engine_healthy(engine: Any) -> bool:
    if not _process_alive(getattr(engine, "transport", None)):
        return False
    try:
        engine.ping()
        return True
    except Exception as exc:
        logger.debug("pooled engine failed liveness ping: %s", exc)
        return False


def _quit_sync_engine(engine: Any) -> None:
    if engine is None:
        return
    pid = _child_pid(engine)
    for name in ("quit", "close"):
        fn = getattr(engine, name, None)
        if fn is None:
            continue
        try:
            fn()
            _forget_pid(pid)
            return
        except Exception as exc:
            logger.debug("sync engine %s() failed: %s", name, exc)
    # Last resort: never leave a Stockfish process behind.
    _force_kill_process(engine)


_sync_pool: Optional[SyncEnginePool] = None
_sync_pool_lock = threading.Lock()


def get_sync_engine_pool(stockfish_path: str = STOCKFISH_PATH) -> SyncEnginePool:
    """Process-wide singleton pool for the synchronous engine users."""
    global _sync_pool
    with _sync_pool_lock:
        if _sync_pool is None or _sync_pool.stockfish_path != stockfish_path:
            if _sync_pool is not None:
                _sync_pool.close_all()
            _sync_pool = SyncEnginePool(stockfish_path)
        return _sync_pool


def _atexit_shutdown_sync_pool() -> None:
    """Interpreter-exit net: close the sync pool, then SIGKILL anything left.

    Registered with ``threading._register_atexit`` **and not only**
    ``atexit.register`` because python-chess runs each engine on a *non-daemon*
    thread, and ``threading._shutdown()`` joins non-daemon threads *before* any
    ``atexit`` callback runs.  With a pooled (still-alive) engine that join
    blocks forever and the process hangs on exit.  Registering on the threading
    hook is what keeps the exit path prompt.

    The second half (SIGKILL of every PID we ever spawned) matters because a UCI
    ``quit()`` needs a live event loop: if the loop is already gone, the polite
    path cannot run and the child would be orphaned.
    """
    pool = _sync_pool
    if pool is not None:
        try:
            pool.close_all()
        except Exception:  # pragma: no cover - interpreter shutdown
            pass
    with _SPAWNED_LOCK:
        leftovers = list(_SPAWNED_PIDS)
    for pid in leftovers:
        try:
            os.kill(pid, signal.SIGKILL)
            _forget_pid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            _forget_pid(pid)
        except Exception:  # pragma: no cover - interpreter shutdown
            pass
    # Anything still parented by us (spawned outside these pools) would keep a
    # non-daemon chess.engine thread alive and hang `threading._shutdown()`.
    kill_stray_child_engines()


# Must run *before* non-daemon threads are joined (see docstring).
if hasattr(threading, "_register_atexit"):
    threading._register_atexit(_atexit_shutdown_sync_pool)
atexit.register(_atexit_shutdown_sync_pool)


# ---------------------------------------------------------------------------
# Evaluator / SSE stream
# ---------------------------------------------------------------------------
class AsyncEngineEvaluator:
    """Owns the async engine pool, the position eval cache, and the SSE stream."""

    _instance: Optional["AsyncEngineEvaluator"] = None

    def __init__(self, stockfish_path: str = STOCKFISH_PATH, pool: Optional[AsyncEnginePool] = None):
        self.stockfish_path = stockfish_path
        self.pool = pool if pool is not None else AsyncEnginePool(stockfish_path)
        self._cache: "OrderedDict[str, Dict]" = OrderedDict()
        self._cache_max_size = 1000

    @classmethod
    def get_instance(cls) -> "AsyncEngineEvaluator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def engine_pool(self) -> AsyncEnginePool:
        return self.pool

    async def close(self):
        await self.pool.close()

    # -- cache ---------------------------------------------------------------
    def get_cached_eval(self, fen: str) -> Optional[Dict]:
        norm = normalize_fen(fen)
        if norm in self._cache:
            self._cache.move_to_end(norm)
            return self._cache[norm]
        return None

    def _set_cached_eval(self, fen: str, data: Dict):
        norm = normalize_fen(fen)
        existing = self._cache.get(norm)
        if existing and existing.get("depth", 0) > data.get("depth", 0):
            return
        self._cache[norm] = data
        if len(self._cache) > self._cache_max_size:
            self._cache.popitem(last=False)

    # -- stream --------------------------------------------------------------
    async def stream_eval(
        self,
        fen: str,
        max_depth: int = 16,
        request: Optional[Request] = None,
    ) -> AsyncGenerator[str, None]:
        """Progressively yield SSE evaluation events for ``fen`` (contract v1).

        Guarantees:
        * every frame is a complete ``data:``-first event block;
        * any failure emits a typed ``type="error"`` frame carrying ``final:
          true`` and then **terminates** the stream;
        * the engine slot is released on every exit path, including client
          disconnect / generator abandonment, so no engine process is leaked.
        """
        max_depth = max(1, min(30, int(max_depth)))
        norm_fen = normalize_fen(fen)

        if request and await request.is_disconnected():
            return

        try:
            board = chess.Board(fen)
            if not board.is_valid():
                yield _frame(build_error_event(ERR_INVALID_FEN, "Invalid FEN", fen=norm_fen))
                return
        except Exception:
            yield _frame(build_error_event(ERR_INVALID_FEN, "Invalid FEN", fen=norm_fen))
            return

        # Game over (checkmate / stalemate / insufficient material): one final frame.
        if board.is_checkmate():
            # Black to move and mated  =>  White wins.
            mate = -1 if board.turn == chess.WHITE else 1
            yield _frame(build_eval_event(
                depth=99,
                score_cp=-MATE_SCORE_CP if board.turn == chess.WHITE else MATE_SCORE_CP,
                mate=mate,
                final=True,
                fen=norm_fen,
            ))
            return

        if board.is_stalemate() or board.is_insufficient_material():
            yield _frame(build_eval_event(
                depth=99, score_cp=0, mate=None, final=True, fen=norm_fen,
            ))
            return

        cached = self.get_cached_eval(fen)
        if cached:
            replayed = build_eval_event(
                depth=int(cached.get("depth", 0)),
                score_cp=cached.get("score_cp"),
                mate=cached.get("mate"),
                best_move=cached.get("best_move", ""),
                best_san=cached.get("best_san", ""),
                pv=cached.get("pv"),
                final=int(cached.get("depth", 0)) >= max_depth,
                cached=True,
                fen=norm_fen,
            )
            yield _frame(replayed)
            if int(cached.get("depth", 0)) >= max_depth:
                return

        if request and await request.is_disconnected():
            return

        # Bounded checkout: never queue forever behind a stalled/slow reader.
        try:
            engine = await self.pool.acquire()
        except EngineBusy as exc:
            logger.warning("eval stream rejected: %s", exc)
            yield _frame(build_error_event(ERR_ENGINE_BUSY, sanitize_error(exc), fen=norm_fen))
            return
        except FileNotFoundError as exc:
            logger.error("eval stream: %s", exc)
            yield _frame(build_error_event(ERR_ENGINE_UNAVAILABLE, sanitize_error(exc), fen=norm_fen))
            return
        except Exception as exc:
            logger.exception("eval stream: engine checkout failed")
            yield _frame(build_error_event(ERR_ENGINE_UNAVAILABLE, sanitize_error(exc), fen=norm_fen))
            return

        try:
            last_res: Optional[Dict[str, Any]] = None
            last_emitted_depth = 0
            with await engine.analysis(board, chess.engine.Limit(depth=max_depth)) as analysis:
                async for info in analysis:
                    if "score" not in info or "depth" not in info:
                        continue
                    if request and await request.is_disconnected():
                        break
                    depth = int(info["depth"])
                    if depth == last_emitted_depth:
                        continue
                    if not (depth <= 4 or depth % 3 == 0 or depth >= max_depth):
                        continue
                    last_emitted_depth = depth

                    score_obj = info["score"].white()  # every score is White POV
                    score_cp = score_obj.score()
                    mate = score_obj.mate()
                    if score_cp is None and mate is None:
                        continue

                    # The line is re-read on *every* emitted frame, which is what
                    # makes it evolve with depth: at depth 4 it is a crude
                    # principal line, at depth 16 it is the move the user is
                    # actually looking for.  `best_move`/`best_san` stay exactly
                    # the first ply of it, so the two never disagree.
                    line = pv_line(board, info.get("pv", []), PV_MAX_PLIES)
                    best_move_uci = line[0]["uci"] if line else ""
                    best_move_san = line[0]["san"] if line else ""

                    last_res = build_eval_event(
                        depth=depth,
                        score_cp=score_cp,
                        mate=mate,
                        best_move=best_move_uci,
                        best_san=best_move_san,
                        final=(depth >= max_depth),
                        fen=norm_fen,
                        pv=line,
                    )
                    yield _frame(last_res)

            # Only cache when the search actually reached max_depth.
            if last_res and last_res.get("depth", 0) >= max_depth:
                last_res["final"] = True
                self._set_cached_eval(fen, last_res)
        except (asyncio.CancelledError, GeneratorExit):
            # Client went away (or the generator was abandoned).  Stop the search
            # and retire the engine so the next request gets a clean one; the
            # slot itself is handed back synchronously in the `finally` below.
            await _shielded(self.pool.stop_and_retire(engine))
            raise
        except Exception as exc:
            logger.exception("eval stream engine failure for fen=%s", norm_fen)
            self.pool.retire(engine)
            yield _frame(build_error_event(ERR_ENGINE_ERROR, sanitize_error(exc), fen=norm_fen))
            return
        finally:
            # Synchronous, exception-proof slot hand-back.  This is the single
            # place the engine is returned, so no exit path (success, error,
            # client disconnect, abandoned generator) can leak a slot.
            self.pool.release_slot(engine)


async def _shielded(coro) -> None:
    """Await a cleanup coroutine, swallowing any error (used from `finally`)."""
    try:
        await coro
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.debug("cleanup coroutine failed: %s", exc)
