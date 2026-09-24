#!/usr/bin/env python3
"""
ATK5-08 repro: Stockfish subprocess abuse / engine-sharing races.

 A) RepertoireManager._engine is a SINGLE shared SimpleEngine used from the
    *sync* route /api/repertoire/explore (threadpool). python-chess documents
    that "sending a new command to the engine ... any previous running command
    will be cancelled". The resulting exception is
    concurrent.futures.CancelledError, which inherits from **BaseException**
    and is therefore NOT caught by the `except Exception` at
    repertoire.py:322 -> uncaught -> HTTP 500.
 B) Cold-start race in _get_engine(): check-then-set with no lock spawns two
    stockfish processes and orphans one.
 C) Engine child-process leak: api.py /api/sync and repertoire.py
    analyze_user_games call analyzer.close() WITHOUT finally -> an exception
    mid-loop orphans the stockfish process (64 MB hash + 2 threads).
 D) eval_stream: no time/move limit, and a slow reader can monopolise the
    single async engine lock, starving every other client.

Idempotent, isolated temp DB, network stubbed. Stockfish IS spawned (bounded).
"""
import os
import subprocess
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # MUST be first backend-adjacent import

import requests


class _R:
    status_code = 503
    text = "stub"

    def json(self):
        return {}

    def iter_lines(self, decode_unicode=False):
        return iter(())


requests.get = lambda *a, **k: _R()
requests.post = lambda *a, **k: _R()
import yt_dlp  # noqa: E402


class _Y:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, *a, **k):
        return None


yt_dlp.YoutubeDL = _Y

import chess  # noqa: E402
import chess.engine  # noqa: E402
from backend.repertoire import RepertoireManager  # noqa: E402

print("=" * 78)
print("A) shared SimpleEngine + concurrent analyse() -> CancelledError escapes")
print("=" * 78)
rm = RepertoireManager.get_instance()
eng = rm._get_engine()
print("  engine pid:", eng.transport.get_pid())

POSITIONS = [
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
    "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2",
    "r2q1rk1/pp1nbppp/2p1pn2/3p4/2PP4/2N1PN2/PPQ1BPPP/R1B2RK1 w - - 0 9",
]
results = {}
barrier = threading.Barrier(len(POSITIONS))


def worker(i, fen):
    board = chess.Board(fen)
    barrier.wait()
    try:
        # EXACTLY what repertoire.py:318 does
        info = eng.analyse(board, chess.engine.Limit(depth=10))
        results[i] = ("ok", info["pv"][0].uci())
    except Exception as e:
        results[i] = (f"EXC {type(e).__module__}.{type(e).__name__}", str(e)[:90])
    except BaseException as e:
        results[i] = (f"BASEXC {type(e).__module__}.{type(e).__name__} "
                      f"(isinstance of Exception? {isinstance(e, Exception)})", str(e)[:90])


ts = [threading.Thread(target=worker, args=(i, f)) for i, f in enumerate(POSITIONS)]
[t.start() for t in ts]
[t.join() for t in ts]
for i, v in sorted(results.items()):
    print(f"   worker {i}: {v}")
esc = [v for v in results.values() if v[0].startswith(("EXC", "BASEXC"))]
print(f"  >>> {len(esc)}/{len(POSITIONS)} concurrent analyses failed. "
      f"CancelledError is a BaseException -> NOT caught by `except Exception` in "
      f"get_candidate_moves_2000 -> HTTP 500 for the unlucky request.")
rm.close()

print()
print("=" * 78)
print("B) cold-start race in _get_engine() (no lock) -> two stockfish procs")
print("=" * 78)


def count_stockfish():
    out = subprocess.run(["pgrep", "-fc", "stockfish"], capture_output=True, text=True).stdout.strip()
    return int(out or 0)


before = count_stockfish()
RepertoireManager._instance = None
got = []
bar2 = threading.Barrier(8)


def cold(i):
    bar2.wait()
    m = RepertoireManager.get_instance()
    e = m._get_engine()
    got.append(getattr(getattr(e, "transport", None), "get_pid", lambda: None)())


ts = [threading.Thread(target=cold, args=(i,)) for i in range(8)]
[t.start() for t in ts]
[t.join() for t in ts]
time.sleep(1.0)
after = count_stockfish()
pids = [p for p in got if p]
print(f"  stockfish processes before: {before}  after 8 cold-start threads: {after}")
print(f"  distinct engine pids handed out: {sorted(set(pids))}")
print(f"  RepertoireManager._instance engine pid: "
      f"{getattr(RepertoireManager._instance._engine.transport, 'get_pid', lambda: None)()}")
print(f"  >>> {len(set(pids))} engine(s) spawned for one singleton; "
      f"{after - before} stockfish procs alive (orphaned ones are never quit()).")
for p in sorted(set(pids)):
    if p and p != RepertoireManager._instance._engine.transport.get_pid():
        print(f"      orphan pid {p} still running -> killing")
        subprocess.run(["kill", "-9", str(p)], capture_output=True)
RepertoireManager._instance.close()

print()
print("=" * 78)
print("C) engine child-process leak: analyzer.close() is not in a `finally`")
print("=" * 78)
from backend.analyzer import GameAnalyzer  # noqa: E402

n0 = count_stockfish()
a = GameAnalyzer()
e = a._get_engine()
pid = e.transport.get_pid()
print(f"  spawned stockfish pid {pid}; alive count {n0} -> {count_stockfish()}")
# emulate: an exception escapes analyze_game_openings -> api.py:268 never runs
try:
    raise RuntimeError("simulated mid-sync failure (e.g. malformed game from Lichess)")
except RuntimeError:
    pass
print("  (api.py:261 `blunders = analyzer.analyze_game_openings(g)` raising means")
print("   api.py:268 `analyzer.close()` is skipped -> the child keeps running)")
print(f"  stockfish pid {pid} still alive: {subprocess.run(['kill','-0',str(pid)],capture_output=True).returncode == 0}")
a.close()
time.sleep(0.5)
print(f"  after a.close(): alive = {subprocess.run(['kill','-0',str(pid)],capture_output=True).returncode == 0}")

print()
print("=" * 78)
print("D) eval_stream: depth-only Limit, no time cap; slow reader starves others")
print("=" * 78)
import inspect  # noqa: E402
from backend.evaluator import AsyncEngineEvaluator  # noqa: E402
src = inspect.getsource(AsyncEngineEvaluator.stream_eval)
print("  Limit(...) call sites in stream_eval:",
      [l.strip() for l in src.splitlines() if "chess.engine.Limit(" in l])
print("  a single asyncio.Lock guards the ONE engine:", "async with self._lock" in src)
print("  no semaphore / no queue cap on concurrent streams:",
      "Semaphore" not in src and "MAX_CONCURRENT" not in src)
print("  => a client that opens GET /api/repertoire/eval_stream and simply never")
print("     reads the response holds the engine lock (TCP back-pressure stops the")
print("     generator) and every other eval request queues behind it. depth is")
print("     accepted up to 30 (api.py:998) with no time limit.")

print()
print("DONE")
