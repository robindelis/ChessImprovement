# CODE-02 — Unproven suspicions & explicitly ruled-out hypotheses

> **This file is NOT a finding** and deliberately does not follow the BRIEF reporting
> contract. It is the companion index to the `CODE-02-01` … `CODE-02-23` series
> (agent **CODE-2**) and exists so the orchestrator can see what was checked and
> dismissed, not just what was reported.
>
> **ID note:** findings are namespaced `CODE-02-NN` because agents CODE-3/4/5
> concurrently occupied the plain `CODE-01`…`CODE-20` range (see `audit/STATE.md`
> fleet table). A first pass of these findings written as `CODE-01`…`CODE-20` was
> clobbered by those agents and has been rewritten under the `CODE-02-` prefix.

Everything here is *not* filed as a confirmed finding. Each item states what I checked and
why I could not prove it statically.

## Unproven suspicions

### U1 — `add_no_cache_header` (BaseHTTPMiddleware) may break `request.is_disconnected()` in the SSE generators
`api.py:64-71` registers a global `BaseHTTPMiddleware`. All three SSE routes
(`api.py:409`, `api.py:997`, `api.py:1019`) rely on `await request.is_disconnected()`
inside their generators (`evaluator.py:103, 151, 156, 166`; `repertoire.py:1536, 1799`).
Starlette's `BaseHTTPMiddleware` substitutes its own `receive` channel downstream, and
`Request.is_disconnected()` is a non-deterministic poll of that channel. **Not proven:**
`TestClient` cannot simulate a real client disconnect, and reading the installed Starlette
version's `BaseHTTPMiddleware` internals was not done. A live test (open the app, start
`eval_stream`, kill the tab, watch whether the generator returns) would settle it.
*Impact if real:* the engine lock is held until `max_depth` even after the client leaves,
serialising all subsequent evals.

### U2 — `evaluator.py:56` `await asyncio.wait_for(self._engine.ping(), timeout=2.0)` is a redundant liveness probe that costs a full UCI round-trip on **every** `stream_eval`
`evaluator.py:46-58` runs `ping()` on the existing engine before every reuse, i.e. one
`isready` round-trip per SSE request in addition to the analysis. **Not proven** that this
is measurable overhead — a `ping()` is sub-millisecond locally. Filing only as a code-shape
observation, not a defect.

### U3 — The global `explorer_cache` sign-flip migration (`db.py:216-226`) could double-negate rows
`db.py:221-222` negates every `m["eval"]` for black-to-move FENs that lack the
`eval_convention` marker. If any *other* writer ever stored white-perspective evals without
the marker, the migration would silently invert them once, then stamp the marker so it never
re-runs. **Not proven:** no such writer was found in `backend/`, and I did not inspect the
live `explorer_cache` contents (production-data rule). Reported in CODE-02-18 only for the
perf/gating defect, which *is* provable.

### U4 — `analyzer.analyze_game_openings` can raise `TypeError` on a terminal position
`analyzer.py:147`:
```python
best_move = info_before["pv"][0] if "pv" in info_before and len(info_before["pv"]) > 0 else move
```
If python-chess ever reports `"pv": None` (rather than omitting the key) for a
checkmate/stalemate position, `len(None)` raises. **Not proven:** python-chess 1.11.2
`SimpleEngine.analyse` omits `pv` entirely for terminal positions, and the `score_before <
-200` guard at `analyzer.py:151` fires first for any lost position. Rated unlikely.

### U5 — `POST /api/repertoire/import_line` and `POST /api/scout/import_repertoire` may not be truly byte-identical
I compared them by reading and by grepping the shared call signature. **Not proven** by
`diff`; a future patch to one and not the other is the actual risk, which CODE-02-12
already documents.

### U6 — `repertoire.py` blocking network I/O inside an `async def` generator
`repertoire.py:1841-1846` calls `self.get_explorer_game_count(...)` **synchronously** in
the middle of the `stream_analyze_user_games` async generator (it is *not* wrapped in
`asyncio.to_thread`, unlike `analyze_game_openings` at line 1840 which is). If that helper
performs a network call, it blocks the event loop for up to the client's timeout, once per
detected blunder. **Not proven:** `repertoire.py` is outside my assigned scope and I did not
open `get_explorer_game_count`. Flagged for CODE-1 (who owns `repertoire.py`).

### U7 — `frontend/scout.js:1987-1989` flips the eval-bar height for black orientation *after* setting the label class
```js
if (this.orientation === 'black') { pct = 100 - pct; }
```
Since the backend sends white-perspective `score_cp` and `repertoire_builder.js` does *not*
flip, one of the two bars is wrong in black orientation. **Not proven** which is
authoritative — it depends on the CSS `flex-direction` of `.eval-bar-fill`, which I did not
inspect. Reported as an adjacent note in CODE-02-23, not filed separately.

---

## Explicitly ruled out (checked, found correct)

| Hypothesis | Result |
|---|---|
| `transport.is_closing()` in `evaluator.py:51` might `AttributeError` on an `asyncio.subprocess.Process` and force an engine respawn on every request | **Ruled out.** `chess.engine.popen_uci` returns a `_UnixSubprocessTransport`; measured `type(transport).__mro__` includes `asyncio.transports.SubprocessTransport`, which has both `is_closing()` and `get_returncode()`. `audit/scripts/CODE-2_engine.py`. |
| A full stderr pipe could deadlock the Stockfish subprocess (no stderr drain) | **Ruled out.** Both `chess.engine.popen_uci` and `SimpleEngine.popen_uci` default to `debug=False`, which wires the child's stderr to `subprocess.DEVNULL`; `UciProtocol`'s reader thread continuously drains stdout. No deadlock path exists. |
| `GameAnalyzer`/`AsyncEngineEvaluator` share/corrupt one engine instance across concurrent requests | **Ruled out for the async evaluator** — it is a singleton guarded by `asyncio.Lock` (`evaluator.py:36, 155`). **Confirmed as a *different* problem** in CODE-02-09: the *sync* `GameAnalyzer` is per-request, so N requests = N processes. |
| `?min_loss=0` on `GET /api/blunders` is dropped by the falsy check at `api.py:514` | **Ruled out as a bug.** `cpl_loss` is only ever stored when `>= blunder_threshold_cp` (`analyzer.py:167`), so it is always positive and `min_loss=0` is semantically a no-op anyway. |
| The root `StaticFiles` mount returns an HTML 404 body for unknown `/api/*` paths | **Ruled out.** `GET /api/this_endpoint_does_not_exist` returns `404 {"detail":"Not Found"}` with `content-type: application/json` (the exception handler normalises it). The *method* problem is real though — see CODE-02-02. |
| `analyze_game_openings` blocks the event loop when called from `stream_analyze_user_games` | **Ruled out.** `repertoire.py:1840` wraps it in `await asyncio.to_thread(...)`. (See U6 for the neighbouring un-wrapped call.) |
| The 3-token FEN key in `db.py:324-344` contradicts the 4-token `normalize_fen` and would make the purge non-conservative | **Ruled out as a *defect*.** `repertoire.py` uses the 3-token prefix for move lookup in ~10 places, so `db.py` is consistent with the rest of the app. The 3-vs-4-token *doc* mismatch is filed in CODE-02-20(d) and CODE-02-15 instead. |
| `evaluate_training_move`'s `diff_str` sign at `api.py:727` is inverted | **Ruled out.** `cpl_diff = score_best - score_after`; a negative `cpl_diff` means the user gained cp, and the message prints `+N`. Correct as written. |
| `evaluator.py` mate→cp conversion loses information needed by the eval bar | **Cosmetic only.** The backend also sends the separate `mate` field, which the frontend prefers (`repertoire_builder.js:1707`). |
| `start.py` crashes when `CHESS_PORT` is unset | **Ruled out.** `os.environ.get("CHESS_PORT", os.environ.get("PORT", DEFAULT_PORT))` falls through correctly when unset; only the *empty-string* case raises. Filed precisely in CODE-02-21. |
