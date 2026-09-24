#!/usr/bin/env python3
"""
REATK-EVAL-01 — the shipped `repertoire_builder.js` cannot parse a single frame
of the shipped `eval_stream`, because `_frame()` appends an `event:` line to the
SSE block and the builder's parser assumes `data:` is the *only* line.

Method (no mocks of the code under test):
  1. Capture the REAL bytes of `GET /api/repertoire/eval_stream` through
     `TestClient` (real backend, real local Stockfish, depth 4 -> bounded).
  2. Replay those exact bytes through the REAL `frontend/repertoire_builder.js`
     `startEvalStream()` in the Node harness, with only `fetch` replaced by a
     reader over the captured text.
  3. Ablation: the same bytes with the `event:` lines removed (the pre-FIXER-C
     wire format) must work -> proves the `event:` line is the only difference.
  4. Control: `frontend/scout.js`'s line-wise parser on the same bytes works
     -> proves the bytes are valid and the defect is in the builder's parser.

Re-runnable and idempotent. Never touches production data (temp DB only).
"""
import json
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tests.test_setup  # noqa: F401,E402  — FIRST backend-adjacent import

from fastapi.testclient import TestClient  # noqa: E402
from backend.api import app  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVIDENCE_DIR = os.path.join(PROJECT_ROOT, "audit", "evidence")
DEPTH = 4
FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# A position the first capture never touches, so the server-side eval cache cannot
# serve a replay instead of the error frame we are trying to capture.
ERR_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"

client = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)


def capture_stream(fen: str = FEN, depth: int = DEPTH) -> str:
    """Return the exact response body the browser would receive."""
    resp = client.get("/api/repertoire/eval_stream", params={"fen": fen, "depth": depth})
    if resp.status_code != 200:
        raise SystemExit("eval_stream returned HTTP %s: %s" % (resp.status_code, resp.text[:400]))
    return resp.text


def js_literal(text: str) -> str:
    """Embed a string as a JS string literal (JSON encoding is valid JS)."""
    return json.dumps(text)


SCENARIO = textwrap.dedent(
    """
    return (async () => {
    const START = %(fen)s;
    const RAW  = %(raw)s;
    const ERRF = %(err)s;
    const ERRB = %(errbytes)s;
    // Pre-encoded byte array: the harness sandbox has TextDecoder but NOT
    // TextEncoder, so the chunk values are built here rather than in JS.
    const RAW_BYTES = %(bytes)s;
    const b = window.repertoireBuilder;
    b.evalCache = b.evalCache || new Map();
    b.evalCacheMax = b.evalCacheMax || 400;
    b.history = [{ ply: 0, fen: START, san: 'Start', uci: null, openingName: 'x', eco: '' }];
    b.currentStepIndex = 0;

    // Count what the real renderEvalBar() path is asked to draw. The DOM shim has
    // no eval-bar elements, so we count and swallow the body rather than letting
    // a missing element masquerade as the defect.
    let renders = [];
    b.renderEvalBar = function (scoreCp, mate, depth) { renders.push([scoreCp, mate, depth]); };
    let cachePuts = 0;
    b._evalCachePut = function (k, v) { cachePuts += 1; return v; };

    function reader(bytes, chunkSize) {
        let i = 0;
        return {
            read() {
                if (i >= bytes.length) return Promise.resolve({ done: true, value: undefined });
                const slice = bytes.slice(i, i + chunkSize);
                i += slice.length;
                return Promise.resolve({ done: false, value: Uint8Array.from(slice) });
            }
        };
    }

    async function drive(bytes, chunkSize) {
        renders = []; cachePuts = 0;
        window.fetch = function () {
            return Promise.resolve({ ok: true, status: 200, body: { getReader: () => reader(bytes, chunkSize) } });
        };
        await b.startEvalStream(START);
        return { renders: renders.slice(), cachePuts: cachePuts };
    }

    // --- the real builder, on the real bytes -------------------------------
    const live = await drive(RAW_BYTES, 37);

    // sanity: the same drive() must work on ANY well-formed payload, otherwise
    // a 0 could be the stub's fault rather than the application's.
    const canary = 'data: {"v":1,"type":"eval","fen":"x","depth":5,"score_cp":42,"eval":42,'
        + '"mate":null,"is_mate":false,"best_move":"e2e4","best_san":"e4","final":true,"cached":false}\\n\\n';
    const canaryBytes = Array.from(canary).map(ch => ch.charCodeAt(0));
    const canaryRun = await drive(canaryBytes, 11);

    // --- what the builder's own parser does, line by line -------------------
    // Verbatim copy of frontend/repertoire_builder.js:2114-2124 so the actual
    // thrown SyntaxError is visible (the app swallows it in `catch (e) {}`).
    let firstThrow = null, builderFrames = 0;
    for (const block of RAW.split('\\n\\n')) {
        const trimmed = block.trim();
        if (!trimmed) continue;
        if (trimmed.startsWith('data: ')) {
            try { JSON.parse(trimmed.slice(6)); builderFrames += 1; }
            catch (e) { if (firstThrow === null) firstThrow = String(e); }
        }
    }

    // --- control: scout.js's parser shape on the same bytes -----------------
    let scoutFrames = 0;
    for (const block of RAW.split('\\n\\n')) {
        const eventLine = block.split('\\n').find(l => l.startsWith('data: '));
        if (eventLine) { try { JSON.parse(eventLine.slice(6)); scoutFrames += 1; } catch (e) {} }
    }

    // --- ablation: pre-FIXER-C wire format (no `event:` line) ---------------
    const legacy = RAW.split('\\n').filter(l => !l.startsWith('event: ')).join('\\n');
    const legacyBytes = Array.from(legacy).map(ch => ch.charCodeAt(0));
    const ablated = await drive(legacyBytes, 37);

    // --- variant: a real terminal ERROR block (engine_unavailable) ----------
    // The builder has no `type === 'error'` branch at all: even a perfectly
    // parsed error frame is silently ignored, so the user is told nothing.
    const errRun = await drive(ERRB, 23);
    let errParsed = 0, errErr = null;
    for (const block of ERRF.split('\\n\\n')) {
        const trimmed = block.trim();
        if (!trimmed.startsWith('data: ')) continue;
        try { const d = JSON.parse(trimmed.slice(6)); if (d.type === 'error') errParsed += 1; }
        catch (e) { if (errErr === null) errErr = String(e); }
    }
    const errLineWise = ERRF.split('\\n').find(l => l.startsWith('data: '));
    let errCodeLineWise = null;
    if (errLineWise) { try { errCodeLineWise = JSON.parse(errLineWise.slice(6)).code; } catch (e) {} }

    return {
        rawBytes: RAW_BYTES.length,
        rawFirstBlock: RAW.split('\\n\\n')[0],
        hasEventLine: /\\nevent: /.test(RAW),
        canaryRenderCount: canaryRun.renders.length,
        canaryFirst: canaryRun.renders[0] || null,
        liveRenderCount: live.renders.length,
        liveCachePuts: live.cachePuts,
        builderParserFrames: builderFrames,
        builderParserFirstError: firstThrow,
        scoutParserFrames: scoutFrames,
        ablatedRenderCount: ablated.renders.length,
        ablatedCachePuts: ablated.cachePuts,
        ablatedFirst: ablated.renders[0] || null,
        errRawFirstBlock: ERRF.split('\\n\\n')[0],
        errBuilderParsed: errParsed,
        errBuilderError: errErr,
        errCodeLineWise: errCodeLineWise,
        errLiveRenderCount: errRun.renders.length
    };
    })();
    """
)


def run_node(raw: str, err_raw: str = "") -> dict:
    from tests import frontend_runtime
    js = SCENARIO % {
        "fen": js_literal(FEN),
        "raw": js_literal(raw),
        "bytes": json.dumps(list(raw.encode("utf-8"))),
        "err": js_literal(err_raw),
        "errbytes": json.dumps(list(err_raw.encode("utf-8"))),
    }
    payload = frontend_runtime.run_scenario(js, timeout=120)
    return {k: v for k, v in payload.items() if k != "__harness"}


def capture_error_stream() -> str:
    """Real error bytes: point the live evaluator pool at a missing binary."""
    from backend.evaluator import AsyncEngineEvaluator, AsyncEnginePool, STOCKFISH_PATH
    ev = AsyncEngineEvaluator.get_instance()
    original_pool = ev.pool
    ev.pool = AsyncEnginePool("/nonexistent/stockfish-binary", max_size=1, wait_timeout=1.0)
    try:
        resp = client.get("/api/repertoire/eval_stream", params={"fen": ERR_FEN, "depth": 4})
        if resp.status_code != 200:
            raise SystemExit("error-stream probe got HTTP %s" % resp.status_code)
        return resp.text
    finally:
        ev.pool = original_pool
        assert STOCKFISH_PATH  # keep the import meaningful for readers


def main() -> int:
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    raw = capture_stream()
    err_raw = capture_error_stream()
    with open(os.path.join(EVIDENCE_DIR, "REATK_EVAL_01_raw_stream.txt"), "w", encoding="utf-8") as fh:
        fh.write("### eval frames ###\n" + raw + "### terminal error frame ###\n" + err_raw)

    print("=== real wire bytes (depth=%d, %d bytes) ===" % (DEPTH, len(raw)))
    print(raw[:400].replace("\n", "\\n"))
    print("=== end of first block ===")
    print("=== real error block ===")
    print(err_raw.replace("\n", "\\n")[:400])
    print("=== end ===\n")

    result = run_node(raw, err_raw)
    for key in (
        "rawBytes", "hasEventLine", "canaryRenderCount", "canaryFirst",
        "liveRenderCount", "liveCachePuts",
        "builderParserFrames", "builderParserFirstError", "scoutParserFrames",
        "ablatedRenderCount", "ablatedCachePuts", "ablatedFirst",
        "errBuilderParsed", "errBuilderError", "errCodeLineWise", "errLiveRenderCount",
    ):
        print("%-26s %s" % (key + ":", result.get(key)))

    print("keys:", sorted(result))
    bugs = []
    if result["liveRenderCount"] == 0 and result["builderParserFrames"] == 0:
        bugs.append("builder parses 0/%d real bytes (JSON.parse error: %s)"
                    % (result["rawBytes"], result["builderParserFirstError"]))
    if result["ablatedRenderCount"] > 0:
        bugs.append("ablation (event: line removed) renders %d frame(s) -> the "
                    "`event:` line is the sole cause" % result["ablatedRenderCount"])
    if result["scoutParserFrames"] == 0:
        bugs.append("scout.js parser also fails -> the BYTES are invalid, not the parser")
    if result.get("errBuilderParsed") == 0 and result.get("errCodeLineWise"):
        bugs.append("terminal error frame (code=%s) is unparseable by the builder too, "
                    "so engine failures are reported to the user as silence"
                    % result.get("errCodeLineWise"))

    print()
    if bugs:
        print("VERDICT: REPRODUCED (REATK-EVAL-01)")
        for b in bugs:
            print("  - " + b)
        return 1
    print("VERDICT: not reproduced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
