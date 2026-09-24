# ABUSE-06 — `RepertoireManager.get_instance()` is an unsynchronised check-then-set: 8 concurrent cold-start requests spawn 8 Stockfish processes (7 orphaned forever)
- **Severity:** high
- **Type:** security (resource exhaustion) / race
- **File:** `backend/repertoire.py`
- **Line(s):** 74-78 (`get_instance`), 80-89 (`_get_engine`)
- **Component:** engine lifecycle
- **Repro script:** `audit/scripts/atk5_engine_repro.py` (section B)
- **Detected by:** ATK-5

## Expected
A process-wide singleton holding a heavyweight child process must be created under
a lock (double-checked locking) so that N concurrent first-requests yield exactly
one instance and exactly one `stockfish` process. `ContentScout.get_instance()`
and `AsyncEngineEvaluator.get_instance()` have the same shape.

## Actual
`backend/repertoire.py:74-89`:

```python
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _get_engine(self):
        if self._engine is None and os.path.exists(STOCKFISH_PATH):
            try:
                self._engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
                self._engine.configure({"Threads": 2, "Hash": 64})
```

There is no lock on either check-then-set. `RepertoireManager.get_instance()` is
called from 23 sites in `backend/`, most of them **sync** routes, which FastAPI
runs in the `anyio` threadpool — i.e. genuinely parallel OS threads.

### Observed
`atk5_engine_repro.py` resets `RepertoireManager._instance = None` and then fires
8 threads through `get_instance()._get_engine()`:

```
B) cold-start race in _get_engine() (no lock) -> two stockfish procs
  stockfish processes before: 7  after 8 cold-start threads: 15
  distinct engine pids handed out: [483600, 483601, 483603, 483605, 483608, 483610, 483613, 483614]
  RepertoireManager._instance engine pid: 483614
  >>> 8 engine(s) spawned for one singleton; 8 stockfish procs alive (orphaned ones are never quit()).
```

8 distinct PIDs for 1 logical singleton. `RepertoireManager.close()` is only ever
called on the *winning* instance (and in practice never, since the app relies on
process exit), so the other 7 children — each configured `Hash: 64, Threads: 2`,
observed at **237–259 MB RSS each** — run for the remaining lifetime of the
server.

## Root cause
Unsynchronised lazy singleton initialisation. `cls._instance = cls()` is not
atomic with the `is None` test, and `self._engine = …` is not atomic with
`if self._engine is None`. Nothing ever reaps the losing instances: python-chess's
`SimpleEngine` keeps a background `threading.Thread` alive, so the losing objects
are never garbage-collected and their `stockfish` children are never `quit()`.

## Exploit / escalation
1. **Amplification DoS from a web page.** Because CORS reflects any origin and no
   route is authenticated (ABUSE-03), a hostile page can
   `fetch('http://127.0.0.1:38281/api/repertoire/explore?fen=…', {cache:'no-store'})`
   N times in parallel. If it lands while `_instance` is still `None` — i.e. right
   after the server starts, or right after any code path sets `_instance = None`
   (`tests/test_setup.py:69` does exactly that) — each racing request that wins the
   `is None` test spawns a fresh 250 MB engine. A few hundred parallel requests
   is several gigabytes of RSS, and the engines are never reaped.
2. **Adjacent — same pattern in the other singletons.** `ContentScout.get_instance()`
   (`backend/scout.py:89-92`) and `AsyncEngineEvaluator.get_instance()`
   (`backend/evaluator.py:40-44`) are identical check-then-set sequences; the
   evaluator is only reached from the async event loop today (single-threaded), but
   `ContentScout` is used from 6 sync routes.
3. **Adjacent — `_explorer_count_memo` / `_user_stats_cache`** are plain dicts
   mutated from the threadpool with no lock (ABUSE-13 covers the engine; these are
   the same class of hazard for the memo caches).

**Required fix:** guard both initialisations with a `threading.Lock`
(double-checked), and register an `atexit`/lifespan shutdown that iterates over
*all* instances ever created (or, better, key the engine by a process pool with a
hard cap) so no child can outlive its request.
