"""FIXER-C-01 real-engine verification: launch the actual Stockfish binary for a
few seconds on a trivial position (low depth) through the production code paths
(async SSE pool + sync analyzer pool), then prove every child process is gone.

Run:  /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-C-01_realengine.py
The process tree is sampled with `ps` before / during / after.
"""
import os
import re
import sys
import json
import time
import asyncio
import subprocess
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # noqa: E402  -- MUST be first (isolated temp DB)

import chess  # noqa: E402

from backend.evaluator import (  # noqa: E402
    AsyncEngineEvaluator,
    AsyncEnginePool,
    STOCKFISH_PATH,
    SyncEnginePool,
    get_sync_engine_pool,
    outstanding_engine_pids,
)
from backend.analyzer import GameAnalyzer  # noqa: E402

TRIVIAL = "8/8/8/4k3/8/8/4P3/4K3 w - - 0 1"


def stockfish_pids():
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        if "stockfish" in line and "grep" not in line and "ps -eo" not in line:
            if re.search(r"stockfish-linux-x86-64-universal", line):
                pids.append(line.split()[0])
    return sorted(pids, key=int)


def banner(text):
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def main():
    banner("0) BASELINE")
    base = stockfish_pids()
    print("  stockfish PIDs before anything:", base or "(none)")

    banner("1) ASYNC SSE POOL -- 4 concurrent real depth-6 streams, 3 navigations")
    pool = AsyncEnginePool(STOCKFISH_PATH, max_size=2, wait_timeout=60.0)
    ev = AsyncEngineEvaluator(STOCKFISH_PATH, pool=pool)

    async def one(fen):
        frames = []
        async for f in ev.stream_eval(fen, max_depth=6):
            frames.append(f)
        return frames

    async def run_async():
        fens = [TRIVIAL, chess.STARTING_FEN, "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
                "8/5k2/8/8/8/8/4R3/4K3 w - - 0 1"]
        t0 = time.monotonic()
        results = await asyncio.gather(*[one(f) for f in fens])
        return results, time.monotonic() - t0

    sampler_stop = threading.Event()
    sampled = set()

    def sampler():
        while not sampler_stop.is_set():
            sampled.update(stockfish_pids())
            time.sleep(0.05)

    t = threading.Thread(target=sampler, daemon=True)
    t.start()
    results, elapsed = asyncio.run(run_async())
    sampler_stop.set()
    t.join(timeout=2)

    for fen, frames in zip(
        [TRIVIAL, chess.STARTING_FEN,
         "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
         "8/5k2/8/8/8/8/4R3/4K3 w - - 0 1"], results):
        last = json.loads([l for l in frames[-1].split("\n") if l.startswith("data: ")][0][6:])
        print("  frames=%d  final=%s  type=%s  score_cp=%s eval=%s mate=%s is_mate=%s depth=%s"
              % (len(frames), last["final"], last["type"], last["score_cp"],
                 last["eval"], last["mate"], last["is_mate"], last["depth"]))
    print("  wall time for 4 concurrent streams: %.2fs" % elapsed)
    print("  async pool: max_size=%d  spawned=%d  peak_in_use=%d  in_use_now=%d  live_now=%d"
          % (pool.max_size, pool.spawned, pool.peak_in_use, pool.in_use, pool.live))
    print("  PIDs observed while running     :", sorted(sampled, key=int) or "(none)")
    print("  PIDs alive right after streams  :", stockfish_pids() or "(none)")

    banner("2) ABANDONED STREAM (client disconnects mid-search) -- depth 20, cut early")
    peak = {}

    async def abandoned():
        gen = ev.stream_eval(chess.STARTING_FEN, max_depth=20)
        first = await gen.__anext__()
        pending = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0.35)          # let it search
        peak["before_cancel"] = stockfish_pids()
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        try:
            await gen.aclose()
        except Exception:
            pass
        await asyncio.sleep(0.4)
        return first

    first = asyncio.run(abandoned())
    print("  first frame emitted            :", first.splitlines()[0][:90] + "...")
    print("  PIDs while searching           :", peak["before_cancel"] or "(none)")
    print("  PIDs after cancel + aclose     :", stockfish_pids() or "(none)")
    print("  async pool in_use=%d  live=%d (retired engines are reaped on next use)"
          % (pool.in_use, pool.live))
    print("  -- a follow-up request must reap the retired engine --")
    frames = asyncio.run(one(TRIVIAL))
    print("  follow-up stream frames=%d  PIDs now: %s"
          % (len(frames), stockfish_pids() or "(none)"))
    print("  async pool spawned=%d peak_in_use=%d" % (pool.spawned, pool.peak_in_use))

    banner("3) SYNC ANALYZER POOL -- 6 concurrent GameAnalyzer sessions, real analyse")
    get_sync_engine_pool(SyncEnginePool.__module__ and STOCKFISH_PATH)
    sync_pool = SyncEnginePool(STOCKFISH_PATH, max_size=2, wait_timeout=120.0)
    import backend.evaluator as evmod
    saved, evmod._sync_pool = evmod._sync_pool, sync_pool

    results = {}
    barrier = threading.Barrier(6)
    seen = set()

    def worker(i):
        a = GameAnalyzer()
        try:
            barrier.wait(timeout=30)
            engine = a._get_engine()
            info = engine.analyse(chess.Board(TRIVIAL), chess.engine.Limit(depth=8))
            results[i] = (info["score"].white().score(), info.get("depth"))
        except Exception as exc:
            results[i] = ("ERROR", "%s: %s" % (type(exc).__name__, exc))
        finally:
            a.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    sampler2_stop = threading.Event()
    sampled2 = set()

    def sampler2():
        while not sampler2_stop.is_set():
            sampled2.update(stockfish_pids())
            time.sleep(0.05)

    s2 = threading.Thread(target=sampler2, daemon=True)
    s2.start()
    t0 = time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=180)
    sampler2_stop.set()
    s2.join(timeout=2)
    print("  wall time: %.2fs" % (time.monotonic() - t0))
    for i in sorted(results):
        print("   worker %d -> %s" % (i, results[i]))
    print("  sync pool: max_size=%d  spawned=%d  peak_in_use=%d  in_use=%d  idle=%d"
          % (sync_pool.max_size, sync_pool.spawned, sync_pool.peak_in_use,
             sync_pool.in_use, sync_pool.idle))
    print("  PIDs observed while running     :", sorted(sampled2, key=int) or "(none)")
    print("  max simultaneous PIDs observed  :", len(sampled2))
    print("  PIDs alive (pooled, by design)  :", stockfish_pids() or "(none)")

    banner("4) EXPLICIT SHUTDOWN")
    print("  engine PIDs still tracked by this process BEFORE close():",
          outstanding_engine_pids() or "(none)")
    asyncio.run(ev.close())
    sync_pool.close_all()
    evmod._sync_pool = saved
    time.sleep(0.8)
    tracked = outstanding_engine_pids()
    alive = {int(p) for p in stockfish_pids()}
    print("  engine PIDs still tracked by this process AFTER  close():",
          tracked or "(none)")
    print("  of those, still present in `ps` (=> ORPHANS)          :",
          [p for p in tracked if p in alive] or "(none)")
    print("  full host stockfish inventory now                    :", sorted(alive))
    print("  BASELINE PIDs still running                          :",
          [p for p in base if int(p) in alive] or "(none)")
    orphans = [p for p in tracked if p in alive]
    print("  ORPHAN PROCESSES (attributable to this run)          :", orphans or "(none)")
    return 0 if not orphans else 1


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        import traceback
        traceback.print_exc()
        rc = 2
    print()
    print("EXIT", rc)
    sys.exit(rc)
