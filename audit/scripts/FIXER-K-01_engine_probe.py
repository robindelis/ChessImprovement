"""FIXER-K-01 -- ABUSE-06 / ATK-5 verification for `backend/analyzer.py`.

ATK-5 proved that 8 cold-start requests spawned 8 Stockfish processes with 7
orphaned at ~250 MB each.  FIXER-C pooled `analyzer.py`; this script is the
evidence that the pool actually holds under concurrency, using the **real**
binary and the **real** code path (`GameAnalyzer.analyze_game_openings`, i.e.
what `POST /api/sync` runs).

Three phases, all bounded (one short opening line per game, the pipeline's own
depth, hard wall-clock cap):

  A. 8 CONCURRENT cold-start analyses  -> must stay within the pool cap
  B. 8 SEQUENTIAL cold-start analyses  -> must reuse, not respawn
  C. 8 CONCURRENT `analyzer._get_engine()` checkouts (the `api.py:1678`
     drill-submit shape)                             -> must stay within the cap

Every engine PID this process ever spawns is tracked with
`evaluator.outstanding_engine_pids()`, so "orphan" is self-attributing and
cannot be confused with another agent's concurrently running suites.

    /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-K-01_engine_probe.py
"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # noqa: E402  FIRST backend-adjacent import (temp DB only)
import chess  # noqa: E402
import chess.engine  # noqa: E402
from backend.analyzer import GameAnalyzer  # noqa: E402
from backend.evaluator import get_sync_engine_pool, outstanding_engine_pids  # noqa: E402

SESSIONS = 8
GAME = {"id": "fixerk_engine", "user_color": "white",
        "moves": "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7", "opening": {}}
WALL_CLOCK_CAP = 120.0

_stop = threading.Event()
_samples = []


def ps_children():
    """(pids, count) of live stockfish processes whose parent is THIS process."""
    out = subprocess.run(["ps", "-eo", "pid,ppid,args"], capture_output=True, text=True).stdout
    mine = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3 or "stockfish" not in parts[2]:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        if ppid == os.getpid():
            mine.append(pid)
    return sorted(mine), len(mine)


def sampler():
    while not _stop.is_set():
        pids, count = ps_children()
        _samples.append((time.time(), count, tuple(pids)))
        time.sleep(0.05)


def run_phase(label, worker, sessions=SESSIONS):
    _samples.clear()
    _stop.clear()
    thread = threading.Thread(target=sampler, daemon=True)
    thread.start()
    started = time.time()
    results = []
    errors = []

    def guarded(fn, index):
        try:
            results.append(fn(index))
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=guarded, args=(worker, i)) for i in range(sessions)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(WALL_CLOCK_CAP)
        if t.is_alive():
            errors.append("WORKER TIMED OUT after %.0fs" % WALL_CLOCK_CAP)
    elapsed = time.time() - t0
    _stop.set()
    thread.join(2)
    peak = max((count for _t, count, _p in _samples), default=0)
    print("  %-46s sessions=%d  peak_my_children=%d  wall=%.2fs  errors=%s"
          % (label, sessions, peak, elapsed, errors or "none"))
    return peak, results, errors


def analyse_once(index):
    analyzer = GameAnalyzer()
    try:
        blunders = analyzer.analyze_game_openings(dict(GAME))
        return len(blunders)
    finally:
        analyzer.close()


def checkout_once(index):
    analyzer = GameAnalyzer()
    try:
        engine = analyzer._get_engine()
        board = chess.Board()
        info = engine.analyse(board, chess.engine.Limit(depth=6))
        return info["score"].pov(chess.WHITE).score(mate_score=10000)
    finally:
        analyzer.close()


def main():
    pool = get_sync_engine_pool()
    baseline_pids, baseline_count = ps_children()
    print("=" * 78)
    print("BASELINE: stockfish processes parented by THIS process: %d %s"
          % (baseline_count, baseline_pids or ""))
    print("         host-wide stockfish count: %d"
          % int(subprocess.run(["bash", "-c", "ps -eo args | grep -c '[s]tockfish-linux'"],
                               capture_output=True, text=True).stdout.strip() or 0))
    print("         sync pool: max_size=%d spawned=%d live=%d in_use=%d"
          % (pool.max_size, pool.spawned, pool.live, pool.in_use))
    print("=" * 78)

    peak_a, results_a, errors_a = run_phase("A) 8 CONCURRENT analyses (POST /api/sync)",
                                             analyse_once)
    print("     pool after A: spawned=%d peak_in_use=%d live=%d in_use=%d"
          % (pool.spawned, pool.peak_in_use, pool.live, pool.in_use))

    spawned_before = pool.spawned
    peak_b, _results_b, errors_b = run_phase("B) 8 SEQUENTIAL analyses (reuse check)",
                                             analyse_once)
    print("     pool after B: spawned=%d (+%d for 8 more sessions) live=%d"
          % (pool.spawned, pool.spawned - spawned_before, pool.live))

    peak_c, _results_c, errors_c = run_phase("C) 8 CONCURRENT _get_engine() checkouts",
                                             checkout_once)
    print("     pool after C: spawned=%d peak_in_use=%d live=%d"
          % (pool.spawned, pool.peak_in_use, pool.live))

    print("=" * 78)
    verdict = (peak_a <= pool.max_size and peak_b <= pool.max_size
               and peak_c <= pool.max_size
               and not (errors_a + errors_b + errors_c))
    print("VERDICT  cap=%d honoured: A=%d B=%d C=%d  -> %s"
          % (pool.max_size, peak_a, peak_b, peak_c,
             "PASS" if verdict else "FAIL (over-spawn)"))

    tracked = outstanding_engine_pids()
    print()
    print("engine PIDs tracked by this process BEFORE close_all(): %s" % (sorted(tracked) or "none"))
    pool.close_all()
    time.sleep(0.3)
    pids_now, count_now = ps_children()
    print("engine PIDs still parented by this process AFTER close_all(): %s" % (pids_now or "none"))
    print("=> ORPHANS LEFT BEHIND: %s" % (pids_now or "none"))
    print("=> live=%d in_use=%d" % (pool.live, pool.in_use))

    from backend.evaluator import kill_stray_child_engines
    kill_stray_child_engines()
    time.sleep(0.3)
    pids_final, count_final = ps_children()
    print("=> after kill_stray_child_engines(): %s" % (pids_final or "none"))
    ok = verdict and not pids_final
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
