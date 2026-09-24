# BUILDER-05 — `/api/repertoire/explore` and `/api/repertoire/tree` drive ONE unsynchronised Stockfish from concurrent threadpool threads; the UCI protocol desynchronises permanently
- **Severity:** high
- **Type:** race
- **File:** backend/repertoire.py
- **Line(s):** 80-88 (`_get_engine`) and 329-342 (the depth-10 eval loop in `get_candidate_moves_2000`)
- **Component:** Repertoire Builder — 2000+ explorer candidate table
- **Repro script:** `audit/scripts/atk1_b05_engine_race.py`
- **Detected by:** ATTACKER-1 (BUILDER)

## Expected

`GET /api/repertoire/explore` and `GET /api/repertoire/tree` are **sync** FastAPI endpoints, so
Starlette runs them in its anyio threadpool. They call `RepertoireManager._get_engine()`,
which hands out a single process-wide `chess.engine.SimpleEngine`. `SimpleEngine` is **not**
re-entrant for analysis: it owns one `asyncio` subprocess transport and one in-flight UCI
command, and its only lock is a *shutdown* lock
(`chess/engine.py`: `self._shutdown_lock = threading.Lock()`, used solely by `close()`).

The codebase already knows the right pattern — `backend/evaluator.py` was rewritten to add a
bounded, mutually-exclusive `SyncEnginePool` (`get_sync_engine_pool()`,
`ENGINE_OPTIONS = {"Threads": 2, "Hash": 64}` as "single source of truth … previously
duplicated verbatim in analyzer.py, repertoire.py and evaluator.py"). The repertoire explorer
path was never migrated to it, so it must serialise its own engine use (or use the pool) and
must health-check / recycle an engine that has desynchronised.

## Actual

Part A of the repro is deterministic: it wraps the real engine in a spy that records how many
threads are inside `analyse()` at the same time, and issues two simultaneous
`GET /api/repertoire/explore` requests:

```
shared engine object id: 140529175208560
--- PART A: is analyse() entered concurrently by two HTTP requests?
   status codes: {0: 200, 1: 200}
   analyse() calls: 16
   MAX CONCURRENT analyse() CALLS: 2
   >>> SimpleEngine is not re-entrant for analysis: RACE CONFIRMED
```

Part B races the real engine and shows the consequence — the subprocess protocol desynchronises:

```
Exception in callback Protocol.pipe_data_received()
  File ".../chess/engine.py", line 968, in pipe_data_received
    self._line_received(line)
  File ".../chess/engine.py", line 1740, in line_received
    self._readyok()
  File ".../chess/engine.py", line 1753, in _readyok
    self.result.set_result(self.analysis)
asyncio.exceptions.InvalidStateError: invalid state
```

(`InvalidStateError` = a `bestmove`/`info` line from an interleaved search was delivered to a
command future that had already been resolved.) An earlier run of the same probe left the
engine permanently unusable:

```
engine still alive after the race? True
ping after race -> EXC AssertionError CommandState.NEW
```

`ping()` raising `AssertionError: CommandState.NEW` means the engine object is wedged: its
command state machine no longer matches what the subprocess is saying. Nothing recovers it,
because `_get_engine()` only re-creates the engine when `self._engine is None`:

```python
# backend/repertoire.py:80-88
def _get_engine(self):
    if self._engine is None and os.path.exists(STOCKFISH_PATH):
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
            self._engine.configure({"Threads": 2, "Hash": 64})
        except Exception as e:
            print(f"Error starting engine in RepertoireManager: {e}")
        return self._engine
    return self._engine
```

After a desync every later `/explore` and `/tree` call silently returns
`"eval": null` for every candidate (the `except Exception: cand["eval"] = None` at line 341
swallows `EngineError`/`AssertionError`), and the 2000+ candidate table shows `-` in the eval
column until the app is restarted.

## Root cause

`RepertoireManager` holds a lazily-created, process-wide, unsynchronised `SimpleEngine` and
drives it from a blocking call (`eng.analyse(...)`) that runs on whatever anyio worker thread
happens to be serving the request. `chess.engine.SimpleEngine.analyse()` has no internal
mutual exclusion, so two threads interleave `ucinewgame` / `position` / `go` / `stop` /
`bestmove` on the same pipe. The per-request `try/except` turns the resulting protocol errors
into missing evals instead of invalidating the engine, and `_get_engine()` performs no
liveness check on an already-created engine (contrast `evaluator._sync_engine_healthy()`).


## Cross-reference / dedup note

`ABUSE-12` ("One shared `SimpleEngine` + no lock: concurrent `/api/repertoire/explore`
requests cancel each other's analyses") reports the **same root cause** from the resource /
cancellation angle. This report is kept because it establishes two things ABUSE-12 does not:
(a) a *deterministic* proof that two threadpool threads are inside `analyse()` at the same
time (`MAX CONCURRENT analyse() CALLS: 2`), and (b) that the damage is **permanent** — the
engine wedges (`ping() -> AssertionError: CommandState.NEW`) and `_get_engine()` never
health-checks, so the candidate table's eval column stays empty for the rest of the process
lifetime. A fixer for ABUSE-12 should treat (b) as in scope.

## Exploit / escalation

This is reached by ordinary UI use, not by an adversary:

1. **`refreshTree()` + `loadCandidateMoves()` overlap.** `saveMove()` awaits `refreshTree()`
   and then `loadCandidateMoves(...)`; both hit `get_candidate_moves_2000`, and `updatePosition()`
   fires an explore request on every step navigation. Two uncached positions in flight is normal.
2. **Two browser tabs / the desktop app plus a browser** both open on the Builder.
3. **Any two uncached positions**, because `explorer_cache` only short-circuits positions that
   were fetched before; a first visit to each new position pays the full race window.
4. **Amplifier — a wedged engine is permanent.** Because `_get_engine()` never health-checks,
   one unlucky interleaving degrades the eval column of the candidate table for the whole
   process lifetime; there is no self-heal and no user-visible error.
5. **Amplifier — leaked subprocess.** If `popen_uci` succeeds but `configure` raises, the
   exception is swallowed *after* `self._engine` has been assigned, leaving an unconfigured
   process pinned for the lifetime of the app.
