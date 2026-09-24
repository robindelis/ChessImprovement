# ABUSE-22 — `SYNC_POOL_WAIT_TIMEOUT` is 300 s: an engine-using request blocks for minutes before it can be told the engine is busy, which is the exact behaviour the 503 mapping was written to prevent
- **Severity:** medium
- **Type:** security (resource exhaustion) / ui-regression
- **File:** `backend/evaluator.py`
- **Line(s):** 126 (`SYNC_POOL_WAIT_TIMEOUT = _env_float("CHESS_ENGINE_WAIT_TIMEOUT", 300.0)`), 738-742 (`SyncEnginePool.acquire` — the blocking `_sem.acquire(timeout=wait)`), 126's counterpart 131 (`ASYNC_POOL_WAIT_TIMEOUT`, default 20 s); the consumer comment at `backend/api.py:1036`
- **Component:** engine pool (the ABUSE-06/FIXER-C fix)
- **Repro script:** `audit/scripts/atk5_re2_5c_pool_stall.py` (176 s queueing, 60 concurrent requests), `audit/scripts/atk5_re2_5b_pool_saturation.py` (the wait mechanism), `audit/scripts/atk5_re2_7_busy_wait.py`
- **Detected by:** ATK-5 (re-attack pass 2)

## Expected

FIXER-B added a retryable `503` + `Retry-After: 5` for `EngineBusy` at two call sites, and its comment states the
intent precisely (`api.py:1033-1036`):

> The shared Stockfish pool is saturated; `acquire()` raised after its bounded wait. This is a "try again later"
> condition, not a per-game failure: it must not be counted as a failed game … **and must not be waited out for
> 300 s.**

The `Retry-After: 5` tells the client to come back in five seconds. That instruction is only coherent if the
server actually gives up at about that scale. The shipped default does not: the sync pool waits **300 seconds**,
sixty times the advertised retry hint.

## Actual

### The number is unambiguous

```
[D] SOURCE FACTS
    evaluator.py:126  SYNC_POOL_WAIT_TIMEOUT = _env_float("CHESS_ENGINE_WAIT_TIMEOUT", 300.0)
    evaluator.py:131  ASYNC_POOL_WAIT_TIMEOUT = _env_float("CHESS_ENGINE_ASYNC_WAIT_TIMEOUT", 20.0)
```

`SyncEnginePool.acquire()` (`:738-742`) does `self._sem.acquire(timeout=wait)` — a genuine blocking wait on a
worker thread. The async pool's 20 s default shows the author knew a *short* bound was the right shape; the sync
pool, which serves the `def` routes, got 300 s.

### Measured: 176 s of queueing, and the control proves it is the wait

`atk5_re2_5c_pool_stall.py`, real uvicorn socket, own process, 60 concurrent `/api/repertoire/explore`, engine
replaced by a 2 s fake (no real Stockfish):

```
[A] flood 60 concurrent engine-using requests, pool wait_timeout = 300 s
    flood statuses           : ['200 OK']
    flood latency min/med/max: 0.16 / 0.31 / 176.12s

[B] CONTROL: same flood, CHESS_ENGINE_WAIT_TIMEOUT = 0.05 s
    flood statuses           : ['200 OK']
    worst cheap-route probe  : 0.72s on /api/dashboard
```

Identical load, identical fake analysis cost. The only difference is the wait budget, and the tail latency
collapses from **176 s** to under a second. So the queueing is the wait, not the work.

An earlier run at a 300 s cap with 44 requests reproduced the same shape (max 176.15 s), so this is stable and
not a one-off.

### A single unlucky request can eat the whole budget

`atk5_re2_5b_pool_saturation.py`, 10 concurrent `acquire()` calls against a cap-2 pool:

```
    outcomes               : {'acquired': 2, 'EngineBusy': 8}
    wait min/med/max       : 1.10 / 3.00 / 3.00s      (this probe used wait_timeout=3.0 s)
        EngineBusy: engine pool saturated (max_size=2, waited 3.0s)   x8
```

With the shipped 300 s default, those same 8 callers each block for 300 s before receiving the identical
`EngineBusy`. The two `GET /api/repertoire/explore` and `GET /api/repertoire/tree` routes that catch it and
degrade gracefully (`repertoire.py:487-493`) hide the wait from the response — the user sees a slow page, not an
error, so there is no signal to stop retrying.

## Root cause

Two defaults were chosen independently and disagree by 15x:

| pool | serves | default wait | env var |
| :--- | :--- | :--- | :--- |
| `AsyncEnginePool` | the SSE eval stream | **20 s** | `CHESS_ENGINE_ASYNC_WAIT_TIMEOUT` |
| `SyncEnginePool` | every `def` route that needs an engine | **300 s** | `CHESS_ENGINE_WAIT_TIMEOUT` |

`evaluator.py:126` was evidently carried over from the era when a sync caller waited on an unsynchronised
`SimpleEngine`; the pool rewrite bounded the wait but kept the old magnitude. The `503` mapping that was added
alongside inherits it, so a caller that is told "retry in 5 seconds" has in fact already spent 300 seconds
learning it should.

## Exploit / escalation

Reached by: any concurrency against an engine-using route. `GET /api/repertoire/explore` and
`GET /api/repertoire/tree` are the cheapest doors — both are unauthenticated GETs on the loopback interface, both
acquire the 2-slot sync pool, and both *degrade silently* rather than returning 503, so a caller has no way to
know it is being queued. Note the explorer cache means an attacker does not need fresh positions forever: the
ABUSE-19 finding shows the memo is keyed by raw FEN, so many distinct clock variants of the *same* position each
pay a full queueing wait.

Adjacent variants of the same root cause:

1. **A slow real analysis is the trigger, not just concurrency.** The pool cap is 2 and each analysis is a
   depth-10 search over up to 8 candidate moves (`repertoire.py:464-467`). A handful of simultaneous
   `/api/repertoire/explore` calls on *cold* positions is enough on a modest machine — no flood required.
2. **The 300 s wait outlives the client.** A browser tab, the Electron window, or a reverse proxy will have
   given up long before `acquire()` returns. The user sees a spinner and then an error with no useful message,
   because the request that would have told them "busy, retry" is still blocked.
3. **The two pools disagree, so a fix here is not optional-by-preference.** Whichever value is chosen, it should
   be chosen once and applied to both, or the SSE stream and the sync routes will keep failing differently under
   the same load.
4. **Env-var escape hatch exists** (`CHESS_ENGINE_WAIT_TIMEOUT`) — so this is a one-line deploy-level mitigation
   available to the operator today, which is worth stating in the report to whoever triages it.

## Suggested fix shape (for the `evaluator.py` owner)

* Drop the sync default to the same order as the async one. A few seconds is the right shape: long enough to ride
  out a transient burst, short enough that the `Retry-After: 5` the client is given is honest.
* Better: derive the response from the same number. If the wait is 5 s, say `Retry-After: 5`; today the two are
  unrelated constants 60x apart, which is why the mismatch survived review.
* Consider a fast-fail for the *degrading* routes specifically: `explorer`/`tree` do not need the engine, so they
  could take a much shorter wait (or none at all) and render the candidate table without evals — which is
  exactly what `repertoire.py:487-493` already knows how to do.
* Regression test: with the pool pre-saturated, assert that an engine-using request answers within a few
  seconds, not that it eventually answers.
