# BUILDER-14 — the Repertoire Builder's live evaluation bar is 100% dead: the server's `event:` line makes every SSE block unparseable, and the regression is pinned green by the test suite
- **Severity:** critical
- **Type:** ui-regression
- **File:** `frontend/repertoire_builder.js` (parser) / `backend/evaluator.py` (wire format)
- **Line(s):** `frontend/repertoire_builder.js:2114-2125` (`startEvalStream` parse loop); producer `backend/evaluator.py:310-319` (`format_sse`) and `:322-327` (`_frame`)
- **Component:** Repertoire Builder — real-time progressive evaluation bar (AGENTS.md §4C)
- **Repro script:** `audit/scripts/REATK_EVAL_01_sse_client_parse.py`
- **Detected by:** REATK-1 (re-attack of FIXER-C-01 / `CODE-02-16` + `CODE-02-23`)
- **Evidence:** `audit/evidence/REATK_EVAL_01_raw_stream.txt` (real captured bytes, both an eval stream and a terminal error stream)

## Expected

`GET /api/repertoire/eval_stream` streams SSE blocks that the Repertoire Builder can consume, so
that navigating to any position animates the eval bar as Stockfish deepens (AGENTS.md §4C:
"Asynchronous Stockfish evaluates positions on the fly via Server-Sent Events").
`frontend/scout.js` consumes the *same* endpoint successfully, so the endpoint is not the problem.

## Actual

The builder's parser can never parse a single frame the server actually sends. Replaying the real
captured bytes through the real `startEvalStream()`:

```
rawBytes:                  917
hasEventLine:              True
canaryRenderCount:         1          <- control: a one-line `data:` block renders fine
liveRenderCount:           0          <- the REAL bytes: ZERO renders
liveCachePuts:             0
builderParserFrames:       0
builderParserFirstError:   SyntaxError: Unexpected non-whitespace character after JSON at position 209 (line 2 column 1)
scoutParserFrames:         4          <- scout.js parses the identical bytes fine
ablatedRenderCount:        4          <- same bytes minus the `event:` line: renders fine
```

The first block on the wire (real, depth 4, real Stockfish):

```
data: {"v":1,"type":"eval","fen":"rnbqkbnr/… w KQkq -","depth":1,"score_cp":30,"eval":30,…,"final":false,"cached":false}
event: eval
```

`block.trim()` is therefore `"data: {…}\nevent: eval"`. `startsWith('data: ')` passes — it is only a
**prefix** test — and `slice(6)` hands `JSON.parse` the two-line string `{…}\nevent: eval`, which
throws `SyntaxError: Unexpected non-whitespace character after JSON at position 209`. The throw
lands in `catch (e) {}` (`:2123`), i.e. **silently, for every frame of every stream**.

Observed user-visible consequences, all from the same two lines:

1. The eval bar never moves. It stays at the constructor default `+0.0 / Equal position`
   (`repertoire_builder.js:274`) for every position in the builder.
2. `this._evalCachePut()` (`:2121`) is never called, so the client-side position cache stays empty
   and the `cached.depth >= 14` short-circuit (`:2083`) can never fire — every navigation re-requests.
3. **Engine failures are reported to the user as silence.** A real terminal error block
   (`code:"engine_unavailable"`, captured live by pointing the pool at a missing binary) is equally
   unparseable (`errBuilderParsed: 0`, `SyntaxError … position 287`), and the builder has no
   `data.type === 'error'` branch anyway, so a dead engine, a saturated pool or an invalid FEN all
   look like "the bar just never moves".

## Root cause

`backend/evaluator.py::format_sse` renders **two** lines per block:

```python
data = json.dumps(payload, separators=(",", ":"))
lines = ["data: " + data]
if event:
    lines.append("event: " + event)      # <-- evaluator.py:317-318, added by FIXER-C-01
return "\n".join(lines) + "\n\n"
```

and `_frame()` (`:322-327`) passes `event="eval" | "eval_final" | "error"` for **every** block, so no
block is ever a single line.

The consumer assumes the opposite — that a block is *only* a `data:` line:

```js
// frontend/repertoire_builder.js:2114-2125
for (const block of lines) {
    const trimmed = block.trim();
    if (trimmed.startsWith('data: ')) {
        try {
            const data = JSON.parse(trimmed.slice(6));   // <-- gets "{…}\nevent: eval"
            …
        } catch (e) {}                                  // <-- swallows the SyntaxError
    }
}
```

`frontend/scout.js:2813-2821` parses the *same* endpoint correctly because it extracts the data line
first (`block.split('\n').find(l => l.startsWith('data: '))`), which is why the defect is invisible
from the Scout tab and invisible to a suite that only ever exercises one consumer.

**This is a regression introduced by FIXER-C-01, and the patch documents the exact mistake.**
`audit/patches/FIXER-C-01.md:65-69`:

> **`data:` is deliberately first.** `repertoire_builder.js:1774` does
> `block.trim().startsWith('data: ')`. An `event:` line placed first would make
> the Repertoire Builder drop every frame. … `data:`-first is safe for conforming clients
> *and* keeps both hand-rolled parsers working. **Do not reorder.**

The reasoning treats `startsWith('data: ')` as the builder's *only* dependency. It is not: the very
next statement, `JSON.parse(trimmed.slice(6))`, requires the block to be **exactly** one `data:`
line. Putting `event:` second instead of first changes nothing for that parser — the frame is
dropped either way. "Keeps both hand-rolled parsers working" is false; the ablation above shows the
identical bytes rendering 4 frames the moment the `event:` line is removed.

**Why the suite is green.** `tests/test_evaluator_stream.py:360-366` *asserts the broken format* and
quotes the builder's parser in its own docstring:

```python
def test_sse_framing_puts_data_first_and_terminates_with_blank_line(self):
    """repertoire_builder.js does block.trim().startsWith('data: ')."""
    frame = format_sse(build_eval_event(depth=5, score_cp=10), event="eval")
    self.assertTrue(frame.startswith("data: "), frame)
    self.assertIn("\nevent: eval\n\n", frame)          # pins the regression
```

and the module's `parse_frame()` helper (`:227-235`) is **line-wise** (`lines = frame.strip("\n").split("\n")`),
so it parses the two-line block happily. No test in the repo feeds real server bytes through the real
client parser: `tests/test_timeline_spam.py` stubs `fetch` to a promise that never resolves
(`tests/frontend_harness.js:291-293`), so it can only ever observe the request, never the response.
The suite is structurally incapable of catching this class of bug.

## Exploit / escalation

Not a security issue — a silent, total feature loss plus a test suite that certifies it. Adjacent
variants of the same root cause, all reachable and all confirmed or directly derivable:

1. **Terminal `error` and `eval_final` blocks** (confirmed above): `event: error` / `event: eval_final`
   are appended to *every* block, so a saturated engine pool (`engine_busy`) or a missing binary
   (`engine_unavailable`) is indistinguishable from a slow search. The builder has no error branch
   even when parsing succeeds, so this is a *second*, independent reason the user is never told.
2. **The same one-line assumption in two sibling parsers** —
   `frontend/repertoire_builder.js:866-870` (`/api/sync_games_stream`) and `:1403-1408`
   (the recent-games stream). They are safe *today* only because `backend/repertoire.py:1979-2077`
   and `:2355-2450` hand-build their frames as a single `yield f"data: {json.dumps(...)}\n\n"`.
   Any future patch that routes those endpoints through `evaluator.format_sse()` — the obvious
   de-duplication — silently kills both progress UIs the same way. Verified: no `event:` is emitted
   on those two routes today (`grep 'event: ' backend/repertoire.py` → no match), so this is a
   latent variant, not a live one.
3. **`sanitize_error()` does not sanitise the FEN.** `build_error_event(..., fen=norm_fen)` echoes the
   client's own FEN back into the error frame, which is harmless; but `sanitize_error` (`evaluator.py:285-294`)
   only strips POSIX-style paths, so a Windows-style `C:\…` engine path would pass through verbatim.
   Cosmetic, listed only because it sits on the same code path.
4. **Server-side cache interaction.** Because the client cache is never populated (§Actual 2), the
   server cache is the only cache in play — and it is keyed on the 4-token `normalize_fen` with
   `max_size=1000` and no TTL, so a long session evaluates the same position repeatedly instead of
   hitting the client cache the design intends.

## Suggested fix (frontend-side, minimal, does not touch FIXER-C's contract)

Make the builder's parser line-wise, exactly like `scout.js`'s — this is the conforming fix, keeps
`data:`-first/`event:`-second (which the SSE spec allows in any order) and needs no backend change:

```js
const dataLine = block.split('\n').find(l => l.startsWith('data: '));
if (!dataLine) continue;
let data;
try { data = JSON.parse(dataLine.slice(6)); }
catch (e) { console.warn('eval_stream: unparseable frame', e); continue; }
if (data.type === 'error') { this.showEvalStreamError(data); continue; }
if (data.score_cp !== undefined) { /* render + cache */ }
```

Note the bare `catch (e) {}` must not survive: it is what turned a hard parse failure into silence.
The same treatment applied to `:866-870` and `:1403-1408` closes variant 2.

**A ready-to-apply, verified patch is delivered as `audit/patches/REATK-BUILDER-14_suggested.diff`**
(3 parsers + one new `showEvalStreamError()`; `git apply --check` passes). It is deliberately **not
applied**: `frontend/repertoire_builder.js` belongs to the frontend fixer (BRIEF hard rule 6).

## Regression tests delivered (currently RED — that is the point)

Added to the existing `tests/test_evaluator_stream.py` (no new module, so `test_docs_contract.py`'s
exact-pinned inventory stays valid) as `TestShippedClientCanParseTheStream`:

| test | against live code | against the patched tree |
| :--- | :--- | :--- |
| `test_a_block_is_a_sequence_of_lines_with_exactly_one_data_line` | PASS | PASS |
| `test_the_builder_does_not_parse_the_whole_block_as_one_data_line` | **FAIL** → `[870, 1408, 2118]` | PASS |
| `test_real_startEvalStream_renders_every_real_frame` | **FAIL** → `0 != 5` | PASS |

The middle one is a source-level guard (it fails with the exact line numbers), the last one runs the
**shipped** `startEvalStream()` in the Node harness over real `format_sse()` bytes with only `fetch`
replaced, and includes a canary frame that proves a `0` cannot be the stub's fault.

Whole-suite status when these landed: `Ran 456 tests` — the only new reds are these two; the other
nine pre-existing reds (`test_docs_contract` ×3, `test_api_security`, `test_analyzer_correctness`,
`test_isolation_guard`, `test_repertoire_write_safety`, `test_scout_pipeline` ×3) belong to other
agents' in-flight work.

## Verification

```bash
# 1. the bug, end to end, against real server bytes
/home/robin/miniconda3/bin/python3 audit/scripts/REATK_EVAL_01_sse_client_parse.py   # exits 1
#   VERDICT: REPRODUCED (REATK-EVAL-01)
#     - builder parses 0/917 real bytes (JSON.parse error: SyntaxError: …)
#     - ablation (event: line removed) renders 4 frame(s) -> the `event:` line is the sole cause
#     - terminal error frame (code=engine_unavailable) is unparseable by the builder too

# 2. the fix (apply to a scratch copy of frontend/ + tests/, not the repo)
git apply --check audit/patches/REATK-BUILDER-14_suggested.diff
# then: 3/3 new tests OK, 182 tests across every frontend-executing module OK
```
