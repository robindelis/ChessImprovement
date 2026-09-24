# BUILDER-04 — The Builder renders SSE `type:"error"` frames as a 0.00 "Equal position" evaluation
- **Severity:** medium
- **Type:** wrong-result
- **File:** frontend/repertoire_builder.js
- **Line(s):** `startEvalStream()` — the frame guard is `if (data.score_cp !== undefined)` (~line 1992 in the current tree; the function starts at ~1940)
- **Component:** Repertoire Builder — progressive eval bar (SSE consumer)
- **Repro script:** `audit/scripts/atk1_b05_evalbar_errorframe.py`
- **Detected by:** ATK-1 (BUILDER)

## Expected

`backend/evaluator.py` documents an explicit v1 SSE contract (module docstring, lines 1-75):
every frame carries `v`, `type` (`"eval"` | `"error"`), `score_cp`, `eval`, `mate`, `is_mate`,
`best_move`, `best_san`, `final`, `cached`, and error frames add `code` + `error`. `final` is
"forced to `True` on an error frame **so a client that waits for a final frame stops instead
of hanging**".

A conforming client must therefore branch on `type`/`code` and must **not** paint an error
frame as an evaluation. On `engine_busy` / `engine_unavailable` / `engine_error` the Builder
must keep the previous score (or show an explicit "engine unavailable" state) and surface the
message — not silently claim the position is dead level.

## Actual

`startEvalStream()` gates on `score_cp !== undefined` and ignores `type`, `code` and `error`:

```javascript
const data = JSON.parse(trimmed.slice(6));
if (data.score_cp !== undefined) {          // null !== undefined  ->  TRUE
    this.renderEvalBar(data.score_cp, data.mate, data.depth);
    this.evalCache.set(fen, data);
}
```

and `renderEvalBar(null, null, 0)` falls through both branches to the defaults:

```javascript
let pct = 50;  let text = '+0.0';  let full = 'Equal position';
if (mate !== null && mate !== undefined) { ... }
else if (typeof scoreCp === 'number') { ... }        // typeof null === 'object' -> skipped
```

`evaluator._clamp_cp(None)` returns `None`, so an error frame's `score_cp` is `null` — which
is `!== undefined`. Verified against the **real shipped frontend** through
`tests/frontend_harness.js` (only `fetch` is stubbed; the exact bytes the server emits for
`engine_busy` are replayed):

```
A. control: a genuine depth-14 evaluation renders correctly
   eval bar: {'height': '77.69534596019572%', 'text': '+3.1',
              'title': 'White +3.12 pawns · Stockfish depth 14'}

B. the server replies with an engine_busy ERROR frame
   eval bar rendered by the app: {'height': '50%', 'text': '+0.0',
                                  'title': 'Equal position · Stockfish'}
   console warnings: []  errors: []

C. engine_unavailable (stockfish binary missing) - same outcome
   eval bar rendered by the app: {'height': '50%', 'text': '+0.0',
                                  'title': 'Equal position · Stockfish'}

D. a real +3.12 eval followed by an error frame for a later depth
   eval bar (was +3.12, now?): {'height': '50%', 'text': '+0.0',
                                'title': 'Equal position · Stockfish'}
```

Case D is the damaging one: a correct `+3.1` evaluation is **overwritten** with `+0.0` when the
stream later terminates with an error frame. No console warning, no toast, no
`window.showToast` — the failure is completely invisible, and in the blunder-review flow the
user is looking at the eval bar to judge how bad a position is.

The condition is easy to reach: `evaluator.ASYNC_POOL_MAX_SIZE` defaults to **1**
(`backend/evaluator.py:129`) with `ASYNC_POOL_WAIT_TIMEOUT = 20.0`, so any two overlapping
eval streams (Builder + Scout tab, or a slow engine) make the loser emit
`ERR_ENGINE_BUSY` after 20 s. `ERR_ENGINE_UNAVAILABLE` is emitted whenever the Stockfish
binary is missing or a checkout fails.

## Root cause

The frontend implements a pre-v1 subset of the contract: it was written when the server only
ever emitted `{"depth", "score_cp", "mate", "best_move", "best_san", "final"}` frames with no
`type` discriminator, and the guard `data.score_cp !== undefined` was a "is this an eval?"
test written before `null` was a legal value. The server rewrite (which added
`build_error_event`, `_clamp_cp`, the `type`/`code`/`error` fields and the explicit
`final: true` guarantee) did not update the client, and nothing in the contract is asserted
from the JS side.

## Exploit / escalation

Adjacent weaker variants of the same client-side gap:

1. **`is_mate` is ignored.** A mate frame (`mate: -1`, `score_cp: -10000`) is rendered as
   `-100.0` on the bar plus `-M1` text; the Builder never reads `is_mate`, so any consumer that
   trusts `score_cp` for magnitude (per the contract: *"never derive M3 from score_cp"*) will
   be wrong by up to 100 pawns.
2. **The Scout tab is the other consumer.** `evaluator.py`'s docstring names
   `frontend/scout.js` (`data.eval`) as the second hand-rolled parser; the same `null`
   confusion applies there, which is why the fix belongs in the contract + both parsers
   rather than in one call site.
3. **`final` is not used for cache validation.** Because the client caches every frame that
   passes the guard, an aborted stream's partial (non-`final`) depth is stored, and
   `if (cached.depth >= 14) return;` then suppresses every later refresh of that position.
