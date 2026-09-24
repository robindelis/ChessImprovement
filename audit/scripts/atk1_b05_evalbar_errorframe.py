"""BUILDER-05 probe (frontend): the Builder's eval_stream consumer ignores the SSE
`type`/`code`/`error` fields of the v1 contract and renders ERROR frames as if they
were evaluations, resetting the eval bar to "Equal position +0.0".

Driven through the repo's REAL-frontend harness (tests/frontend_harness.js), which
loads frontend/board.js + frontend/repertoire_builder.js from disk.  Nothing is mocked
except `fetch`, and the exact bytes the backend emits for `engine_busy` /
`engine_unavailable` are replayed (see backend/evaluator.py build_error_event).
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import tests.test_setup  # noqa: F401,E402  (isolated temp DB, per BRIEF hard rules)
import tests.frontend_runtime as frontend_runtime  # noqa: E402

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

SETUP_JS = """
const START = '%s';
const b = window.repertoireBuilder;
b.board = new window.InteractiveChessboard(document.getElementById('repertoireBoard'), {
    orientation: 'white', interactive: true,
    onMove: (from, to, promo, res) => b.onBoardMove(from, to, promo, res)
});
b.history = [{ ply: 0, fen: START, san: 'Start', uci: null,
               openingName: 'White Opening Repertoire', eco: '' }];
b.currentStepIndex = 0;

// The two DOM nodes renderEvalBar() writes to.
const fill = document.createElement('div');  fill.id = 'repEvalBarFill';
const label = document.createElement('div'); label.id = 'repEvalBarLabel';
const realGet = document.getElementById.bind(document);
document.getElementById = (id) => id === 'repEvalBarFill' ? fill
                               : id === 'repEvalBarLabel' ? label : realGet(id);

function streamOf(frames) {
    // The harness sandbox has no TextEncoder (it is a Node global, not an
    // ECMAScript intrinsic), so encode the ASCII frames by hand.  Uint8Array IS an
    // intrinsic of the vm context, so TextDecoder accepts it.
    const bytes = (s) => Uint8Array.from(s, c => c.charCodeAt(0));
    return { ok: true, body: { getReader() {
        let i = 0;
        return { read() {
            if (i >= frames.length) return Promise.resolve({ done: true });
            return Promise.resolve({ done: false, value: bytes(frames[i++]) });
        } };
    } } };
}
function sse(obj) { return 'data: ' + JSON.stringify(obj) + '\\n\\n'; }

// Byte-for-byte what backend/evaluator.py emits for ERR_ENGINE_BUSY.
const ERR_BUSY = sse({v:1, type:'error', fen:'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -',
    depth:0, score_cp:null, eval:null, mate:null, is_mate:false, best_move:'', best_san:'',
    final:true, cached:false, code:'engine_busy',
    error:'engine pool saturated (max_size=1, waited 20.0s)'});
const ERR_UNAVAIL = sse({v:1, type:'error', fen:'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -',
    depth:0, score_cp:null, eval:null, mate:null, is_mate:false, best_move:'', best_san:'',
    final:true, cached:false, code:'engine_unavailable',
    error:'<stockfish>'});
// A genuine evaluation of the same position.
const REAL_EVAL = sse({v:1, type:'eval', fen:'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -',
    depth:14, score_cp:312, eval:312, mate:null, is_mate:false, best_move:'e2e4', best_san:'e4',
    final:false, cached:false});

function bar() {
    return { height: fill.style.height, text: label.textContent, title: label.title };
}
""" % START


def run(body):
    payload = frontend_runtime.run_scenario(SETUP_JS + "\n" + body)
    return {k: v for k, v in payload.items() if k != "__harness"}, payload.get("__harness", {})


def main():
    print("=" * 78)
    print("A. control: a genuine depth-14 evaluation renders correctly")
    print("=" * 78)
    r, h = run("""
        window.fetch = () => Promise.resolve(streamOf([REAL_EVAL]));
        return b.startEvalStream(START).then(() => ({ bar: bar() }));
    """)
    print("   eval bar:", r["bar"])
    print("   console warnings:", h.get("consoleWarnings"))

    print("\n" + "=" * 78)
    print("B. the server replies with an engine_busy ERROR frame")
    print("=" * 78)
    r, h = run("""
        window.fetch = () => Promise.resolve(streamOf([ERR_BUSY]));
        return b.startEvalStream(START).then(() => ({
            bar: bar(),
            cachedEntry: b.evalCache.get(START) || null,
            warnings: window.__h ? null : undefined
        }));
    """)
    print("   eval bar rendered by the app:", r["bar"])
    print("   entry written into the position cache:", json.dumps(r["cachedEntry"]))
    print("   console warnings:", h.get("consoleWarnings"), " errors:", h.get("consoleErrors"))

    print("\n" + "=" * 78)
    print("C. engine_unavailable (stockfish binary missing) - same outcome")
    print("=" * 78)
    r, h = run("""
        window.fetch = () => Promise.resolve(streamOf([ERR_UNAVAIL]));
        return b.startEvalStream(START).then(() => ({ bar: bar() }));
    """)
    print("   eval bar rendered by the app:", r["bar"])
    print("   console warnings:", h.get("consoleWarnings"), " errors:", h.get("consoleErrors"))

    print("\n" + "=" * 78)
    print("D. a real +3.12 eval followed by an error frame for a *later* depth")
    print("=" * 78)
    r, h = run("""
        window.fetch = () => Promise.resolve(streamOf([REAL_EVAL, ERR_BUSY]));
        return b.startEvalStream(START).then(() => ({ bar: bar() }));
    """)
    print("   eval bar (was +3.12, now?):", r["bar"])


if __name__ == "__main__":
    main()
