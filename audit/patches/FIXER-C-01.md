# FIXER-C-01 — `backend/evaluator.py` SSE contract, typed errors, bounded engine pool

**Owner:** FIXER-C · **Files touched:** `backend/evaluator.py` (owned),
`backend/analyzer.py` (engine-lifecycle only, unowned — see §3.0),
`tests/test_evaluator_stream.py` (new), `audit/scripts/FIXER-C-01_*.py` (new).
**Not touched:** `frontend/**`, `backend/api.py`, `backend/db.py`,
`backend/repertoire.py`, `tests/*` other than the new file.

**Findings fixed:** `CODE-02-23` (HIGH), `CODE-02-16` (MEDIUM), `CODE-02-09` (HIGH).

---

## 1. Root cause per finding

### CODE-02-23 — `score_cp` vs `eval` key drift froze the Scout eval bar

`backend/evaluator.py:181-189` (old) built every frame as a bare dict and `json.dumps`'d it:

```python
last_res = {"depth": depth, "score_cp": score_cp, "mate": mate,
            "best_move": ..., "best_san": ..., "final": (depth >= max_depth)}
yield f"data: {json.dumps(last_res)}\n\n"
```

The Scout consumer (`frontend/scout.js:1950`) reads `data.eval`, a key that
exists only in the *explorer* payloads. There was no schema, no discriminator,
no `response_model`, and the project is zero-build vanilla JS, so nothing could
catch the mismatch except a human. The two key conventions coexisted silently.

### CODE-02-16 — untyped `{"error": ...}` froze the bar mid-stream

Three failure exits (old `evaluator.py:106-113`, `208-216`) wrote
`data: {"error": "<raw str(e)>"}` with **no** `event:` name, no `type`, no
`final`. Both consumers gate on something the error frame does not carry
(`data.score_cp !== undefined` / `data.eval`), so the frame was dropped and the
bar froze with no visible failure. `str(e)` also shipped the absolute
Stockfish path to the browser, and nothing was logged server-side.

### CODE-02-09 — a new Stockfish process per request

`GameAnalyzer._get_engine()` (old `analyzer.py:22-29`) was an uncached
`SimpleEngine.popen_uci()` and `api.py:218` / `api.py:710` construct a
`GameAnalyzer` inline per request. No singleton, no semaphore, no reuse → N
concurrent requests = N processes × (2 threads + 64 MB hash). The async side
had one shared engine, but guarded it with an **unbounded** `asyncio.Lock`
(`async with self._lock:` wrapped the whole `async for`, i.e. the lock was held
across every `yield`), so one slow reader could hold the engine and the queue
forever.

---

## 2. The SSE payload contract now emitted (v1)

Route: `GET /api/repertoire/eval_stream?fen=…&depth=…` → `text/event-stream`.
Producer: `backend/evaluator.py` (`format_sse`, `build_eval_event`,
`build_error_event`). Self-description: `eval_stream_contract()`.

Every block is rendered as

```
data: {"v":1,"type":"eval",…}      <-- MUST stay the FIRST line
event: eval                        <-- event name, never first
```

> **`data:` is deliberately first.** `repertoire_builder.js:1774` does
> `block.trim().startsWith('data: ')`. An `event:` line placed first would make
> the Repertoire Builder drop every frame. Field order inside an SSE block is
> not significant per the spec, so `data:`-first is safe for conforming clients
> *and* keeps both hand-rolled parsers working. **Do not reorder.**

| field | type | meaning |
|---|---|---|
| `v` | int | contract version, `1`. Bump on any change. |
| `type` | str | **`"eval"`** (progress/terminal position) or **`"error"`** (terminal failure). The discriminating field. |
| `fen` | str | normalized FEN (4 tokens) the frame describes. |
| `depth` | int | search depth reached. Always an int: `0` on errors, `99` for an already-over position, ≤ requested depth otherwise. |
| `score_cp` | int\|null | centipawns, **White's point of view**, saturated to ±10000. Canonical key (read by `repertoire_builder.js`). |
| `eval` | int\|null | **Documented alias of `score_cp` — always equal.** Exists so `scout.js` (`data.eval`) and `repertoire_builder.js` (`data.score_cp`) read the same frame. |
| `mate` | int\|null | mate distance in **moves**, White POV, else `null`. |
| `is_mate` | bool | `mate is not null`. Redundant with `mate` so a consumer that cannot compare against `null` still gets a safe boolean. |
| `best_move` | str | best move, UCI, `""` if unknown. |
| `best_san` | str | best move, SAN, `""` if unknown. |
| `final` | bool | `true` on the last frame of a successful stream **and on every `error` frame**, so a client gating on `final` stops instead of hanging. |
| `cached` | bool | `true` when replayed from the server-side position cache. |
| `code` | str | *error frames only*: `invalid_fen` \| `engine_unavailable` \| `engine_busy` \| `engine_error`. |
| `error` | str | *error frames only*: **sanitized** message — single line, no `/`, no filesystem path, ≤200 chars. |

Event names: `eval` (progress), `eval_final` (`final:true`), `error`.

**Mate handling (end to end).** When the engine reports a mate, `mate` is set
(e.g. `3`), `is_mate` is `true`, and `score_cp`/`eval` carry a **saturated
stand-in** of `±10000`. Consumers must read `mate` for the distance; `score_cp`
is deliberately clamped by `_clamp_cp()` so a mate can never be rendered as a
huge centipawn value. `repertoire_builder.js:1707` and `scout.js:1969` both
check `mate !== null` **before** the centipawn branch, so they render `M3`
correctly.

Example frame (real engine, Italian Game, White +4.93):
```
data: {"v":1,"type":"eval","fen":"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKB1R w KQkq -","depth":6,"score_cp":493,"eval":493,"mate":null,"is_mate":false,"best_move":"e2e4","best_san":"e4","final":true,"cached":false}
event: eval_final
```

Example error frame:
```
data: {"v":1,"type":"error","fen":"rnbqkbnr/…","depth":0,"score_cp":null,"eval":null,"mate":null,"is_mate":false,"best_move":"","best_san":"","final":true,"cached":false,"code":"engine_unavailable","error":"Stockfish binary not found at <path>"}
event: error
```

**Frontend owners can rely on:** `data.eval === data.score_cp` always;
`data.mate !== null` ⇒ read the distance from `data.mate`;
`data.type === "error"` ⇒ the stream is over and the bar should freeze (and may
be labelled from `data.error`); no consumer needs `data.error` to be a raw
exception string.

---

## 3. Changes

### 3.0 `backend/analyzer.py` — engine lifecycle only (unowned file, minimal diff)

> **Scope statement:** `analyzer.py` was not owned by anyone. I edited it *only*
> for the engine-lifecycle fix of CODE-02-09, as permitted. No analysis logic
> was touched. Verified with `git diff --stat -- backend/analyzer.py`:
> `1 file changed, 22 insertions(+), 10 deletions(-)`.

`backend/analyzer.py:9,18-58` — verbatim diff:

```diff
@@ -6,6 +6,7 @@ import chess
 from backend.db import get_db, get_setting
+from backend.evaluator import get_sync_engine_pool
 from backend.openings import OpeningBook
 
 class GameAnalyzer:
+    """Opening-blunder analyzer.
+
+    Engine lifecycle note (audit CODE-02-09): this class no longer spawns its
+    own Stockfish process.  Engines come from the process-wide bounded pool in
+    ``backend.evaluator`` and are handed back to it by :meth:`close`, so N
+    concurrent requests can never produce N processes.  ``close()`` keeps its
+    original contract (releases the engine and makes the instance reusable).
+    """
+
     def __init__(self, stockfish_path: str = STOCKFISH_PATH):
         self.stockfish_path = stockfish_path
         self.opening_book = OpeningBook.get_instance()
         self._engine = None
+        self._pool = None
 
     def _get_engine(self):
         if self._engine is None:
-            if not os.path.exists(self.stockfish_path):
-                raise FileNotFoundError(f"Stockfish binary not found at {self.stockfish_path}")
-            self._engine = chess.engine.SimpleEngine.popen_uci(self.stockfish_path)
-            # Configure engine for fast, high-quality analysis
-            self._engine.configure({"Threads": 2, "Hash": 64})
+            pool = get_sync_engine_pool(self.stockfish_path)
+            self._pool = pool
+            # Raises FileNotFoundError (unchanged legacy contract) when the
+            # binary is missing, or EngineBusy when the pool stays saturated.
+            self._engine = pool.acquire()
         return self._engine
 
     def close(self):
-        if self._engine:
+        if self._engine is not None:
+            engine, pool = self._engine, self._pool
+            self._engine = None
+            self._pool = None
             try:
-                self._engine.quit()
+                if pool is not None:
+                    pool.release(engine)
+                else:
+                    engine.quit()
             except Exception:
                 pass
-            self._engine = None
```

`FileNotFoundError` is still raised for a missing binary with the identical
message, so `api.py`'s existing error handling is unaffected.

### 3.1 `backend/evaluator.py` — module docstring = the contract

`backend/evaluator.py:1-95` — the module docstring now *is* the payload
contract (field table, `data:`-first rule, mate rules, the cp→height formula),
so the next agent editing a field name has one place to look. Followed by
`ENGINE_OPTIONS` (`:103`, previously duplicated verbatim in three modules) and
the env-tunable caps:

```python
SYNC_POOL_MAX_SIZE      = _env_int("CHESS_ENGINE_MAX_PROCS", 2)          # :123
SYNC_POOL_WAIT_TIMEOUT  = _env_float("CHESS_ENGINE_WAIT_TIMEOUT", 300.0)
ASYNC_POOL_MAX_SIZE     = _env_int("CHESS_ENGINE_ASYNC_MAX", 1)          # :130
ASYNC_POOL_WAIT_TIMEOUT = _env_float("CHESS_ENGINE_ASYNC_WAIT_TIMEOUT", 20.0)
ENGINE_IDLE_SECONDS     = _env_float("CHESS_ENGINE_IDLE_SECONDS", 600.0)
MATE_SCORE_CP           = 10000
```

`ASYNC_POOL_MAX_SIZE` stays **1** so eval-bar latency is bit-for-bit what it was;
the cap is now explicit, testable and bounded.

### 3.2 CODE-02-23 — typed, aliased payload

`backend/evaluator.py:158-174` constants, `:177` `eval_stream_contract()`,
`:252-283` `build_eval_event()`, `:297-308` `build_error_event()`,
`:310-320` `format_sse()`, `:322-327` `_frame()`.

```python
def build_eval_event(depth, score_cp, mate=None, best_move="", best_san="",
                     final=False, cached=False, fen="") -> Dict[str, Any]:
    is_mate = mate is not None
    if score_cp is None and is_mate:
        score_cp = MATE_SCORE_CP if mate > 0 else -MATE_SCORE_CP
    cp = _clamp_cp(score_cp)                      # saturate to ±10000, int or None
    return {
        "v": SSE_CONTRACT_VERSION, "type": TYPE_EVAL, "fen": fen,
        "depth": int(depth),
        "score_cp": cp,
        "eval": cp,            # documented alias -- frontend/scout.js reads this key
        "mate": int(mate) if is_mate else None,
        "is_mate": is_mate,
        "best_move": best_move or "", "best_san": best_san or "",
        "final": bool(final), "cached": bool(cached),
    }

def format_sse(payload, event=None) -> str:
    """Render a payload as one SSE event block.

    ``data:`` is always first on purpose -- see the module docstring.
    """
    data = json.dumps(payload, separators=(",", ":"))
    lines = ["data: " + data]
    if event:
        lines.append("event: " + event)
    return "\n".join(lines) + "\n\n"
```

`EVAL_STREAM_FIELDS` / `ERROR_STREAM_FIELDS` tuples + `eval_stream_contract()`
give the schema a name so a drift can be asserted in a test
(`test_contract_self_description`) rather than discovered by a user.

### 3.3 CODE-02-16 — typed, terminal, sanitized errors

`backend/evaluator.py:285-295` `sanitize_error()` strips absolute paths
(`re.sub(r"(?:/[^\s'\"]+)+", "<path>", …)`), collapses whitespace and truncates
to 200 chars. `build_error_event()` (`:297`) forces `final=True` and adds
`code` + `error`. `stream_eval` (`:1038-1051`) now has three distinct
checkout-failure branches (`EngineBusy` → `engine_busy`, `FileNotFoundError` →
`engine_unavailable`, anything else → `engine_unavailable`) and `:1104-1108`
maps engine failures to `engine_error`; each one **yields one error frame and
returns**, which ends the `StreamingResponse` body so the client sees `done`.

Server-side `logger.warning` / `logger.error` / `logger.exception` calls were
added at every failure exit (nothing was logged before).

`stream_eval` also gained an explicit docstring of the three guarantees
(`:975-983`) so the contract is visible at the call site.

### 3.4 CODE-02-09 — bounded pools, reuse, guaranteed cleanup

`backend/evaluator.py:147-152` `EngineBusy` / `EngineUnavailable`;
`:333-346` `_process_alive()`; `:388-416` `_force_kill_process()`;
`:467-672` **`AsyncEnginePool`**; `:674-870` **`SyncEnginePool`**;
`:872-880` `get_sync_engine_pool()`.

`SyncEnginePool` (the CODE-02-09 core):

* `threading.BoundedSemaphore(max_size)` caps **simultaneous engine users**, so
  total live processes ≤ `max_size` (default 2) — a slot is held for the whole
  search, so one engine is never driven by two threads.
* `acquire()` (`:738`) has a **bounded** wait (`CHESS_ENGINE_WAIT_TIMEOUT`,
  default 300 s) and raises `EngineBusy` instead of queueing forever.
* An `OrderedDict` free list recycles healthy engines (liveness = transport
  `returncode is None` **and** a `ping()`), so sequential requests reuse one
  process; dead engines are replaced; engines idle > `CHESS_ENGINE_IDLE_SECONDS`
  are dropped so the pool never pins 64 MB forever.
* `release()` / `discard()` / `engine()` context manager (`discard` on error,
  `release` on success) make cleanup exception-proof; the semaphore is released
  in a `finally`.
* `close_all()` for shutdown.

`AsyncEnginePool` mirrors it for `chess.engine.UciProtocol`, and additionally:

* `release_slot()` (`:545`) is **fully synchronous** so the slot hand-back is
  safe from a `finally` during `GeneratorExit` — a cancelled or abandoned
  generator can never leak a slot.
* `stop_and_retire()` (`:566`) sends a best-effort `stop` on client disconnect
  and retires the engine so the next request never inherits a *searching* one.
* **Loop affinity** (`:600-616`): a `UciProtocol` is bound to the event loop
  that created it (its reader task and queues live there). An idle engine whose
  loop is gone is never reused — it is terminated instead. Without this,
  `asyncio.run()`-per-call callers (scripts, tests) orphaned a process.

`stream_eval` was restructured (`:1037-1112`): the unbounded
`async with self._lock:` was replaced by bounded `pool.acquire()`, the analysis
body is unchanged, and the single `finally` (`:1109-1112`) is the one place the
engine is handed back.

### 3.5 Orphan / hang nets (found by the real-engine verification, §5)

Two defects surfaced while verifying with a real engine; both are fixed:

**(a) A pooled engine bound to a closed event loop cannot be `quit()`.**
`backend/evaluator.py:388-416` `_force_kill_process()` — `SIGKILL` +
`waitpid(WNOHANG)` (we are the parent, so no zombie either) — is the fallback in
both `AsyncEnginePool._kill()` (`:653`) and `_quit_sync_engine()` (`:812`).

**(b) The interpreter hung on exit.** `chess.engine` starts one **non-daemon**
thread per engine (`chess/engine.py:76`, no `daemon=True`), and
`threading._shutdown()` joins non-daemon threads *before* any `atexit` callback
runs (CPython 3.13+ `threading._register_atexit`). With a pooled, still-alive
engine the join blocked forever. Fixed by registering on the threading hook as
well (`:883-921`):

```python
# Must run *before* non-daemon threads are joined (see docstring).
if hasattr(threading, "_register_atexit"):
    threading._register_atexit(_atexit_shutdown_sync_pool)
atexit.register(_atexit_shutdown_sync_pool)
```

`_atexit_shutdown_sync_pool()` then (i) `close_all()`s the pool, (ii) SIGKILLs
every PID in `_SPAWNED_PIDS` (`:352-377`, exposed as
`outstanding_engine_pids()`), and (iii) calls `kill_stray_child_engines()`
(`:418-465`), a narrow `/proc` sweep that SIGKILLs any *direct child* of this
interpreter whose cmdline mentions `stockfish`. Step (iii) is what stops a
caller that spawns `SimpleEngine` **outside** these pools (e.g.
`RepertoireManager._get_engine`, `backend/repertoire.py:80`) from deadlocking
the whole process — see §7.

### 3.6 §4C centipawn → height mapping: verified, no deviation

`AGENTS.md` §4C writes `height% = 50 + 50 * 2/(1+e^(-0.004·cp)) - 50`. Read
literally that is `100/(1+e^(-0.004·cp))`, which is **algebraically identical**
to what both frontends implement, `50 + 50*(2/(1+e^(-0.004·cp)) - 1)`. Checked
end to end (`backend/evaluator.py:211-232` `centipawn_to_height()`,
`tests/test_evaluator_stream.py::TestCentipawnMapping`):

| input | expected | result |
|---|---|---|
| `cp = 0` | 50 % (centre) | 50.0 ✔ |
| `cp = +150` | > 50 % | ✔ |
| `cp = -150` | < 50 % | ✔ |
| `cp = ±200` | `50+50*(2/(1+e^-0.8)-1)` | exact to 6 dp ✔ |
| `cp = +100` | 59.9 % (≈ +1 pawn, not saturated) | ✔ |
| `cp = ±10000` | 100 % / 0 % | ✔ |
| `cp = ±40000` (out of range) | saturated, no overflow | ✔ |

**No deviation found.** Sign is correct (positive = White = taller), scale
0.004/pawn gives a usable 59.9 % at +1.0, and the server saturates at ±10000
which is exactly where the curve is 100 %/0 % anyway — so the server clamp and
the frontend height clamp never disagree about sign or saturation. Mate is
handled *before* the curve by both consumers (`mate !== null` branch first), and
`MATE_SCORE_CP` bounds `score_cp` so a mate can't be mistaken for a big cp.

**Two pre-existing inconsistencies reported, not changed** (frontend-owned):
the two frontends clamp the *height* differently (builder 3–97, scout 5–95), and
`scout.js:1985-1987` flips `pct = 100 - pct` for black orientation **after** the
label class was already computed, so the label and the height contradict each
other in black orientation. Both are `frontend/**` and therefore not mine.

---

## 4. Before / after test evidence

New file `tests/test_evaluator_stream.py` — **29 tests**, no real long Stockfish
run (both spawn sites are stubbed and counted). The single exception is
`TestInterpreterExitPath`, which spawns a real engine for ~150 ms to prove the
exit path.

### 4.1 The three required regression tests

**BEFORE** (original `backend/evaluator.py` + `backend/analyzer.py` restored
from `git show HEAD:`, with a documented *legacy shim* supplying the new API
names with exactly the pre-fix behaviour so the tests can be expressed):

```
$ /home/robin/miniconda3/bin/python3 /tmp/opencode/fixer_c_before_runner.py
test_every_frame_exposes_score_cp_and_eval_alias ... FAIL
test_engine_crash_emits_typed_terminal_event_and_retires_the_engine ... FAIL
test_cap_is_enforced_and_saturation_raises_engine_busy ... FAIL

FAIL: test_every_frame_exposes_score_cp_and_eval_alias
AssertionError: 'eval' not found in {'depth': 4, 'score_cp': 30, 'mate': None,
  'best_move': 'e2e4', 'best_san': 'e4', 'final': False} : scout.js reads data.eval -- it must exist

FAIL: test_engine_crash_emits_typed_terminal_event_and_retires_the_engine
AssertionError: None != 'error'

FAIL: test_cap_is_enforced_and_saturation_raises_engine_busy
AssertionError: EngineBusy not raised

Ran 3 tests in 0.002s
FAILED (failures=3)
```

Whole new module against the original code: `Ran 28 tests … FAILED (failures=13, errors=14)`.
Without the shim the module cannot even import
(`ImportError: cannot import name 'AsyncEnginePool' from 'backend.evaluator'`).

**AFTER:**

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_evaluator_stream
Ran 29 tests in 2.564s
OK
```

### 4.2 The interpreter-exit regression test (found during verification)

**BEFORE:** `Ran 1 test in 45.013s / FAILED (failures=1)` —
`AssertionError: interpreter hung on exit with an un-quit engine
(threading._shutdown joins python-chess's non-daemon thread)`.
**AFTER:** part of the `Ran 29 tests … OK` above (subprocess exits in ~1 s).

### 4.3 Version-agnostic repro probe (`audit/scripts/FIXER-C-01_repro.py`)

Same script, same stubbed engines, run against both versions.

**BEFORE**
```
A) SSE eval frame keys : ['best_move', 'best_san', 'depth', 'final', 'mate', 'score_cp']
   has 'eval'         : False  <- scout.js (data.eval)
   scout.js sees scoreCp : None
   -> scout.js eval bar  : FROZEN at '+0.0' / 50%  (CODE-02-23 reproduced)
B) SSE error frame     : {"error": "Stockfish binary not found at /nonexistent/bin/stockfish"}
   has discriminating 'type': False -> None
   terminal ('final')    : None
   leaks a path          : True
C1 sequential sessions=5  spawns=5  distinct_processes=5
C2 concurrent sessions=8  spawns=8  PEAK_CONCURRENT_ENGINES=8
VERDICT  payload_has_eval=False  typed_terminal_error=False  engine_cap<=2=False   (exit 1)
```

**AFTER**
```
A) first frame keys : ['best_move','best_san','cached','depth','eval','fen','final','is_mate','mate','score_cp','type','v']
   has 'score_cp'   : True   has 'eval' : True   has 'depth' : True   mate/is_mate : True/False
   scout.js sees scoreCp : 120
   -> scout.js eval bar  : 61.8% (white to move, +1.20 expected)
B) frames emitted : 1 (stream terminates)
   payload : {"v":1,"type":"error",…,"final":true,"code":"engine_unavailable",
              "error":"Stockfish binary not found at <path>"}
   has discriminating 'type': True -> error
   has stable 'code' : True -> engine_unavailable
   terminal ('final') : True      leaks a path : False
C1 sequential sessions=5  spawns=1  distinct_processes=1
C2 concurrent sessions=8  spawns=1  PEAK_CONCURRENT_ENGINES=2
VERDICT  payload_has_eval=True  typed_terminal_error=True  engine_cap<=2=True   (exit 0)
```

### 4.4 Full test suite

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
```

| run | result |
|---|---|
| my session baseline (before any agent's work) | `Ran 104 tests in 5.731s` / `OK` |
| baseline of *other agents' current tree*, my patch reverted and my test file removed | `Ran 246 tests in 12.471s` / `FAILED (failures=9)` |
| **with my patch** | `Ran 266 tests in 12.240s` / `FAILED (failures=9)` |
| with my patch (earlier snapshot, count drifts as agents add tests) | `Ran 275 tests in 12.374s` / `FAILED (failures=8)` |

My 29 tests: **`Ran 29 tests in 2.564s / OK`**.

The failing set is a strict subset of the pre-existing baseline and every failure
is in another agent's file, all caused by their in-flight
`tests/fixtures.py` rewrite (the synthetic fixture has 12 games, not the 280 the
old clone-based test expects) and by **network-dependent** Lichess-explorer tests
in `backend/repertoire.py`:

```
FAIL: test_all_games_scan_and_count (test_repertoire)  AssertionError: 12 not greater than or equal to 280
FAIL: test_candidate_moves_2000 (test_repertoire)      (Lichess explorer: no network)
FAIL: test_candidate_moves_white_eval_convention (test_repertoire)  AssertionError: 0 not greater than 0
FAIL: ×5 test_scout / test_scout_pipeline             (scout agent's fixtures)
FAIL: ×2 test_studies                                (studies agent)
```

I introduced **no** new failure and removed none of these. Before my patch the
suite *hung* at exit; with it the suite completes in 12 s (§3.5b).

---

## 5. `ps` orphan-process evidence (real Stockfish)

`audit/scripts/FIXER-C-01_realengine.py` launches the real binary briefly
(trivial positions, depth 6–8, a few seconds) through the production code paths
and samples `ps -eo pid,args` in a background thread.

Baseline before the run (21 Stockfish PIDs, all children of *other* agents'
concurrently running `unittest discover tests` / probe processes — see §7):

```
$ ps -eo pid,args | grep -c "[s"]tockfish-linux"
21
```

Run output (abridged; full log in §5.1):

```
1) ASYNC SSE POOL -- 4 concurrent real depth-6 streams
   frames=5  final=True type=eval score_cp=3  eval=3  mate=None is_mate=False depth=6
   frames=5  final=True type=eval score_cp=28 eval=28 mate=None is_mate=False depth=6
   frames=5  final=True type=eval score_cp=22 eval=22 mate=None is_mate=False depth=6
   frames=5  final=True type=eval score_cp=493 eval=493 mate=None is_mate=False depth=6
   wall time for 4 concurrent streams: 0.32s
   async pool: max_size=2  spawned=2  peak_in_use=2  in_use_now=0  live_now=2

2) ABANDONED STREAM (client disconnects mid-search) -- depth 20, cut early
   async pool in_use=0  live=3   (retired engines are reaped on the next checkout)

3) SYNC ANALYZER POOL -- 6 concurrent GameAnalyzer sessions, real analyse
   worker 0..5 -> (0,8) (3,8) (5,8) (5,8) (3,8) (0,8)      <- every session analysed
   sync pool: max_size=2  spawned=2  peak_in_use=2  in_use=0  idle=2

4) EXPLICIT SHUTDOWN
   engine PIDs still tracked by this process BEFORE close(): [839844, 841149, 841150]
   engine PIDs still tracked by this process AFTER  close(): (none)
   of those, still present in `ps` (=> ORPHANS)          : (none)
   full host stockfish inventory now                    : [673194 … 809595]
   BASELINE PIDs still running                          : [673194 … 809595]
   ORPHAN PROCESSES (attributable to this run)          : (none)
EXIT 0
```

**Interpretation.** 4 concurrent SSE streams → 2 engine processes (cap honoured,
2 of 4 would have serialised on the old unbounded lock). 6 concurrent
`GameAnalyzer` sessions → 2 processes instead of 6. An abandoned mid-search
stream retires its engine and the slot comes back immediately. After
`close()`, all 3 PIDs this process ever spawned are gone from `ps`; the 21
remaining host PIDs are byte-identical to the baseline set.

`outstanding_engine_pids()` is tracked **per PID**, so "orphan" is
self-attributing and cannot be confused with another agent's processes.

### 5.1 The `atexit` net, separately (process exits with *no* `close()`)

```
=== BEFORE: stockfish PID list ===
527835 527854 619786 620681 620700 621006 640627 641733 641753 642760 644074 644093 644418 647373 647421 648462 648482 648746 649866 649886 650208
stockfish PIDs before: 21
this process spawned engine PIDs: [650290, 650302]
tracked & alive now: [650290, 650302]

=== script exited WITHOUT calling close_all() ===

=== AFTER: stockfish PID list ===
619786 620681 620700 621006 641733 641753 642760 644074 644093 644418 647373 647421 648462 648482 648746 649866 649886 650208 654488 654507 655484 659816 659823
```

`650290` and `650302` are absent from the after-list: the exit net terminated
both. (The count grows because other agents' suites keep spawning.)

### 5.2 Interpreter-exit hang, before → after

```
# BEFORE (original code)
test_repertoire (BEFORE my patch)  100s  Ran 21 tests in 2.423s  FAILED (failures=1)   <- hung on exit
test_analyzer  (BEFORE my patch)    2s  Ran  2 tests in 1.501s  OK

# AFTER
test_analyzer        2s  Ran  2 tests in 1.529s  OK
test_repertoire      3s  Ran 21 tests in 2.164s  FAILED (failures=1)   <- no longer hangs
test_evaluator_stream 2s Ran 29 tests in 2.096s  OK
```

`test_repertoire`'s residual failure is `AssertionError: 12 not greater than or
equal to 280` from the other agent's `tests/fixtures.py` rewrite — not mine.

---

## 6. Changes a route owner (FIXER-B) may want

I did not touch `backend/api.py`. Precise, optional follow-ups:

1. **`api.py:78-81` (`shutdown_event`)** — add
   `get_sync_engine_pool().close_all()` next to `await evaluator.close()`. Not
   required (the `threading._register_atexit` net covers it) but it makes
   shutdown deterministic and log-visible:
   ```python
   @app.on_event("shutdown")
   async def shutdown_event():
       evaluator = AsyncEngineEvaluator.get_instance()
       await evaluator.close()
       get_sync_engine_pool().close_all()
   ```
2. **`api.py:218` and `api.py:710`** — wrap the `GameAnalyzer` body in
   `except EngineBusy: raise HTTPException(503, headers={"Retry-After": "5"})`.
   Today a saturated sync pool blocks for up to 300 s and then yields a 500.
   Import `EngineBusy` from `backend.evaluator`.
3. **`api.py:997-1011` (`/api/repertoire/eval_stream`)** — optionally publish the
   contract for the frontend owners, e.g.
   `@app.get("/api/repertoire/eval_stream/contract")` returning
   `eval_stream_contract()`. Not required for the fix.
4. **`api.py:712` `analyzer._get_engine()`** — still reaches into a private
   method (pre-existing, `CODE-02-09` variant 3). Unchanged by me because
   `_get_engine()` now returns a *pooled* engine whose lease is returned by
   `analyzer.close()`, exactly as the previous per-request engine was, so the
   reach-through keeps working untouched.

---

## 7. Also noticed (not fixed)

1. **`backend/repertoire.py:77-91` has the *same* uncached engine bug as the
   old `GameAnalyzer`** — `RepertoireManager._get_engine()` spawns a
   `SimpleEngine` on a singleton and nothing ever calls `RepertoireManager.close()`.
   This is the direct cause of the `test_repertoire` exit hang
   (verified: it hangs on the original code too). **Owner: whoever owns
   `repertoire.py`.** Fix is one line: route it through
   `get_sync_engine_pool().acquire()`. I did not touch the file; my
   `kill_stray_child_engines()` net only *mitigates* the hang, it does not fix
   the unbounded process count on that path.
2. **17–35 Stockfish processes were already running on this host** before my
   work (parents: other agents' `python3 -m unittest discover tests` and
   `audit/scripts/atk1_b05_engine_race.py`, plus one `start.py --port 39311`),
   each ~310 MB RSS. That is the CODE-02-09 signature in the wild and is worth
   a cleanup pass; I did not kill another agent's processes.
3. **`frontend/repertoire_builder.js:1742`** — `if (cached.depth >= 14) return;`
   while the request is issued with `depth=16`, silently downgrading the search
   to 14 on a cache hit (`CODE-02-23` variant 3). Frontend-owned.
4. **`frontend/scout.js:1985-1987`** — black-orientation `pct = 100 - pct` runs
   *after* the label class is computed, so label and height contradict each
   other when flipped. Frontend-owned.
5. **The two frontends clamp the eval-bar height differently** (builder 3–97 %,
   scout 5–95 %). Cosmetic inconsistency; frontend-owned.
6. **The async search has no time cap** — `Limit(depth=max_depth)` only, so a
   pathological position at `depth=30` can run for minutes holding the engine
   slot. The bounded wait (`ASYNC_POOL_WAIT_TIMEOUT`, 20 s) now converts the
   resulting queue into a typed `engine_busy` frame instead of an indefinite
   hang, which bounds the *blast radius* but not the single request. A
   `time=` budget on the `Limit` is a behaviour change I did not want to make
   unasked (it can make a search stop short of `max_depth` and change the
   `final`/cache semantics).
7. **`@app.on_event` is deprecated** in current Starlette (lifespan handlers).
   Pre-existing; out of scope.

---

## 8. Residual risks

* **Sync pool saturation latency.** With `max_size=2` and a 300 s wait, a burst
  of >2 concurrent `/api/sync` requests queues instead of running in parallel.
  That is the intended trade (bounded resources) but it changes the latency
  profile of `POST /api/sync`; a route owner should map `EngineBusy` to 503
  (§6.2) rather than let it block for 5 minutes. Tunable via
  `CHESS_ENGINE_MAX_PROCS` / `CHESS_ENGINE_WAIT_TIMEOUT`.
* **Engine reuse across requests.** A recycled engine is only as clean as the
  previous request left it. Mitigated by the `ping()` + `returncode` health check
  on every checkout and by `discard()` on any exception, but a request that
  aborts a search *without* an exception (e.g. the thread is abandoned) could
  hand a mid-search engine to the next caller; `ping()` (isready) would then
  block until that search finishes. Not observed in the real-engine run; the
  async path avoids it entirely via `retire()`.
* **The contract is still only convention.** There is no `response_model` on an
  SSE route in FastAPI (it cannot validate a stream), so the schema is a
  documented constant plus `tests/test_evaluator_stream.py::test_contract_self_description`.
  A consumer that invents a *third* key name can still drift. The `eval` alias
  removes the specific drift that occurred; it does not make drift impossible.
* **`kill_stray_child_engines()` reads `/proc`** and only runs at interpreter
  exit on Linux. On other platforms it is a no-op (guarded by `OSError`), and
  the loop-registered hook is skipped where `threading._register_atexit` is
  absent (then the plain `atexit` registration applies — which, on CPython
  < 3.13, *is* the right hook anyway).
* **The pools hold up to 2 + 1 engines for up to `CHESS_ENGINE_IDLE_SECONDS`
  (600 s) after the last request.** That is ~190 MB RSS of idle Stockfish,
  versus zero in the old per-request design. It is the deliberate cost of reuse
  and it is bounded; `close_all()` / `evaluator.close()` release immediately.
