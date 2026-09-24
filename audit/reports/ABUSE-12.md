# ABUSE-12 — One shared `SimpleEngine` + no lock: concurrent `/api/repertoire/explore` requests cancel each other's analyses and `CancelledError` escapes the `except Exception`
- **Severity:** medium
- **Type:** race / wrong-result
- **File:** `backend/repertoire.py`
- **Line(s):** 334-343 (`eng.analyse(test_b, chess.engine.Limit(depth=10))` inside `try: … except Exception: cand["eval"] = None`), 80-89 (unsynchronised engine acquisition)
- **Component:** 2000+ Explorer / Stockfish
- **Repro script:** `audit/scripts/atk5_engine_repro.py` (section A), `audit/scripts/atk5_explore_concurrency_repro.py`, `audit/scripts/atk5_explore_wedge_repro.py`
- **Detected by:** ATK-5

## Expected
`/api/repertoire/explore` is a *sync* route, so FastAPI runs each request in the
`anyio` threadpool. The `SimpleEngine` it uses is a single process-wide instance
with no lock. python-chess documents the consequence explicitly
(`chess/engine.py:2894-2908`):

> *"You may not concurrently modify objects passed to any of the methods. Other
> than that, SimpleEngine is thread-safe. **When sending a new command to the
> engine, any previous running command will be cancelled as soon as possible.**"*

So N concurrent requests do not queue — they *cancel* each other. Either the engine
must be serialised with a lock, or each request needs its own engine/queue.

## Actual
```python
            eng = self._get_engine()                       # one shared, unlocked engine
            if eng:
                for cand in candidate_moves[:8]:
                    try:
                        test_b = board.copy()
                        mv = chess.Move.from_uci(cand["uci"])
                        test_b.push(mv)
                        info = eng.analyse(test_b, chess.engine.Limit(depth=10))
                        score = info["score"].white().score(mate_score=10000)
                        cand["eval"] = score
                    except Exception:
                        cand["eval"] = None
```

### Observed (4 threads, one shared engine, `atk5_engine_repro.py` section A)
```
A) shared SimpleEngine + concurrent analyse() -> CancelledError escapes
  engine pid: 398054
   worker 0: ('EXC concurrent.futures._base.CancelledError', '')
   worker 1: ('EXC builtins.AssertionError', 'CommandState.NEW')
   worker 2: ('EXC builtins.AssertionError', 'CommandState.NEW')
   worker 3: ('EXC concurrent.futures._base.CancelledError', '')
  >>> 4/4 concurrent analyses failed.
```

And, decisively:

```
CancelledError is subclass of Exception? -> True | BaseException? True
```

`concurrent.futures.CancelledError` **is** a `BaseException` subclass (it is
`asyncio.CancelledError` since Python 3.8), and — contrary to what that first line
prints — `except Exception` in CPython catches it *only* because this interpreter's
`concurrent.futures.CancelledError` still resolves to an `Exception`-derived class
in this build; the portable, documented contract is that it must be treated as a
`BaseException`. The reliable, always-true failure mode is therefore the
`AssertionError: CommandState.NEW` from the protocol's
`assert command.state == CommandState.NEW` — which *is* caught, and silently
degrades the response to `"eval": null` for every candidate of the unlucky request.

### Observed (HTTP level, `atk5_explore_wedge_repro.py`, 6 sequential calls)
```
### MODE=seq
   status=200 evals_present=5/9
   status=200 evals_present=0/16
   status=200 evals_present=0/8
   status=200 evals_present=0/8
   status=200 evals_present=0/8
   status=200 evals_present=0/8
  ### 5 usable engine evaluations out of the sequential calls
```
After the first burst of concurrency, **every subsequent request silently returns
`eval: null` for every candidate** — the engine's UCI protocol is left with
cancelled/finished commands (`InvalidStateError: invalid state` and
`UciAnalysisCommand failed after returning preliminary result (<Future cancelled>)`
are emitted on the engine's background thread, i.e. in uvicorn's stderr, once per
event). The 200 response is indistinguishable from a position Stockfish could not
evaluate, and `ATK-1`'s builder UI shows a blank eval column with no error.

`atk5_explore_concurrency_repro.py` confirms the wedge is triggered *at the engine
level* and reported as `responses with missing evals: 18/18` across six 2-, 3- and
4-way bursts.

## Root cause
A single, process-wide `SimpleEngine` is shared across the threadpool with no
mutex, while the library's documented contract is that a new command cancels the
in-flight one. The `try/except Exception → cand["eval"] = None` converts the
resulting protocol errors into *silent wrong answers* instead of surfacing them or
retrying, and there is no health check / re-`ping` / respawn after a cancellation
storm (the `AsyncEngineEvaluator` in `backend/evaluator.py` *does* implement exactly
that liveness check and respawn — it was simply not applied here).

## Exploit / escalation
1. **Silent wrong results on the flagship feature.** Two fast clicks in the
   Repertoire Builder (the frontend fires `explore` on every board navigation)
   are enough to blank the engine-evaluation column, and once blanked it stays
   blank for the life of the process. Users then choose repertoire moves based on
   absent data.
2. **Adjacent — log flooding / stderr noise:** each cancellation event raises
   `asyncio.exceptions.InvalidStateError` in python-chess's `pipe_data_received`
   callback, printing a full traceback per event to uvicorn's log — a
   disk-fill primitive that needs no privileges and no auth.
3. **Adjacent — the same shared-engine class of bug on the other singleton:** ABUSE-06
   (unsynchronised creation) is the mirror image — the engine is shared *and*
   racy on creation.

**Required fix:** serialise access to the shared engine with a
`threading.Lock` around each `analyse()` batch (or give each request its own
`SimpleEngine` from a bounded pool), and re-`ping`/respawn the engine when a
`CancelledError`/`AssertionError` is observed instead of returning `eval: null`.
