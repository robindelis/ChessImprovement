"""FIXER-C-01 repro / verification probe  (works on BOTH the pre- and post-fix code).

Usage:
    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-C-01_repro.py

Sections
    A) SSE eval frame: which keys does the stream actually publish, and what does
       frontend/scout.js (`data.eval`) see?
    B) SSE error frame: is the failure typed and terminal?
    C) Engine process accounting: 8 concurrent GameAnalyzer sessions -- how many
       Stockfish processes exist at once, and how many spawns for 5 sequential
       sessions?

No real Stockfish process is started (both spawn sites are stubbed and counted).
"""
import os
import sys
import json
import asyncio
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# --------------------------------------------------------------------------
# tests.test_setup must be the first backend-adjacent import (hard rule).  It
# currently aborts when another agent's tests/fixtures.py seed is mid-edit, so
# fall back to a no-op seed *loudly* rather than silently touching production.
# This probe never reads or writes the DB -- it only needs the temp-path redirect.
# --------------------------------------------------------------------------
_stubbed = False
try:
    import tests.test_setup  # noqa: E402,F401
except Exception as exc:  # pragma: no cover - depends on concurrent edits
    for _name in [n for n in list(sys.modules) if n == "tests" or n.startswith("tests.")]:
        del sys.modules[_name]
    import types  # noqa: E402

    _stub = types.ModuleType("tests.fixtures")
    _stub.seed_synthetic = lambda conn: {"probe": "no-op seed"}
    sys.modules["tests.fixtures"] = _stub
    import tests.test_setup  # noqa: E402,F401  (re-import, now with the stub)

    _stubbed = True
    print("!! WARNING: tests.test_setup failed (%s: %s)" % (type(exc).__name__, exc))
    print("!! WARNING: continuing with a NO-OP synthetic seed; this probe never")
    print("!! WARNING: reads or writes any database (temp-path redirect only).")
    print(flush=True)

import chess  # noqa: E402
import chess.engine  # noqa: E402

from backend.evaluator import AsyncEngineEvaluator, STOCKFISH_PATH  # noqa: E402
from backend.analyzer import GameAnalyzer  # noqa: E402

FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKB1R w KQkq - 0 1"


# --------------------------------------------------------------------------
# spawn accounting
# --------------------------------------------------------------------------
LIVE = {"spawns": 0, "quits": 0, "peak": 0, "in_use": 0}
LOCK = threading.Lock()


def _spawn():
    with LOCK:
        LIVE["spawns"] += 1


def _quit():
    with LOCK:
        LIVE["quits"] += 1


def _enter():
    with LOCK:
        LIVE["in_use"] += 1
        LIVE["peak"] = max(LIVE["peak"], LIVE["in_use"])


def _exit():
    with LOCK:
        LIVE["in_use"] = max(0, LIVE["in_use"] - 1)


def _reset():
    with LOCK:
        LIVE["spawns"] = 0
        LIVE["quits"] = 0
        LIVE["peak"] = 0
        LIVE["in_use"] = 0


class FakeTransport:
    def __init__(self):
        self.alive = True

    def is_closing(self):
        return not self.alive

    def get_returncode(self):
        return None if self.alive else 0

    def get_pid(self):
        return 99999


class FakeSyncEngine:
    _next_pid = [41000]

    def __init__(self):
        self.transport = FakeTransport()
        FakeSyncEngine._next_pid[0] += 1
        self.pid = FakeSyncEngine._next_pid[0]
        _spawn()

    def configure(self, options):
        return "ok"

    def ping(self):
        # is_closing() == True  =>  the child process is gone => unhealthy.
        if self.transport.is_closing():
            raise chess.engine.EngineTerminatedError("engine is dead")
        return None

    def analyse(self, board, limit):
        return {"score": chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE), "pv": []}

    def quit(self):
        self.transport.alive = False
        _quit()

    def close(self):
        self.quit()


class _Analysis:
    def __init__(self, engine):
        self.engine = engine
        self.remaining = [
            {"depth": d, "score": chess.engine.PovScore(chess.engine.Cp(cp), chess.WHITE),
             "pv": [chess.Move.from_uci("e2e4")]}
            for d, cp in ((4, 120), (6, 145))
        ]

    def __await__(self):
        async def _i():
            return self
        return _i().__await__()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self.remaining:
            raise StopAsyncIteration
        return self.remaining.pop(0)


class FakeAsyncEngine:
    def __init__(self):
        self.transport = FakeTransport()

    async def configure(self, options):
        return None

    async def ping(self):
        return True

    def analysis(self, board, limit):
        return _Analysis(self)

    async def quit(self):
        self.transport.alive = False

    async def close(self):
        self.transport.alive = False


def install_stubs():
    def sync_popen(path, *a, **kw):
        return FakeSyncEngine()

    async def async_popen(path, *a, **kw):
        return (None, FakeAsyncEngine())

    chess.engine.SimpleEngine.popen_uci = sync_popen
    chess.engine.popen_uci = async_popen


def collect(stream):
    async def _go():
        return [f async for f in stream]
    return asyncio.run(_go())


def first_payload(frame):
    line = next(l for l in frame.split("\n") if l.startswith("data: "))
    return json.loads(line[6:])


# --------------------------------------------------------------------------
def section_a():
    print("=" * 78)
    print("A) SSE eval frame keys  (GET /api/repertoire/eval_stream)")
    print("=" * 78)
    ev = AsyncEngineEvaluator(STOCKFISH_PATH)
    frames = collect(ev.stream_eval(FEN, max_depth=6))
    payload = first_payload(frames[0])
    print("  first frame keys      :", sorted(payload.keys()))
    print("  payload               :", json.dumps(payload, sort_keys=True))
    print("  has 'score_cp'        :", "score_cp" in payload, " <- repertoire_builder.js")
    print("  has 'eval'            :", "eval" in payload, " <- scout.js (data.eval)")
    print("  has 'depth'           :", isinstance(payload.get("depth"), int))
    print("  has 'mate'/'is_mate'  :", "mate" in payload, "/", payload.get("is_mate"))
    # Replay scout.js:_updateEvalBar(data.eval, data.mate) verbatim.
    score_cp, mate = payload.get("eval"), payload.get("mate")
    print("  scout.js sees scoreCp :", score_cp)
    if score_cp is None:
        print("  -> scout.js eval bar  : FROZEN at '+0.0' / 50%  (CODE-02-23 reproduced)")
    else:
        pct = 50 + 50 * (2 / (1 + __import__("math").exp(-0.004 * score_cp)) - 1)
        print("  -> scout.js eval bar  : %.1f%% (white to move, +1.20 expected)" % pct)
    if mate is not None:
        print("  -> mate rendering     : M%s (authoritative)" % abs(mate))
    return "eval" in payload


def section_b():
    print()
    print("=" * 78)
    print("B) SSE error frame (engine binary missing)")
    print("=" * 78)
    ev = AsyncEngineEvaluator("/nonexistent/bin/stockfish")
    frames = collect(ev.stream_eval(FEN, max_depth=4))
    print("  frames emitted        :", len(frames), "(1 => stream terminates)")
    payload = first_payload(frames[0])
    print("  payload               :", json.dumps(payload, sort_keys=True))
    print("  has discriminating 'type':", "type" in payload, "->", payload.get("type"))
    print("  has stable 'code'     :", "code" in payload, "->", payload.get("code"))
    print("  terminal ('final')    :", payload.get("final"))
    print("  leaks a path          :", "/" in str(payload.get("error", "")))
    return ("type" in payload and payload.get("final") is True)


def section_c():
    print()
    print("=" * 78)
    print("C) Engine process accounting")
    print("=" * 78)
    # C1: 5 sequential GameAnalyzer sessions must reuse one process.
    _reset()
    sessions = []
    for _ in range(5):
        a = GameAnalyzer()
        sessions.append(a._get_engine())
        a.close()
    print("  C1 sequential sessions=5  spawns=%d  distinct_processes=%d  peak_concurrent=%d"
          % (LIVE["spawns"], len({e.pid for e in sessions}), LIVE["peak"]))

    # C2: 8 concurrent GameAnalyzer sessions -- peak simultaneous engine users.
    _reset()
    barrier = threading.Barrier(8)

    def worker():
        a = GameAnalyzer()
        try:
            barrier.wait(timeout=10)
            e = a._get_engine()
            _enter()
            try:
                time.sleep(0.25)      # simulate a search
            finally:
                _exit()
        except Exception as exc:
            print("     worker error:", type(exc).__name__, exc)
        finally:
            a.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    print("  C2 concurrent sessions=8  spawns=%d  PEAK_CONCURRENT_ENGINES=%d  "
          "quits=%d  leak=%d" % (LIVE["spawns"], LIVE["peak"], LIVE["quits"],
                                 LIVE["spawns"] - LIVE["quits"]))
    return LIVE["peak"] <= 2 and LIVE["spawns"] <= 2


if __name__ == "__main__":
    install_stubs()
    a_ok = section_a()
    b_ok = section_b()
    c_ok = section_c()
    print()
    print("=" * 78)
    print("VERDICT  payload_has_eval=%s  typed_terminal_error=%s  engine_cap<=2=%s"
          % (a_ok, b_ok, c_ok))
    print("=" * 78)
    sys.exit(0 if (a_ok and b_ok and c_ok) else 1)
