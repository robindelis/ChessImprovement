"""FIXER-K-01 -- ABUSE-06 cross-check for `backend/repertoire.py` (FIXER-A's file).

`RepertoireManager._get_engine()` (repertoire.py:80-88) is the *same* uncached,
unsynchronised `chess.engine.SimpleEngine` that ATK-5/ATK-1 found on the
pre-fix `GameAnalyzer`.  It is a singleton, so:

  * two threads can both observe `self._engine is None` and BOTH spawn a
    process (the second overwrites the first, orphaning it), and
  * two threads can then drive the SAME `SimpleEngine` object concurrently, which
    interleaves their UCI command streams and wedges the engine permanently
    (ATK-1).

This script only *measures*; it edits nothing.  It runs under a hard wall-clock
timeout from the caller because a wedge is permanent by design: the point is to
show it happens, and the subprocess boundary is what makes that safe to
demonstrate.

    timeout 60 /home/robin/miniconda3/bin/python3 audit/scripts/FIXER-K-01_repertoire_engine_probe.py
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
from backend.repertoire import RepertoireManager  # noqa: E402

THREADS = 4
WALL_CLOCK_CAP = 25.0

_stop = threading.Event()
_samples = []


def ps_children():
    out = subprocess.run(["ps", "-eo", "pid,ppid,args"], capture_output=True, text=True).stdout
    mine = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3 or "stockfish" not in parts[2]:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        if ppid == os.getpid():
            mine.append(pid)
    return sorted(mine)


def sampler():
    while not _stop.is_set():
        _samples.append(len(ps_children()))
        time.sleep(0.05)


def worker(index):
    rm = RepertoireManager.get_instance()
    engine = rm._get_engine()
    if engine is None:
        return "no engine"
    board = chess.Board()
    info = engine.analyse(board, chess.engine.Limit(depth=6))
    return int(info["score"].pov(chess.WHITE).score(mate_score=10000))


def main():
    # ---- phase B first: pre-create the singleton engine, then drive it from
    # several threads at once (the ATK-1 shape).  A `SimpleEngine` is not
    # thread-safe, so concurrent `analyse()` calls interleave their UCI command
    # streams.  This is the wedge, and it is permanent for that engine.
    rm = RepertoireManager.get_instance()
    first = rm._get_engine()
    print("phase B: singleton engine pre-created ->", "ok" if first is not None else "NONE")

    _stop.clear()
    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()

    results, errors = [], []

    def guarded(index):
        try:
            results.append(worker(index))
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=guarded, args=(i,), daemon=True) for i in range(THREADS)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(WALL_CLOCK_CAP)
    elapsed = time.time() - t0
    _stop.set()
    sampler_thread.join(2)

    stuck = [t for t in threads if t.is_alive()]
    peak = max(_samples, default=0)
    print("PHASE B (shared engine, concurrent analyse) THREADS=%d wall=%.2fs completed=%d "
          "still_running=%d peak_my_children=%d" % (THREADS, elapsed, len(results), len(stuck), peak))
    print("results=%s" % (results,))
    print("errors=%s" % (errors or "none"))
    if stuck:
        print("VERDICT B  WEDGED: %d thread(s) never returned from analyse() after %.0fs"
              % (len(stuck), WALL_CLOCK_CAP))
    # Phase B2: is the shared engine still usable single-threaded afterwards?
    # Run it in a daemon thread with its own cap: the expected outcome of the
    # concurrent abuse IS a permanent wedge, and a wedged `quit()` hangs too, so
    # the script must be able to finish and print.
    reuse = {}

    def reuse_once():
        try:
            info = first.analyse(chess.Board(), chess.engine.Limit(depth=6))
            reuse["ok"] = info["score"].pov(chess.WHITE).score(mate_score=10000)
        except Exception as exc:
            reuse["error"] = repr(exc)

    reuse_thread = threading.Thread(target=reuse_once, daemon=True)
    reuse_thread.start()
    reuse_thread.join(10)
    poisoned = reuse_thread.is_alive()
    if poisoned:
        print("PHASE B2 (reuse the shared engine single-threaded): WEDGED -- no answer in 10s")
    elif "ok" in reuse:
        print("PHASE B2 (reuse the shared engine single-threaded): OK, score=%s" % reuse["ok"])
    else:
        print("PHASE B2 (reuse the shared engine single-threaded): BROKEN -> %s" % reuse.get("error"))
        poisoned = True
    RepertoireManager._instance = None

    # ---- phase A: 4 concurrent cold callers -> how many processes does a
    # singleton spawn?
    _stop.clear()
    _samples.clear()
    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()
    results, errors = [], []

    threads = [threading.Thread(target=guarded, args=(i,), daemon=True) for i in range(THREADS)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(WALL_CLOCK_CAP)
    elapsed_a = time.time() - t0
    _stop.set()
    sampler_thread.join(2)
    peak_a = max(_samples, default=0)
    print("PHASE A (cold concurrent checkout) THREADS=%d wall=%.2fs completed=%d "
          "still_running=%d peak_my_children=%d errors=%s"
          % (THREADS, elapsed_a, len(results), len([t for t in threads if t.is_alive()]),
             peak_a, errors or "none"))

    # `close()` only quits the engine the singleton happens to be holding, so any
    # process lost to the cold-checkout race stays behind.  On a wedged engine
    # `quit()` hangs, so it is capped in its own thread.
    rm = RepertoireManager.get_instance()
    leaked = ps_children()
    print("engine children BEFORE close(): %s" % (leaked or "none"))
    closer = threading.Thread(target=rm.close, daemon=True)
    closer.start()
    closer.join(8)
    print("close() returned: %s" % (not closer.is_alive()))
    time.sleep(0.3)
    after = ps_children()
    print("engine children AFTER  close(): %s  <= orphans the race created"
          % (after or "none"))
    print("VERDICT A  spawn bound for a singleton (expect <=1): peak=%d  ->  %s"
          % (peak_a, "OK" if peak_a <= 1 else "OVERSPAWN / UNSYNCHRONISED"))
    from backend.evaluator import kill_stray_child_engines
    kill_stray_child_engines()
    time.sleep(0.5)
    print("after kill_stray_child_engines(): %s  <= only FIXER-C's exit net cleans these up"
          % (ps_children() or "none"))
    ok = peak_a <= 1 and not stuck and not poisoned
    print("OVERALL:", "PASS (repertoire.py engine is bounded)" if ok
          else "FAIL (repertoire.py still spawns per caller and/or wedges)")
    sys.stdout.flush()
    # A wedged engine can hang interpreter shutdown; the verdict is printed.
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
