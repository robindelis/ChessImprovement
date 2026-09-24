"""BUILDER-05 (deterministic) probe: the process-wide blocking Stockfish engine is
entered concurrently by /api/repertoire/explore requests -- SimpleEngine.analyse()
has no mutual exclusion, so UCI commands from different threadpool threads
interleave on one subprocess and desynchronise it permanently.

`/api/repertoire/explore` and `/api/repertoire/tree` are sync FastAPI endpoints
(Starlette anyio threadpool).  Both reach
`RepertoireManager.get_candidate_moves_2000()` -> `self._get_engine()`, which
returns the SINGLE process-wide `chess.engine.SimpleEngine`.  Nothing serialises
it, and `_get_engine()` never health-checks an existing engine.

Part A (deterministic): instrument the engine and prove >1 thread is inside
         `analyse()` at the same time.
Part B (real engine, best-effort): race two real requests and report whether the
         UCI protocol breaks.
"""
import os
import sys
import time
import threading
import traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atk1_fixture import *  # noqa

import backend.repertoire as R

FENS = [
    chess.STARTING_FEN,
    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1",
    "rnbqkbnr/pppppppp/8/8/2P5/8/PP1PPPPP/RNBQKBNR b KQkq - 0 1",
]
ALIGN = float(os.environ.get("ATK1_ALIGN", "0.35"))
N_CANDS = 8


def fake_get(url, params=None, headers=None, timeout=None, **kw):
    import chess as _c
    b = _c.Board(params.get("fen", ""))
    sans = [b.san(m) for m in list(b.legal_moves)[:N_CANDS]]

    class Resp:
        status_code = 200

        def json(self):
            return {"white": 100, "draws": 50, "black": 100,
                    "moves": [{"uci": b.parse_san(s).uci(), "san": s, "white": 30,
                               "draws": 10, "black": 20, "averageRating": 2300} for s in sans]}
    time.sleep(ALIGN)          # line both threads up on the engine call
    return Resp()


R.requests = type("m", (), {"get": staticmethod(fake_get)})
wipe()
rm = R.RepertoireManager.get_instance()
real_engine = rm._get_engine()
print("shared engine object id:", id(real_engine))


class SpyEngine:
    """Wraps the real engine, records how many threads are inside analyse()."""
    def __init__(self, inner):
        self._inner = inner
        self._lk = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.entries = 0

    def analyse(self, *a, **k):
        with self._lk:
            self.active += 1
            self.entries += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)   # widen the window a little
            return self._inner.analyse(*a, **k)
        finally:
            with self._lk:
                self.active -= 1

    def __getattr__(self, n):
        return getattr(self._inner, n)


rm._engine = SpyEngine(real_engine)

print("\n--- PART A: is analyse() entered concurrently by two HTTP requests?")
out = {}
barrier = threading.Barrier(2)


def worker(i):
    barrier.wait()
    r = CLIENT.get("/api/repertoire/explore", params={"fen": FENS[i]})
    out[i] = r.status_code


ts = [threading.Thread(target=worker, args=(0,)), threading.Thread(target=worker, args=(1,))]
for t in ts:
    t.start()
for t in ts:
    t.join(60)
print("   status codes:", out)
print("   analyse() calls:", rm._engine.entries)
print("   MAX CONCURRENT analyse() CALLS:", rm._engine.max_active)
print("   >>> SimpleEngine is not re-entrant for analysis:",
      "RACE CONFIRMED" if rm._engine.max_active > 1 else "no overlap this run")

print("\n--- PART B: same race against the REAL engine, repeated until it breaks")
rm._engine = real_engine
broke = False
for rnd in range(4):
    a, b = FENS[rnd % len(FENS)], FENS[(rnd + 1) % len(FENS)]
    for f in (a, b):
        raw("DELETE FROM explorer_cache WHERE fen = ?", (normalize_fen(f),))
    res = {}
    bar = threading.Barrier(2)

    def w(name, fen):
        bar.wait()
        try:
            res[name] = CLIENT.get("/api/repertoire/explore", params={"fen": fen}).status_code
        except Exception as e:
            res[name] = f"EXC {type(e).__name__}"
    ths = [threading.Thread(target=w, args=("A", a)), threading.Thread(target=w, args=("B", b))]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join(45)
    el = time.time() - t0
    try:
        real_engine.ping()
        health = "OK"
    except Exception as e:
        health = f"BROKEN -> {type(e).__name__}: {e}"
        broke = True
    print(f"   round {rnd}: {el:5.1f}s  A={res.get('A')} B={res.get('B')}  engine health: {health}")
    if broke:
        break

if broke:
    print("   >>> UCI protocol permanently desynchronised; _get_engine() will never")
    print("       re-create it, so EVERY later /explore and /tree call is affected.")
    try:
        r = CLIENT.get("/api/repertoire/explore", params={"fen": FENS[0]})
        print("   follow-on ordinary request ->", r.status_code, r.text[:80])
    except Exception as e:
        print("   follow-on ordinary request -> EXC", type(e).__name__, e)


# The probe deliberately leaves the racing SimpleEngine un-quit (that IS the bug),
# which makes interpreter shutdown wait on the subprocess pipe.  Hard-exit so the
# script stays reliably re-runnable.
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)
