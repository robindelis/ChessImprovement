"""
tests/test_trainer_replay.py

Regression tests for the Trainer's **line replay** ("where did this drill
position come from?") and for the h/l vim-key parity across the three views that
walk through a game.

Why these tests live here
-------------------------
* The replay is entirely a *frontend* feature: the drill payloads already carry
  the SAN path (`path_san`, or `move_history_sans` for deviation drills), so
  nothing is fetched and nothing is persisted. There is no database access at all
  in this module, which is why — exactly like
  `tests/test_builder_frontend_contracts.py` — `tests.test_setup` is NOT imported
  (importing it would bring a temp SQLite database into a test that never touches
  one).
* The behavioural half runs the **real shipped** `frontend/repertoire_trainer.js`
  through `tests/frontend_harness.js` (Node `vm` + DOM shim), the same harness
  `tests/test_line_end_semantics.py` and `tests/test_pedagogical_trainer.py` use.
  A hand-written JavaScript copy of the mapping would pass with the feature
  deleted, which is precisely the failure mode audit finding CODE-05-05 recorded.

Nothing here can skip silently: a missing/emptied frontend file is an
`AssertionError`; only an absent `node` binary makes the runtime tests skip.

Cross-engine FEN normalisation
------------------------------
The timeline validation compares a chess.js FEN (the rebuilt path) with a
`parent_fen` produced by python-chess. The two libraries disagree about the
en-passant field on every double pawn push (python-chess writes it only when a
legal en-passant capture exists), so the validation uses 3-token keys. The
regression tests below use *literal* python-chess-style FENs (en-passant `-`);
building both sides with chess.js is what let the bug through the first time.
"""
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(REPO_ROOT, "frontend")


def _read(name):
    with open(os.path.join(FRONTEND_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


def _html_ids(html):
    return set(re.findall(r'\bid="([^"]+)"', html))


def _tag_with_id(html, element_id):
    m = re.search(r"<[^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*>", html)
    return m.group(0) if m else None


def _html_window(html, start_marker, end_marker):
    """The markup from `start_marker` up to the next `end_marker` (exclusive)."""
    i = html.index(start_marker)
    j = html.index(end_marker, i)
    return html[i:j]


def _keydown_block(source, anchor):
    """The `window.addEventListener('keydown', …)` handler that contains `anchor`.

    A file may bind several keydown handlers; the one this test cares about is the
    one that checks the view, so the anchor is the view lookup itself.
    """
    for m in re.finditer(r"window\.addEventListener\('keydown'", source):
        start = m.start()
        end = source.index("\n        });", start)
        block = source[start:end]
        if anchor in block:
            return block
    raise AssertionError("no keydown handler containing %r" % (anchor,))


def fen3(fen):
    """3-token FEN key (placement + turn + castling) — see `_fen3()`.

    The en-passant token is excluded on purpose: `parent_fen` is a python-chess FEN
    and the replay timeline is rebuilt with chess.js, and the two libraries only
    agree on the en-passant field when a legal en-passant capture exists.
    """
    return " ".join(str(fen).split()[:3])


def db_style_fen_key(fen):
    """The 4-token key the app writes into the DB (AGENTS.md §4, `normalize_fen`).

    Only used here to *document the bug*: two engines' FENs disagree on this key
    whenever the last move was a double pawn push, so it must never be used to
    compare a chess.js FEN with a python-chess `parent_fen`.
    """
    return " ".join(str(fen).split()[:4])


def backend_style_fen(fen):
    """The same position as python-chess would write it: no en-passant square.

    python-chess omits the en-passant target unless a legal en-passant capture
    exists, chess.js writes it after every double pawn push. Every FEN in this
    module that models a *backend* `parent_fen` goes through here (or is a literal
    from the planner's cross-engine probe), so the tests cannot accidentally compare
    chess.js with chess.js again.
    """
    parts = str(fen).split()
    if len(parts) >= 4:
        parts[3] = "-"
    return " ".join(parts)


# Ids the replay feature added. Every one of them is looked up with a literal
# `getElementById('…')`, so a typo would make the control silently dead.
REPLAY_IDS = (
    "trainReplayBar",
    "btnTrainReplay",
    "btnTrainReplayStart",
    "btnTrainReplayPrev",
    "btnTrainReplayNext",
    "btnTrainReplayExit",
    "trainReplayStatus",
)


class TestReplayDomContracts(unittest.TestCase):
    """The Trainer's replay controls must exist in index.html AND be wired by the JS."""

    def setUp(self):
        self.html = _read("index.html")
        self.js = _read("repertoire_trainer.js")

    def test_every_replay_id_exists_in_index_html_and_is_looked_up(self):
        ids = _html_ids(self.html)
        for element_id in REPLAY_IDS:
            self.assertIn(element_id, ids, "#%s is missing from index.html" % element_id)
            self.assertIn(
                "getElementById('%s')" % element_id, self.js,
                "#%s exists in index.html but repertoire_trainer.js never binds it" % element_id)

    def test_the_replay_button_is_labeled_and_keeps_its_box(self):
        """AGENTS.md §8.4: a conditionally available control keeps its box (visibility + disabled)."""
        tag = _tag_with_id(self.html, "btnTrainReplay")
        self.assertIsNotNone(tag, "#btnTrainReplay is missing from index.html")
        compact = tag.replace(" ", "")
        self.assertIn("visibility:hidden", compact)
        self.assertIn("min-width:118px", compact)
        self.assertNotIn("display:none", compact,
                         "an inline display:none cannot be beaten by a JS visibility toggle")
        labeled = re.search(r'<button id="btnTrainReplay"[^>]*>\s*([^<]*?)\s*</button>', self.html)
        self.assertIsNotNone(labeled, "#btnTrainReplay has no label")
        self.assertEqual(labeled.group(1), "▶ Replay Line")

    def test_the_replay_bar_is_a_constant_height_visibility_toggled_strip(self):
        """AGENTS.md §8.5: permanently in flow, constant height, revealed with `visibility`."""
        window = _html_window(self.html, 'id="trainReplayBar"', 'class="drill-sidebar"')
        compact = window.replace(" ", "")
        self.assertIn("display:flex", compact)
        self.assertNotIn("display:none", compact)
        self.assertIn("visibility:hidden", compact)
        self.assertIn("min-height:34px", compact)
        self.assertIn("max-height:34px", compact)
        self.assertIn("box-sizing:border-box", compact)
        self.assertIn("flex-wrap:nowrap", compact)
        self.assertIn("overflow:hidden", compact)

    def test_the_replay_bar_lists_the_arrow_and_vim_keys(self):
        window = _html_window(self.html, 'id="trainReplayBar"', 'class="drill-sidebar"')
        for snippet in (
            "<kbd>&larr;</kbd>/<kbd>h</kbd> Back",
            "<kbd>&rarr;</kbd>/<kbd>l</kbd> Next",
            "<kbd>Space</kbd> Pause",
            "<kbd>Esc</kbd> Exit",
            "<kbd>r</kbd> Retry",
            "<kbd>i</kbd> Hint",
        ):
            self.assertIn(snippet, window, "the replay bar's kbd hint row is missing %r" % snippet)

    def test_the_trainer_reveals_the_bar_with_visibility_never_display(self):
        block = re.search(r"_renderReplayBar\(\)\s*\{[\s\S]*?\n    \}\n", self.js)
        self.assertIsNotNone(block, "_renderReplayBar() is missing")
        body = block.group(0)
        self.assertIn("bar.style.visibility", body)
        self.assertIn("btnReplay.style.visibility", body)
        self.assertNotIn("bar.style.display", body)
        self.assertNotIn("btnReplay.style.display", body)
        self.assertIn("btnReplay.disabled", body)

    def test_the_replay_tick_is_drill_token_guarded(self):
        """CODE-07: a pending tick must never repaint the board of a newer drill."""
        block = re.search(r"_scheduleReplayTick\(\)\s*\{[\s\S]*?\n    \}\n", self.js)
        self.assertIsNotNone(block, "_scheduleReplayTick() is missing")
        body = block.group(0)
        self.assertIn("const token = this.drillToken;", body)
        self.assertIn("if (token !== this.drillToken) return;", body)
        # …and the timer must be part of the drill teardown.
        clear = re.search(r"_clearDrillTimers\(\)\s*\{[\s\S]*?\n    \}\n", self.js).group(0)
        self.assertIn("this._stopReplayTimer();", clear)

    def test_the_legacy_h_l_actions_kept_their_buttons(self):
        """The owner's ruling: Try Again / Show Hint keep their buttons, `r`/`i` are added keys."""
        for element_id in ("btnTrainRetry", "btnTrainHint"):
            self.assertIn(element_id, _html_ids(self.html))
        mapping = re.search(r"handleTrainerKey\(e\)\s*\{[\s\S]*?\n    \}\n", self.js).group(0)
        self.assertIn("this.retryCurrentPosition();", mapping)
        self.assertIn("this.showHint();", mapping)
        self.assertRegex(mapping, r"key === 'r' \|\| key === 'R'")
        self.assertRegex(mapping, r"key === 'i' \|\| key === 'I'")


class TestHlParityInAllViews(unittest.TestCase):
    """Owner request: "wherever arrows move through the game, h/l must too".

    The Builder and the Scout already satisfied this; the Trainer used to bind
    ←/h to *retry* and →/l to *hint*. This test pins the parity in all three, so
    a future change cannot silently drop one of the vim keys again.
    """

    # (condition regex, the call the condition must reach)
    BUILDER_PAIRS = (
        (r"if \(e\.key === 'ArrowLeft' \|\| e\.key === 'h' \|\| e\.key === 'H'\) \{", "this.stepBack()"),
        (r"if \(e\.key === 'ArrowRight' \|\| e\.key === 'l' \|\| e\.key === 'L'\) \{", "this.stepForward()"),
        (r"if \(e\.key === 'ArrowUp' \|\| e\.key === 'k' \|\| e\.key === 'K' \|\| e\.key === 'Home'\) \{", "this.jumpToStart()"),
        (r"if \(e\.key === 'ArrowDown' \|\| e\.key === 'j' \|\| e\.key === 'J' \|\| e\.key === 'End'\) \{", "this.jumpToEnd()"),
    )
    SCOUT_PAIRS = (
        (r"if \(e\.key === 'ArrowLeft' \|\| e\.key === 'h'\) \{", "this.prevStep()"),
        (r"if \(e\.key === 'ArrowRight' \|\| e\.key === 'l'\) \{", "this.nextStep()"),
        (r"if \(e\.key === 'Home' \|\| e\.key === 'ArrowUp' \|\| e\.key === 'k'\) \{", "this.firstStep()"),
        (r"if \(e\.key === 'End' \|\| e\.key === 'ArrowDown' \|\| e\.key === 'j'\) \{", "this.lastStep()"),
    )
    TRAINER_PAIRS = (
        (r"if \(key === 'ArrowLeft' \|\| key === 'h' \|\| key === 'H'\) \{", "this.replayStep(-1)"),
        (r"if \(key === 'ArrowRight' \|\| key === 'l' \|\| key === 'L'\) \{", "this.replayStep(1)"),
        (r"if \(key === 'ArrowUp' \|\| key === 'k' \|\| key === 'K' \|\| key === 'Home'\) \{", "this.replayJumpTo(0)"),
        (r"if \(key === 'ArrowDown' \|\| key === 'j' \|\| key === 'J' \|\| key === 'End'\) \{", "this.replayJumpToEnd()"),
    )

    def _assert_pairs(self, source, pairs, view):
        for condition, call in pairs:
            m = re.search(condition, source)
            self.assertIsNotNone(m, "%s: no key condition matching %r" % (view, condition))
            tail = source[m.end():m.end() + 260]
            self.assertIn(call, tail,
                          "%s: the keys matched by %r do not reach %s" % (view, condition, call))

    def test_builder_arrow_and_vim_keys_share_one_handler(self):
        source = _read("repertoire_builder.js")
        block = _keydown_block(source, "document.getElementById('viewRepertoire')")
        self._assert_pairs(block, self.BUILDER_PAIRS, "builder")

    def test_scout_arrow_and_vim_keys_share_one_handler(self):
        source = _read("scout.js")
        block = _keydown_block(source, "document.getElementById('viewScout')")
        self._assert_pairs(block, self.SCOUT_PAIRS, "scout")

    def test_trainer_arrow_and_vim_keys_share_one_mapping(self):
        source = _read("repertoire_trainer.js")
        dispatcher = _keydown_block(source, "document.getElementById('viewRepTrainer')")
        self.assertIn("this.handleTrainerKey(e);", dispatcher,
                      "the trainer's keydown listener must delegate to the testable mapping")
        mapping = re.search(r"handleTrainerKey\(e\)\s*\{[\s\S]*?\n    \}\n", source)
        self.assertIsNotNone(mapping, "handleTrainerKey(e) is missing")
        self._assert_pairs(mapping.group(0), self.TRAINER_PAIRS, "trainer")

    def test_the_trainer_never_binds_h_l_to_retry_or_hint_anymore(self):
        source = _read("repertoire_trainer.js")
        dispatcher = _keydown_block(source, "document.getElementById('viewRepTrainer')")
        self.assertNotIn("this.retryCurrentPosition()", dispatcher)
        self.assertNotIn("this.showHint()", dispatcher)
        # …and the view guard plus the browser-shortcut guard are kept (CODE-22).
        self.assertIn("!trainerView.classList.contains('active')", dispatcher)
        self.assertIn("e.ctrlKey || e.metaKey || e.altKey", dispatcher)
        self.assertIn("isContentEditable", dispatcher)


# --------------------------------------------------------------------------- #
#  Runtime: the real shipped `frontend/repertoire_trainer.js`
# --------------------------------------------------------------------------- #

_BUILD_LINE_STEPS_SCENARIO = r"""
// `_buildLineSteps()` turns the drill's SAN path into concrete board steps.
// It must refuse anything it cannot honour, so the Replay button can never lie
// about where it is.
const trainer = new RepertoireTrainer();

function fenAfter(moves) {
    const c = new Chess();
    moves.forEach(function (m) { c.move(m); });
    return c.fen();
}
const FEN_START = (new Chess()).fen();
const FEN_E4 = fenAfter(['e4']);
const FEN_E4E5 = fenAfter(['e4', 'e5']);
const FEN_E4E5NF3 = fenAfter(['e4', 'e5', 'Nf3']);
const FEN_NF3D5 = fenAfter(['Nf3', 'd5']);
const FEN_D4D5NF3 = fenAfter(['d4', 'd5', 'Nf3']);

// A real Spanish line whose last move is the double pawn push ...b5.
const SPANISH = ['e4', 'e5', 'Nf3', 'Nc6', 'Bb5', 'a6', 'Ba4', 'Nf6', 'O-O', 'Be7', 'Re1', 'b5'];
const FEN_SPANISH = fenAfter(SPANISH);

// Literal python-chess-style FENs (`ep = '-'`, the planner's cross-engine probe).
const BACKEND_E4E5 = 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2';
const BACKEND_E4C5 = 'rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2';
const BACKEND_NF3D5 = 'rnbqkbnr/ppp1pppp/8/3p4/8/5N2/PPPPPPPP/RNBQKB1R w KQkq - 0 2';
const BACKEND_SPANISH = 'r1bqk2r/2ppbppp/p1n2n2/1p2p3/B3P3/5N2/PPPP1PPP/RNBQR1K1 w kq - 0 7';

function brief(steps) {
    if (!steps) return null;
    return {
        len: steps.length,
        first: steps[0],
        last: steps[steps.length - 1],
        sans: steps.map(function (s) { return s.san; }),
        fen3Last: trainer._fen3(steps[steps.length - 1].fen)
    };
}

/* ------------------------------------------------------------------ *
 * `_fen3()` (placement + turn + castling) is the only key that may compare a
 * chess.js FEN with a python-chess `parent_fen`: the en-passant token is written
 * under a different rule by the two libraries, and a 4-token comparison therefore
 * refused every drill whose last move was a double pawn push. The shipped file
 * no longer has a 4-token helper at all — the DB key below is computed on the
 * Python side, purely to document the difference.
 * ------------------------------------------------------------------ */
const key3Chess = trainer._fen3(FEN_E4E5);
const key3Backend = trainer._fen3(BACKEND_E4E5);

const valid = brief(trainer._buildLineSteps({ path_san: ['e4'], parent_fen: FEN_E4 }));
const fallback = brief(trainer._buildLineSteps({ move_history_sans: ['e4', 'e5'], parent_fen: FEN_E4E5 }));
const longer = brief(trainer._buildLineSteps({ path_san: ['e4', 'e5', 'Nf3'], parent_fen: FEN_E4E5NF3 }));
const mismatch = brief(trainer._buildLineSteps({ path_san: ['e4'], parent_fen: FEN_E4E5 }));
const mismatchOpening = brief(trainer._buildLineSteps({ path_san: ['e4', 'e5', 'Nf3'], parent_fen: FEN_D4D5NF3 }));
const illegal = brief(trainer._buildLineSteps({ path_san: ['e4', 'e5', 'Kxe5'], parent_fen: FEN_E4E5 }));
const emptyPath = brief(trainer._buildLineSteps({ path_san: [], move_history_sans: [], parent_fen: FEN_E4 }));
const noPath = brief(trainer._buildLineSteps({ repertoire: 'white', parent_fen: FEN_E4 }));
const noDrill = brief(trainer._buildLineSteps(null));

/* CRoss-engine regression: python-chess-style `parent_fen` (en-passant `-`)
 * against a path whose last move is a double pawn push. */
const crossE4E5 = brief(trainer._buildLineSteps({
    path_san: ['e4', 'e5'],
    parent_fen: BACKEND_E4E5
}));
const crossE4C5 = brief(trainer._buildLineSteps({
    path_san: ['e4', 'c5'],
    parent_fen: BACKEND_E4C5
}));
const crossNf3D5 = brief(trainer._buildLineSteps({
    path_san: ['Nf3', 'd5'],
    parent_fen: BACKEND_NF3D5
}));
const crossNf3D5ChessJs = brief(trainer._buildLineSteps({ path_san: ['Nf3', 'd5'], parent_fen: FEN_NF3D5 }));
const crossSpanish = brief(trainer._buildLineSteps({ path_san: SPANISH, parent_fen: BACKEND_SPANISH }));

// `_loadLineSteps()` derives the timeline from the drill on screen…
trainer.currentDrill = { path_san: ['e4', 'e5'], parent_fen: FEN_E4E5 };
trainer.currentFen = FEN_E4E5;
trainer._loadLineSteps();
const loaded = { steps: trainer.lineSteps.length, index: trainer.lineIndex };

// …and never rebuilds it while a replay is on screen (the board is scrubbed then).
trainer.replayMode = true;
trainer.currentDrill = { path_san: ['e4'], parent_fen: FEN_E4 };
trainer._loadLineSteps();
const lockedMidReplay = { steps: trainer.lineSteps.length, index: trainer.lineIndex };
trainer.replayMode = false;

return {
    fenStart: FEN_START,
    fenE4: FEN_E4,
    fenE4E5: FEN_E4E5,
    fenE4E5Nf3: FEN_E4E5NF3,
    fenNf3D5: FEN_NF3D5,
    backendE4E5: BACKEND_E4E5,
    backendE4C5: BACKEND_E4C5,
    backendNf3D5: BACKEND_NF3D5,
    backendChessJsE4E5: FEN_E4E5,
    chessJsSpanish: FEN_SPANISH,
    backendSpanish: BACKEND_SPANISH,
    keys: {
        fen3Chess: key3Chess,
        fen3Backend: key3Backend
    },
    valid: valid,
    fallback: fallback,
    longer: longer,
    mismatch: mismatch,
    mismatchOpening: mismatchOpening,
    illegal: illegal,
    emptyPath: emptyPath,
    noPath: noPath,
    noDrill: noDrill,
    crossE4E5: crossE4E5,
    crossE4C5: crossE4C5,
    crossNf3D5: crossNf3D5,
    crossNf3D5ChessJs: crossNf3D5ChessJs,
    crossSpanish: crossSpanish,
    loaded: loaded,
    lockedMidReplay: lockedMidReplay
};
"""


_REPLAY_KEYS_SCENARIO = r"""
// A recording stub board: the harness has no `#trainerBoard`, and the replay only
// ever needs this surface (loadFEN / setLastMove / clearArrows / setArrows / getFEN
// / options.interactive).
function makeStubBoard() {
    const log = { loadFEN: [], setLastMove: [], setArrows: [], clearArrows: 0 };
    const board = {
        options: { interactive: true },
        chess: { get: function () { return null; } },
        _fen: null,
        _lastMove: null,
        _arrows: [],
        loadFEN: function (fen) { this._fen = fen; this._lastMove = null; this._arrows = []; log.loadFEN.push(fen); },
        setLastMove: function (from, to) { this._lastMove = (from && to) ? [from, to] : null; log.setLastMove.push([from, to]); },
        clearArrows: function () { this._arrows = []; log.clearArrows++; },
        setArrows: function (arrows) { this._arrows = (arrows || []).slice(); log.setArrows.push((arrows || []).slice()); },
        getFEN: function () { return this._fen; },
        addArrow: function () { return true; },
        playMove: function () {},
        setOrientation: function () {}
    };
    return { board: board, log: log };
}

function press(trainer, key, extra) {
    const ev = Object.assign({ key: key, preventDefault: function () {} }, extra || {});
    return trainer.handleTrainerKey(ev);
}

function snapshot(trainer, board) {
    return {
        index: trainer.lineIndex,
        steps: trainer.lineSteps.length,
        mode: trainer.replayMode,
        playing: trainer.replayPlaying,
        interactive: board.options.interactive,
        fen: board._fen,
        waiting: trainer.isWaitingForUser,
        lastMove: board._lastMove
    };
}

// The replay controls are looked up by id, so give the shim the real ids from
// index.html: `_renderReplayBar()` must then run for real, not just early-return.
['trainReplayBar', 'btnTrainReplay', 'btnTrainReplayStart', 'btnTrainReplayPrev',
 'btnTrainReplayNext', 'btnTrainReplayExit', 'trainReplayStatus',
 'trainFeedback', 'btnTrainHint', 'btnTrainToggleArrows'
].forEach(function (id) {
    if (!document.getElementById(id)) document.byId.set(id, document.createElement('div'));
});

const toasts = [];
window.showToast = function (message) { toasts.push(String(message)); };

const barEl = document.getElementById('trainReplayBar');
const replayBtnEl = document.getElementById('btnTrainReplay');
const statusEl = document.getElementById('trainReplayStatus');
const feedbackEl = document.getElementById('trainFeedback');

function fenAfter(moves) {
    const c = new Chess();
    moves.forEach(function (m) { c.move(m); });
    return c.fen();
}

const DRILL_FEN = fenAfter(['e4', 'e5']);
const FEN_E4 = fenAfter(['e4']);
const START_FEN = (new Chess()).fen();
// The same position as the BACKEND sends it: python-chess writes the en-passant
// square only when a legal en-passant capture exists, so it is `-` here while
// chess.js writes `e6` (see `_fen3()`). This literal is what the shipped app
// receives for a drill after 1.e4 e5 — and it is exactly what used to make the
// replay go dark.
const BACKEND_DRILL_FEN = 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2';

function makeDrill(overrides) {
    return Object.assign({
        repertoire: 'white',
        id: 77,
        expected_uci: 'g1f3',
        expected_san: 'Nf3',
        last_move: { from: 'e7', to: 'e5', san: 'e5' },
        path_san: ['e4', 'e5'],
        path_formatted: '1. e4 e5',
        parent_fen: DRILL_FEN
    }, overrides || {});
}

function makeTrainer() {
    const trainer = new RepertoireTrainer();
    const stub = makeStubBoard();
    trainer.board = stub.board;
    // No real tick may ever fire during the scenario.
    trainer.replayDelayMs = 100000;
    trainer.currentDrill = makeDrill();
    trainer.currentFen = DRILL_FEN;
    trainer.savedPlanArrows = [];
    trainer.isWaitingForUser = true;
    trainer._loadLineSteps();
    return { trainer: trainer, board: stub.board, log: stub.log };
}

/* ------------------------------------------------------------------ *
 * 1. The timeline on a drill with a 2-ply path → 3 board steps.
 * ------------------------------------------------------------------ */
const T1 = makeTrainer();
const t = T1.trainer;
const b1 = T1.board;
const afterLoad = {
    snap: snapshot(t, b1),
    btnVisibility: replayBtnEl.style.visibility,
    btnDisabled: replayBtnEl.disabled,
    barVisibility: barEl.style.visibility,
    status: statusEl.textContent
};

/* 2. startReplay(): fresh replay from the FIRST position of the line. */
t.startReplay();
const afterStart = {
    handled: true,
    snap: snapshot(t, b1),
    loads: T1.log.loadFEN.slice(),
    btnLabel: replayBtnEl.textContent,
    btnTitle: replayBtnEl.title,
    barVisibility: barEl.style.visibility,
    status: statusEl.textContent
};

/* 3. Step parity: `h` and `ArrowLeft` (same index), `l` and `ArrowRight`.
 *    The captured title documents the truthful `▶ Resume` tooltip: paused
 *    mid-line continues in place, paused at the end replays from the start. */
function stepProbe(key, from) {
    t.pauseReplay();
    t.replayJumpTo(from);
    const handled = press(t, key);
    return { handled: handled, snap: snapshot(t, b1), btnTitle: replayBtnEl.title };
}
const backH = stepProbe('h', 2);
const backArrow = stepProbe('ArrowLeft', 2);
const backVimAgain = (t.pauseReplay(), t.replayJumpTo(2), press(t, 'h'), snapshot(t, b1));
const fwdL = stepProbe('l', 0);
const fwdArrow = stepProbe('ArrowRight', 0);
// At the last step a forward step is a no-op (it can never leave the line).
t.pauseReplay();
t.replayJumpTo(2);
const fwdAtEnd = (press(t, 'l'), { snap: snapshot(t, b1), btnTitle: replayBtnEl.title });

/* 4. Jump keys: k / ArrowUp / Home → index 0; j / ArrowDown / End → live drill. */
function jumpProbe(key) {
    t.pauseReplay();
    t.replayJumpTo(2);
    const handled = press(t, key);
    return { handled: handled, snap: snapshot(t, b1) };
}
const jumpK = jumpProbe('k');
const jumpArrowUp = jumpProbe('ArrowUp');
const jumpHome = jumpProbe('Home');

function exitProbe(key) {
    t.startReplay();
    const handled = press(t, key);
    return { handled: handled, snap: snapshot(t, b1) };
}
const exitJ = exitProbe('j');
const exitArrowDown = exitProbe('ArrowDown');
const exitEnd = exitProbe('End');

/* 5. Space toggles the play state; a focused button wins over the shortcut. */
t.startReplay();
const spaceBefore = t.replayPlaying;
const handledSpacePause = press(t, ' ');
const spaceAfterPause = t.replayPlaying;
const handledSpaceResume = press(t, ' ');
const spaceAfterResume = t.replayPlaying;
const handledSpaceOnButton = press(t, ' ', { target: { tagName: 'BUTTON' } });
const spaceAfterButton = t.replayPlaying;

/* 6. Escape leaves the replay; with no replay on screen it is a no-op. */
const handledEsc = press(t, 'Escape');
const afterEsc = snapshot(t, b1);
const handledEscIdle = press(t, 'Escape');
const handledUnknown = press(t, 'x');

/* 7. Space with no replay active starts one. */
const handledSpaceStart = press(t, ' ');
const afterSpaceStart = snapshot(t, b1);
t.exitReplay();

/* 8. `_resetLineReplay()` is the hard reset used on every drill change. */
t.startReplay();
const timerBeforeReset = t.replayTimer !== null;
t._resetLineReplay();
const afterReset = {
    mode: t.replayMode,
    steps: t.lineSteps.length,
    index: t.lineIndex,
    interactive: b1.options.interactive,
    timerStopped: t.replayTimer === null,
    barVisibility: barEl.style.visibility
};

/* 9. "Always step": with NO replay active, `h` enters it one ply back;
 *    `l` stays a no-op because the board is already on the live position. */
const T2 = makeTrainer();
const t2 = T2.trainer;
const b2 = T2.board;
const forwardIdleHandled = press(t2, 'l');
const forwardIdle = snapshot(t2, b2);
// The two cosmetic drill timers must be cleared when the replay takes the board.
t2.mistakeResetTimeout = setTimeout(function () {}, 5000);
t2.solutionArrowFadeTimeout = setTimeout(function () {}, 5000);
const handledAlwaysBack = press(t2, 'h');
const alwaysStep = snapshot(t2, b2);
const cosmeticTimersCleared = (t2.mistakeResetTimeout === null && t2.solutionArrowFadeTimeout === null);

/* 10. `i` (hint) and `r` (Try Again) both return to the LIVE drill position. */
feedbackEl.style.display = 'block';
const handledHint = press(t2, 'i');
const afterHint = {
    handled: handledHint,
    snap: snapshot(t2, b2)
};
press(t2, 'h');
feedbackEl.style.display = 'block';
const handledRetry = press(t2, 'r');
const afterRetry = {
    handled: handledRetry,
    snap: snapshot(t2, b2),
    feedbackDisplay: feedbackEl.style.display
};

/* 11. A drill with no recorded path: the replay refuses and says why; a pending
 *     continuation (CODE-07 timer) also refuses, so a reply cannot land on a
 *     scrubbed board. */
const T3 = makeTrainer();
const t3 = T3.trainer;
const b3 = T3.board;
t3.currentDrill = makeDrill({ path_san: [] });
t3._loadLineSteps();
t3.startReplay();
const afterNoPath = {
    snap: snapshot(t3, b3),
    btnVisibility: replayBtnEl.style.visibility,
    toast: toasts.length ? toasts[toasts.length - 1] : null
};

t3.currentDrill = makeDrill();
t3._loadLineSteps();
t3.autoNextTimeout = setTimeout(function () { t3.autoNextTimeout = null; }, 5000);
const handledWhileContinuation = press(t3, 'h');
const whileContinuation = snapshot(t3, b3);
clearTimeout(t3.autoNextTimeout);
t3.autoNextTimeout = null;
const handledAfterContinuation = press(t3, 'h');
const afterContinuation = snapshot(t3, b3);

/* 12. Regression, end to end: a drill whose `parent_fen` comes from the BACKEND
 *     (python-chess, en-passant `-`) and whose last move was a double pawn push
 *     must still reveal the button and still step back with `h` — this is the
 *     exact shape of the drill payload that used to leave the feature dark. */
const T4 = makeTrainer();
const t4 = T4.trainer;
const b4 = T4.board;
t4.currentDrill = makeDrill({ parent_fen: BACKEND_DRILL_FEN });
t4.currentFen = BACKEND_DRILL_FEN;
t4._loadLineSteps();
const toastsBeforeCross = toasts.length;
const crossDrill = {
    snap: snapshot(t4, b4),
    btnVisibility: replayBtnEl.style.visibility,
    btnDisabled: replayBtnEl.disabled,
    barVisibility: barEl.style.visibility,
    status: statusEl.textContent
};
const handledCrossBack = press(t4, 'h');
const crossBack = {
    handled: handledCrossBack,
    snap: snapshot(t4, b4),
    newToasts: toasts.length - toastsBeforeCross
};
const handledCrossExit = press(t4, 'Escape');
const crossExit = { handled: handledCrossExit, snap: snapshot(t4, b4) };

/* `l` on that same drill (still no replay active) stays on the live position. */
const T5 = makeTrainer();
const t5 = T5.trainer;
const b5 = T5.board;
t5.currentDrill = makeDrill({ parent_fen: BACKEND_DRILL_FEN });
t5.currentFen = BACKEND_DRILL_FEN;
t5._loadLineSteps();
const handledCrossForward = press(t5, 'l');
const crossForward = { handled: handledCrossForward, snap: snapshot(t5, b5) };
t5._stopReplayTimer();

// Never leave a timer behind: a pending one would keep the node process alive.
t._stopReplayTimer();
t2._stopReplayTimer();
t3._stopReplayTimer();
t4._stopReplayTimer();

return {
    startFen: START_FEN,
    fenE4: FEN_E4,
    drillFen: DRILL_FEN,
    backendDrillFen: BACKEND_DRILL_FEN,
    afterLoad: afterLoad,
    afterStart: afterStart,
    backH: backH,
    backArrow: backArrow,
    backVimAgain: backVimAgain,
    fwdL: fwdL,
    fwdArrow: fwdArrow,
    fwdAtEnd: fwdAtEnd,
    jumpK: jumpK,
    jumpArrowUp: jumpArrowUp,
    jumpHome: jumpHome,
    exitJ: exitJ,
    exitArrowDown: exitArrowDown,
    exitEnd: exitEnd,
    space: {
        before: spaceBefore,
        handledPause: handledSpacePause,
        afterPause: spaceAfterPause,
        handledResume: handledSpaceResume,
        afterResume: spaceAfterResume,
        handledOnButton: handledSpaceOnButton,
        afterOnButton: spaceAfterButton
    },
    escape: { handled: handledEsc, snap: afterEsc, handledIdle: handledEscIdle },
    handledUnknown: handledUnknown,
    spaceStart: { handled: handledSpaceStart, snap: afterSpaceStart },
    reset: { timerBeforeReset: timerBeforeReset, after: afterReset },
    forwardIdle: { handled: forwardIdleHandled, snap: forwardIdle },
    alwaysStep: { handled: handledAlwaysBack, snap: alwaysStep, cosmeticTimersCleared: cosmeticTimersCleared },
    hint: afterHint,
    retry: afterRetry,
    noPath: afterNoPath,
    continuation: {
        handledBefore: handledWhileContinuation,
        snapWhile: whileContinuation,
        handledAfter: handledAfterContinuation,
        snapAfter: afterContinuation
    },
    crossDrill: crossDrill,
    crossBack: crossBack,
    crossExit: crossExit,
    crossForward: crossForward,
    toasts: toasts
};
"""


class _NodeScenarioMixin:
    """Shared plumbing: run one real-frontend scenario, skip only when node is absent.

    A plain mixin (not a `TestCase`) so `unittest discover` does not collect it on
    its own — it has no scenario to run.
    """

    SCENARIO = None

    @classmethod
    def setUpClass(cls):
        import tests.frontend_runtime as frontend_runtime
        cls.frontend_runtime = frontend_runtime
        if not frontend_runtime.node_available():
            raise unittest.SkipTest("node not installed; the real frontend cannot be executed")
        missing = frontend_runtime.missing_frontend_sources()
        if missing:
            # A missing or emptied frontend file is a FAILURE, never a skip.
            raise AssertionError(
                "Real frontend sources missing or empty, refusing to test a mock: %s" % (missing,))
        cls.result = frontend_runtime.run_scenario(cls.SCENARIO)

    def test_the_harness_executed_the_shipped_trainer(self):
        provenance = self.frontend_runtime.harness_provenance(self.result)
        self.assertIn("frontend/repertoire_trainer.js", provenance)
        self.assertEqual(
            provenance["frontend/repertoire_trainer.js"],
            self.frontend_runtime.frontend_sha256("frontend/repertoire_trainer.js"),
            "the harness did not execute the shipped repertoire_trainer.js")


class TestReplayTimeline(_NodeScenarioMixin, unittest.TestCase):
    """`_buildLineSteps()` / `_loadLineSteps()` against the real trainer."""

    SCENARIO = _BUILD_LINE_STEPS_SCENARIO

    # (timeline key, parent_fen key) — python-chess writes `ep = '-'` on all of them
    # because no legal en-passant capture exists in the resulting position.
    CROSS_ENGINE_CASES = (
        ("crossE4E5", "backendE4E5"),
        ("crossE4C5", "backendE4C5"),
        ("crossNf3D5", "backendNf3D5"),
        ("crossSpanish", "backendSpanish"),
    )

    def test_a_valid_path_becomes_board_steps_and_lands_on_the_drill_position(self):
        valid = self.result["valid"]
        self.assertIsNotNone(valid, "_buildLineSteps() rejected a valid path_san")
        self.assertEqual(valid["len"], 2)
        self.assertEqual(valid["first"]["fen"], self.result["fenStart"],
                         "step 0 must be the standard start position")
        self.assertEqual(valid["last"]["from"], "e2")
        self.assertEqual(valid["last"]["to"], "e4")
        self.assertEqual(valid["last"]["san"], "e4")
        self.assertEqual(valid["fen3Last"], fen3(self.result["fenE4"]))
        self.assertEqual(valid["last"]["fen"], self.result["fenE4"],
                         "the last step must be snapped to the drill's own FEN")

    def test_the_move_history_sans_fallback_covers_deviation_drills(self):
        fallback = self.result["fallback"]
        self.assertIsNotNone(fallback, "the move_history_sans alias was ignored")
        self.assertEqual(fallback["len"], 3)
        self.assertEqual(fallback["sans"], [None, "e4", "e5"])
        self.assertEqual(fallback["fen3Last"], fen3(self.result["fenE4E5"]))

    def test_a_longer_line_is_rebuilt_in_order(self):
        longer = self.result["longer"]
        self.assertIsNotNone(longer)
        self.assertEqual(longer["len"], 4)
        self.assertEqual(longer["sans"], [None, "e4", "e5", "Nf3"])
        self.assertEqual(longer["fen3Last"], fen3(self.result["fenE4E5Nf3"]))

    # ---- the en-passant bug (cross-engine comparison) ---------------- #

    def test_the_validation_key_ignores_the_en_passant_token(self):
        """`_fen3()` must compare placement + turn + castling only.

        `parent_fen` is a python-chess FEN and the rebuilt steps are chess.js FENs;
        after 1.e4 e5 the two disagree on the en-passant token (`-` vs `e6`), so a
        4-token comparison discards every path ending in a double pawn push.
        """
        keys = self.result["keys"]
        self.assertEqual(keys["fen3Chess"], keys["fen3Backend"],
                         "_fen3() must ignore the en-passant token (chess.js e6 vs python-chess -)")
        self.assertEqual(keys["fen3Backend"], fen3(self.result["backendE4E5"]),
                         "the shipped _fen3() disagrees with the 3-token key definition")
        self.assertEqual(len(keys["fen3Chess"].split()), 3,
                         "the key must be exactly 3 tokens (no en-passant, no counters)")
        # The DB key (AGENTS.md §4, `normalize_fen`) DOES see the difference — this is
        # the comparison that went dark, and the shipped trainer no longer offers such
        # a helper at all (`_fen3()` is the only FEN key in it).
        self.assertNotEqual(db_style_fen_key(self.result["backendChessJsE4E5"]),
                            db_style_fen_key(self.result["backendE4E5"]),
                            "the two engines' FENs must differ on the 4-token key, or this "
                            "regression test is no longer reproducing the bug")
        # The literals really are this position, one en-passant token apart.
        self.assertEqual(backend_style_fen(self.result["backendChessJsE4E5"]), self.result["backendE4E5"])
        self.assertEqual(
            db_style_fen_key(self.result["backendChessJsE4E5"]),
            "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6",
            "chess.js must write the en-passant square after 1.e4 e5")

    def test_a_path_ending_in_a_double_pawn_push_is_accepted_across_engines(self):
        """Regression: python-chess `parent_fen` (`ep = '-'`) vs a chess.js path."""
        for case_key, fen_key in self.CROSS_ENGINE_CASES:
            case = self.result[case_key]
            parent = self.result[fen_key]
            self.assertEqual(parent.split()[3], "-", "%s: the fixture must be a python-chess FEN" % fen_key)
            self.assertIsNotNone(
                case,
                "%s: the timeline was discarded over the en-passant token "
                "(3 tokens must match, 4 do not)" % case_key)
            self.assertEqual(case["fen3Last"], fen3(parent),
                             "%s: the rebuilt path must land on the drill position" % case_key)
            self.assertEqual(case["last"]["fen"], parent,
                             "%s: the last step must be snapped to the drill's own FEN" % case_key)

        # The same path against a chess.js-written parent_fen (the shape the first
        # version of these tests used) must keep working.
        chess_js_case = self.result["crossNf3D5ChessJs"]
        self.assertIsNotNone(chess_js_case)
        self.assertEqual(chess_js_case["fen3Last"], fen3(self.result["fenNf3D5"]))
        self.assertEqual(chess_js_case["last"]["fen"], self.result["fenNf3D5"])

    def test_the_spanish_case_is_the_cross_engine_probe_line(self):
        """The long case is the planner's probe: 12 plies, last move ...b5 (double push)."""
        self.assertEqual(
            self.result["chessJsSpanish"],
            "r1bqk2r/2ppbppp/p1n2n2/1p2p3/B3P3/5N2/PPPP1PPP/RNBQR1K1 w kq b6 0 7",
            "the chess.js FEN of the probe line changed; re-derive BACKEND_SPANISH from it")
        self.assertNotEqual(self.result["chessJsSpanish"], self.result["backendSpanish"])
        self.assertEqual(backend_style_fen(self.result["chessJsSpanish"]), self.result["backendSpanish"])

    # ---- refusals ---------------------------------------------------- #

    def test_a_path_that_does_not_land_on_parent_fen_is_refused(self):
        self.assertIsNone(self.result["mismatch"],
                          "a mismatching parent_fen must yield no timeline, not a wrong one")
        self.assertIsNone(self.result["mismatchOpening"],
                          "a different opening's position must be refused too (the 3-token key "
                          "is not a rubber stamp)")

    def test_an_illegal_san_is_refused(self):
        self.assertIsNone(self.result["illegal"])

    def test_no_replayable_path_is_refused(self):
        for key in ("emptyPath", "noPath", "noDrill"):
            self.assertIsNone(self.result[key], "%s must not produce a timeline" % key)

    def test_load_line_steps_puts_the_marker_on_the_drill_position(self):
        self.assertEqual(self.result["loaded"], {"steps": 3, "index": 2})

    def test_load_line_steps_never_rebuilds_mid_replay(self):
        self.assertEqual(self.result["lockedMidReplay"], {"steps": 3, "index": 2},
                         "the timeline was rebuilt while a replay was on screen")


class TestReplayKeysAndBoard(_NodeScenarioMixin, unittest.TestCase):
    """The real mapping (arrows + vim keys) driving a recording stub board."""

    SCENARIO = _REPLAY_KEYS_SCENARIO

    # ---- the timeline on screen ------------------------------------- #

    def test_a_drill_with_a_path_reveals_the_replay_button(self):
        after = self.result["afterLoad"]
        self.assertEqual(after["snap"]["steps"], 3)
        self.assertEqual(after["snap"]["index"], 2, "the timeline starts on the drill position")
        self.assertFalse(after["snap"]["mode"])
        self.assertEqual(after["btnVisibility"], "visible")
        self.assertFalse(after["btnDisabled"])
        self.assertEqual(after["barVisibility"], "hidden")
        self.assertEqual(after["status"], "Paused 2 / 2")

    def test_start_replay_plays_from_the_start_and_locks_the_board(self):
        after = self.result["afterStart"]
        self.assertEqual(after["snap"]["index"], 0)
        self.assertTrue(after["snap"]["playing"])
        self.assertTrue(after["snap"]["mode"])
        self.assertFalse(after["snap"]["interactive"],
                         "the board must not accept moves while the replay owns it")
        self.assertEqual(after["loads"], [self.result["startFen"]],
                         "the first board load of a replay is the start of the line")
        self.assertEqual(after["barVisibility"], "visible")
        self.assertEqual(after["btnLabel"], "⏸ Pause")
        self.assertEqual(after["btnTitle"], "Pause the line replay (Space)")
        self.assertEqual(after["status"], "Playing 0 / 2")

    # ---- h/l parity, executed --------------------------------------- #

    def test_h_and_arrow_left_make_the_same_step(self):
        back_h = self.result["backH"]
        back_arrow = self.result["backArrow"]
        self.assertTrue(back_h["handled"])
        self.assertTrue(back_arrow["handled"])
        self.assertEqual(back_h["snap"]["index"], 1)
        self.assertEqual(back_arrow["snap"]["index"], back_h["snap"]["index"])
        self.assertEqual(back_arrow["snap"]["fen"], back_h["snap"]["fen"])
        self.assertEqual(back_arrow["snap"], back_h["snap"],
                         "`h` and `ArrowLeft` must produce the very same board state")
        self.assertEqual(self.result["backVimAgain"]["index"], 1)

    def test_l_and_arrow_right_make_the_same_step(self):
        fwd_l = self.result["fwdL"]
        fwd_arrow = self.result["fwdArrow"]
        self.assertTrue(fwd_l["handled"])
        self.assertTrue(fwd_arrow["handled"])
        self.assertEqual(fwd_l["snap"]["index"], 1)
        self.assertEqual(fwd_arrow["snap"], fwd_l["snap"],
                         "`l` and `ArrowRight` must produce the very same board state")
        # Paused mid-line, "resume" continues in place — the title must say so.
        self.assertEqual(fwd_l["btnTitle"], "Resume the line replay (Space)")

    def test_a_step_pauses_the_automatic_play(self):
        # `stepProbe` pauses first, so the step itself must leave playing false.
        self.assertFalse(self.result["fwdL"]["snap"]["playing"])
        self.assertTrue(self.result["fwdL"]["snap"]["mode"], "stepping must not leave replay mode")

    def test_a_forward_step_at_the_last_step_is_a_no_op(self):
        self.assertEqual(self.result["fwdAtEnd"]["snap"]["index"], 2)
        # Paused at the last step, resuming replays the line from the start.
        self.assertEqual(self.result["fwdAtEnd"]["btnTitle"],
                         "Replay the line again from the start (Space)")

    # ---- jump keys --------------------------------------------------- #

    def test_k_arrow_up_and_home_jump_to_the_start(self):
        for key in ("jumpK", "jumpArrowUp", "jumpHome"):
            probe = self.result[key]
            self.assertTrue(probe["handled"], key)
            self.assertEqual(probe["snap"]["index"], 0, key)
            self.assertEqual(probe["snap"]["fen"], self.result["startFen"], key)

    def test_j_arrow_down_and_end_return_to_the_live_drill_position(self):
        for key in ("exitJ", "exitArrowDown", "exitEnd"):
            probe = self.result[key]
            self.assertTrue(probe["handled"], key)
            snap = probe["snap"]
            self.assertFalse(snap["mode"], "%s must leave the replay" % key)
            self.assertEqual(snap["fen"], self.result["drillFen"], key)
            self.assertTrue(snap["interactive"], "%s must hand the board back" % key)
            self.assertTrue(snap["waiting"], "%s must restore the drill's wait state" % key)

    # ---- Space / Escape --------------------------------------------- #

    def test_space_toggles_pause_and_resume(self):
        space = self.result["space"]
        self.assertTrue(space["before"])
        self.assertTrue(space["handledPause"])
        self.assertFalse(space["afterPause"], "the first Space must pause")
        self.assertTrue(space["handledResume"])
        self.assertTrue(space["afterResume"], "the second Space must resume")

    def test_space_defers_to_a_focused_button(self):
        space = self.result["space"]
        self.assertFalse(space["handledOnButton"])
        self.assertTrue(space["afterOnButton"], "a focused button must win over the shortcut")

    def test_space_starts_a_replay_when_none_is_running(self):
        self.assertTrue(self.result["spaceStart"]["handled"])
        self.assertTrue(self.result["spaceStart"]["snap"]["mode"])
        self.assertTrue(self.result["spaceStart"]["snap"]["playing"])

    def test_escape_leaves_the_replay_and_restores_the_board(self):
        escape = self.result["escape"]
        self.assertTrue(escape["handled"])
        self.assertFalse(escape["snap"]["mode"])
        self.assertEqual(escape["snap"]["fen"], self.result["drillFen"])
        self.assertTrue(escape["snap"]["interactive"])
        self.assertTrue(escape["snap"]["waiting"])
        self.assertFalse(escape["handledIdle"], "Escape with no replay active must be a no-op")

    def test_unknown_keys_are_not_swallowed(self):
        self.assertFalse(self.result["handledUnknown"])

    # ---- "always step" (no replay active) --------------------------- #

    def test_h_always_steps_back_even_without_a_replay(self):
        always = self.result["alwaysStep"]
        self.assertTrue(always["handled"])
        snap = always["snap"]
        self.assertTrue(snap["mode"], "`h` with no replay active must enter replay mode")
        self.assertEqual(snap["index"], snap["steps"] - 2, "…and land one ply back")
        self.assertFalse(snap["playing"], "the entry is paused, not playing")
        self.assertFalse(snap["interactive"])
        self.assertTrue(always["cosmeticTimersCleared"],
                        "entering the replay must clear the cosmetic drill timers")

    def test_l_without_a_replay_is_a_no_op(self):
        idle = self.result["forwardIdle"]
        self.assertFalse(idle["snap"]["mode"], "the board is already on the live position")
        self.assertEqual(idle["snap"]["index"], idle["snap"]["steps"] - 1)

    def test_hint_and_retry_both_act_on_the_live_position(self):
        hint = self.result["hint"]
        self.assertFalse(hint["snap"]["mode"], "`i` must leave the replay")
        self.assertEqual(hint["snap"]["fen"], self.result["drillFen"])
        self.assertTrue(hint["snap"]["interactive"])

        retry = self.result["retry"]
        self.assertFalse(retry["snap"]["mode"], "`r` must leave the replay")
        self.assertEqual(retry["snap"]["fen"], self.result["drillFen"])
        self.assertTrue(retry["snap"]["interactive"])
        self.assertEqual(retry["feedbackDisplay"], "none",
                         "Try Again must clear the feedback box (its existing contract)")

    # ---- hard reset + refusal paths --------------------------------- #

    def test_reset_line_replay_is_a_hard_reset(self):
        reset = self.result["reset"]
        self.assertTrue(reset["timerBeforeReset"], "a playing replay owns a timer")
        after = reset["after"]
        self.assertFalse(after["mode"])
        self.assertEqual(after["steps"], 0)
        self.assertEqual(after["index"], 0)
        self.assertTrue(after["interactive"], "the board must be usable again")
        self.assertTrue(after["timerStopped"], "the reset must stop the pending tick")
        self.assertEqual(after["barVisibility"], "hidden")

    def test_a_drill_without_a_path_says_so_and_reveals_nothing(self):
        no_path = self.result["noPath"]
        self.assertFalse(no_path["snap"]["mode"])
        self.assertEqual(no_path["snap"]["steps"], 0)
        self.assertEqual(no_path["btnVisibility"], "hidden")
        self.assertIn("No recorded move path", no_path["toast"] or "")

    def test_a_pending_continuation_refuses_to_enter_replay_mode(self):
        cont = self.result["continuation"]
        before = cont["snapWhile"]
        self.assertFalse(before["mode"],
                         "a queued opponent reply / auto-next must refuse the replay")
        self.assertEqual(before["index"], before["steps"] - 1,
                         "a refused entry must not scrub the board")
        after = cont["snapAfter"]
        self.assertTrue(cont["handledAfter"], "once the continuation is gone the replay works")
        self.assertTrue(after["mode"])
        self.assertEqual(after["index"], after["steps"] - 2)

    # ---- the en-passant bug, end to end ------------------------------ #

    def test_a_backend_parent_fen_after_a_double_pawn_push_still_replays(self):
        """The visible half of the bug: the button must appear and `h` must step back.

        The drill here is the shipped payload for 1.e4 e5: `parent_fen` written by
        python-chess (`ep = '-'`) against a path rebuilt with chess.js (`ep = e6`).
        Before the fix the timeline was discarded, the button stayed `hidden` and
        `h` answered "No recorded move path for this position".
        """
        cross = self.result["crossDrill"]
        self.assertEqual(cross["snap"]["steps"], 3,
                         "the timeline was discarded over the en-passant token")
        self.assertEqual(cross["snap"]["index"], 2)
        self.assertEqual(cross["btnVisibility"], "visible", "the Replay button must be revealed")
        self.assertFalse(cross["btnDisabled"])
        self.assertEqual(cross["barVisibility"], "hidden")
        self.assertEqual(cross["status"], "Paused 2 / 2")

        back = self.result["crossBack"]
        self.assertTrue(back["handled"])
        self.assertEqual(back["newToasts"], 0,
                         "`h` must not answer 'No recorded move path for this position'")
        self.assertEqual(back["snap"]["index"], 1, "`h` must step one ply back")
        self.assertTrue(back["snap"]["mode"])
        self.assertFalse(back["snap"]["playing"])
        self.assertEqual(back["snap"]["fen"], self.result["fenE4"],
                         "one ply back from 1.e4 e5 is the position after 1.e4")
        self.assertFalse(back["snap"]["interactive"])

        # Escape hands the board back on the backend's own FEN.
        self.assertTrue(self.result["crossExit"]["handled"])
        self.assertEqual(self.result["crossExit"]["snap"]["fen"], self.result["backendDrillFen"])
        self.assertTrue(self.result["crossExit"]["snap"]["interactive"])

        # `l` with no replay active stays on the live position (nothing to go forward to).
        self.assertFalse(self.result["crossForward"]["snap"]["mode"])
        self.assertEqual(self.result["crossForward"]["snap"]["index"], 2)


if __name__ == "__main__":
    unittest.main()
