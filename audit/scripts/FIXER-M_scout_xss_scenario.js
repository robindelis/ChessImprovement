/*
 * audit/scripts/FIXER-M_scout_xss_scenario.js
 *
 * Runtime verification of the SEC-M audit of `frontend/scout.js`, executed against the
 * REAL shipped sources through `tests/frontend_harness.js` (which loads every local
 * `<script src>` of `index.html` in document order: board.js, repertoire_builder.js,
 * repertoire_trainer.js, scout.js, app.js, audio.js).
 *
 * Run:  node tests/frontend_harness.js audit/scripts/FIXER-M_scout_xss_scenario.js
 * Output: JSON on stdout; `{ "allPass": true }` means every assertion held.
 *
 * Only PUBLIC entry points are driven (`renderResults`, `renderTreeLines`,
 * `checkActiveLineOverlap`, `checkTreeOverlap`, `_applyTreeBadges`,
 * `importToRepertoire`, `_submitLineImport`, and the builder's `importToRepertoire` /
 * `importAllLinesToRepertoire`), so the same file can be pointed at a pre-fix copy.
 *
 * Two things this scenario adds over the SEC-J one:
 *   1. `makeParsing()` gives `#scoutVariationsList` / `#scoutResultsGrid` an innerHTML
 *      setter that *parses* the markup into real shim elements, so the renderers' own
 *      `querySelectorAll(...)` wiring finds the buttons they emitted and a REAL
 *      dispatched click reaches the real handler. (The stock shim keeps the string and
 *      drops the children, which is what made click assertions impossible before.)
 *   2. The import-status contract (success / already_present / error) and the
 *      `inserted_count` / `errors` reporting are exercised end to end.
 */
'use strict';

const results = [];
function t(name, cond, detail) {
    results.push({ name: name, pass: !!cond, detail: cond ? undefined : String(detail) });
}

// `tests/frontend_harness.js` invokes the scenario as `(function (api) { … })({sandbox,
// Chess, InteractiveChessboard, window, record})` inside the same vm context, so
// `document` / `window` are already ambient globals and only `sandbox` needs aliasing.
const sandbox = api.sandbox;

const b = window.repertoireBuilder;

// ---------------------------------------------------------------------------
// Inject every #id the code under test touches (the harness only pre-seeds
// #repertoireBoard). A missing node would make a renderer a silent no-op.
// ---------------------------------------------------------------------------
const IDS = [
    'scoutBoard', 'scoutVariationsList', 'scoutActiveLineOverlap', 'scoutResultsGrid',
    'scoutTreeCountBadge', 'scoutQuickLineCount', 'scoutActiveLinePlyCount',
    'scoutLoadingIndicator', 'scoutLoadingText', 'scoutCurrentOpeningTag',
    'scoutTurnBadge', 'scoutCurrentMoveNum', 'scoutCurrentMoveSan',
    'scoutNoteMoveLabel', 'scoutMoveComment', 'scoutEvalBarFill', 'scoutEvalBarLabel',
    'scoutCommentSaveStatus', 'scoutStudySelect', 'scoutYoutubePlayer',
    'scoutNonVideoPanel', 'scoutNonVideoPlaceholder', 'scoutChapterSelect',
    'scoutStudio', 'scoutResultsSection', 'scoutStudioTitle', 'scoutStudioChannel',
    'scoutStudioDuration', 'scoutStudioEco', 'scoutStudioOpeningName',
    'btnScoutMarkImportant', 'btnScoutRemoveFromRep', 'btnScoutTrainAfterImport',
    'btnScoutOpenYoutube', 'btnScoutFav', 'viewScout', 'repertoireBoard',
    'trainNoteBox', 'trainNoteContent', 'trainFocusBanner', 'trainFocusName',
    'trainImportantBadge', 'btnTrainToggleArrows'
];
IDS.forEach(function (id) {
    if (!document.getElementById(id)) document.byId.set(id, document.createElement('div'));
});
function q(id) {
    const el = document.getElementById(id);
    if (!el) throw new Error('scenario setup: #' + id + ' was not injected');
    return el;
}

// ---------------------------------------------------------------------------
// Markup scanner. Only pieces of a hostile value that can *execute* matter: extra
// elements, `on*` handlers, script-bearing URLs, CSS `expression()`. `<button>`,
// `<a>`, `<div>`, `<span>`, `<img>`-as-declared are legitimate; `<img>` is listed
// because a *second* one would mean the thumbnail allowlist failed, and the benign
// thumbnails are checked separately for their exact count.
// ---------------------------------------------------------------------------
const DANGEROUS_TAGS = [
    'script', 'svg', 'math', 'iframe', 'object', 'embed', 'applet', 'form',
    'input', 'style', 'link', 'meta', 'base', 'video', 'audio', 'source',
    'template', 'noscript', 'frame', 'frameset', 'portal', 'marquee', 'details',
    'dialog', 'select', 'option', 'body', 'html', 'xmp', 'plaintext', 'listing'
];

function scanMarkup(html) {
    const findings = [];
    const tagRe = /<\s*(\/?)\s*([a-zA-Z][a-zA-Z0-9:-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)\/?>/g;
    let m;
    while ((m = tagRe.exec(html)) !== null) {
        const name = m[2].toLowerCase();
        if (DANGEROUS_TAGS.indexOf(name) !== -1) findings.push('tag:' + name);
        const attrs = m[3] || '';
        const attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*(?:=\s*("[^"]*"|'[^']*'|[^\s"'>]+))?/g;
        let a;
        while ((a = attrRe.exec(attrs)) !== null) {
            const an = a[1].toLowerCase();
            const av = (a[2] || '').replace(/^["']|["']$/g, '');
            if (/^on[a-z]+$/.test(an)) findings.push('handler:' + an);
            if (/javascript\s*:/i.test(av)) findings.push('url:' + an);
            if (/expression\s*\(/i.test(av)) findings.push('css:' + an);
        }
    }
    return findings;
}

function assertInert(label, html) {
    const findings = scanMarkup(html || '');
    t('SEC-M/' + label + ': produces no executable node', findings.length === 0,
        JSON.stringify(findings) + ' in ' + String(html).slice(0, 600));
    return findings;
}

/** Escaped exactly once: escaped form present, double-escaped form absent. */
function assertEscapedOnce(label, html, needle) {
    const n = needle || '&lt;img';
    t('SEC-M/' + label + ': the payload is escaped exactly once',
        html.indexOf(n) !== -1 && html.indexOf('&amp;lt;img') === -1,
        String(html).slice(0, 600));
}

const EVIL = '<img src=x onerror=alert(1)>';
const EVIL_ATTR = '" onmouseover="alert(2)';
const EVIL_URL = 'javascript:alert(3)';
const EVIL_SAN = 'e4" onmouseover="alert(4)';

// ---------------------------------------------------------------------------
// A minimal HTML -> shim-element parser, installed as an innerHTML setter on the two
// containers whose real wiring we want to exercise with a real click.
// ---------------------------------------------------------------------------
function parseInto(parent, html) {
    const stack = [parent];
    const tagRe = /<\s*(\/?)\s*([a-zA-Z][a-zA-Z0-9:-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)\/?>|([^<]+)/g;
    let m;
    while ((m = tagRe.exec(html)) !== null) {
        const top = stack[stack.length - 1];
        if (m[4] !== undefined) {                       // text node
            const txt = m[4].replace(/\s+/g, ' ');
            if (txt.trim()) {
                const tn = document.createTextNode(txt);
                top.appendChild(tn);
            }
            continue;
        }
        const closing = m[1] === '/';
        const name = m[2].toLowerCase();
        if (closing) {
            for (let i = stack.length - 1; i > 0; i--) {
                if (stack[i].tagName.toLowerCase() === name) { stack.length = i; break; }
            }
            continue;
        }
        const el = document.createElement(name);
        const attrs = m[3] || '';
        const attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*(?:=\s*("[^"]*"|'[^']*'|[^\s"'>]+))?/g;
        let a;
        while ((a = attrRe.exec(attrs)) !== null) {
            const an = a[1].toLowerCase();
            const av = (a[2] === undefined ? '' : (a[2] || '').replace(/^["']|["']$/g, ''));
            el.setAttribute(an, av);
            if (an === 'class') el.className = av;
            if (an === 'title') el.title = av;
            if (an.indexOf('data-') === 0) {
                el.dataset[an.slice(5).replace(/-([a-z])/g, (mm, c) => c.toUpperCase())] = av;
            }
        }
        top.appendChild(el);
        if (!/^(img|br|hr|input|meta|link)$/.test(name)) stack.push(el);
    }
}

function makeParsing(el) {
    Object.defineProperty(el, 'innerHTML', {
        configurable: true,
        get: function () { return this._innerHTML; },
        set: function (v) {
            this._innerHTML = v === null || v === undefined ? '' : String(v);
            this.children.forEach(function (c) { c.parentElement = null; });
            this.children = [];
            parseInto(this, this._innerHTML);
        }
    });
    return el;
}

const listEl = makeParsing(q('scoutVariationsList'));
const gridEl = makeParsing(q('scoutResultsGrid'));

// ---------------------------------------------------------------------------
// Fetch stub: scripted per-endpoint, sequential for the white/black overlap pair.
// ---------------------------------------------------------------------------
function makeFetch(routes, log) {
    return function (url, opts) {
        const u = String(url);
        if (log) log.push(u);
        let key = null;
        for (const k of Object.keys(routes)) if (u.indexOf(k) !== -1) { key = k; break; }
        if (!key) {
            return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({}); } });
        }
        const entry = routes[key];
        const body = (entry && entry.queued) ? entry.queue.shift() : entry;
        return Promise.resolve({
            ok: body.ok !== false,
            status: body.status || 200,
            json: function () {
                return body.throwOnJson
                    ? Promise.reject(new Error('bad json'))
                    : Promise.resolve(body.body === undefined ? {} : body.body);
            }
        });
    };
}

// ---------------------------------------------------------------------------
// OpeningScout instance, built the way the app builds it (the constructor's network
// calls resolve to the stub, which answers `{}`).
// ---------------------------------------------------------------------------
const toasts = [];
window.showToast = function (msg, color, duration) { toasts.push({ msg: msg, color: color }); };
window.repertoireBuilder = b;
// `_submitLineImport()` calls `window.repertoireBuilder.loadRepertoire()` WITHOUT awaiting
// it, so the real one would land on a later microtask: `loadRepertoire -> updatePosition
// -> syncLineTreeNode` resets the builder tree's `currentNode` to the root and every
// later import then reports "No moves played to import yet!". Stubbed here (before the
// scout import checks) so the tree this file seeds is the tree the imports see.
b.loadRepertoire = function () { return Promise.resolve(); };

const routeLog = [];
sandbox.fetch = makeFetch({}, routeLog);

// A classic-script top-level `class` is a *lexical* binding, not a `window` property
// (the same trap DUP-01 documents), so the bare identifier is the only way in.
const s = new OpeningScout();
window.videoScout = s;

function hostileLine() {
    s.lineTree.reset();
    s.lineTree.addMove({
        fen_before: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -',
        fen_after: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -',
        uci: 'e2e4', san: EVIL_SAN, color: 'white', move_number: 1,
        opening_name: EVIL, eco: EVIL
    });
    s.lineTree.addMove({
        fen_before: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -',
        fen_after: 'rnbqkbnr/pppppp1p/8/8/4p3/8/PPPP1PPP/RNBQKBNR w KQkq -',
        uci: 'e7e5', san: 'e5', color: 'black', move_number: 1,
        opening_name: 'Sicilian', eco: 'B20'
    });
    return s.lineTree.getAllLines()[0];
}

const overlapWhite = {
    status: 'partially_present', matched_plies: 1, total_plies: 2, new_plies: 1,
    message: EVIL + EVIL_ATTR, divergence_info: null
};
const overlapBlack = {
    status: 'already_present', matched_plies: 2, total_plies: 2, new_plies: 0,
    message: EVIL
};

const run = async () => {
    // =======================================================================
    // 1. renderResults()  —  the YouTube result card (SEC-01 surface)
    // =======================================================================
    const goodItem = {
        source_type: 'youtube', id: 'dQw4w9WgXcQ', title: 'Caro-Kann for Beginners',
        channel: 'Chess Guide', duration_formatted: '12:00',
        thumbnail: 'https://i.ytimg.com/vi/x/hq.jpg', eco: 'B12', opening_name: 'Caro-Kann'
    };
    const evilItem = Object.assign({}, goodItem, {
        id: EVIL, title: EVIL, channel: EVIL, duration_formatted: EVIL,
        eco: EVIL, thumbnail: EVIL_URL, source_type: EVIL
    });

    s.activeFilter = 'all';
    s.cachedResults = [goodItem];
    s.renderResults();
    const benignGrid = gridEl.innerHTML;
    assertInert('search card (benign)', benignGrid);
    t('SEC-M/search card: the benign card is unchanged',
        /class="card-panel scout-card"/.test(benignGrid) &&
        benignGrid.indexOf('Caro-Kann for Beginners') !== -1 &&
        benignGrid.indexOf('Chess Guide') !== -1 &&
        /class="scout-badge-youtube">YouTube</.test(benignGrid) &&
        /class="badge-eco">B12</.test(benignGrid) &&
        /class="scout-card-title" title="Caro-Kann for Beginners"/.test(benignGrid) &&
        /src="https:\/\/i\.ytimg\.com\/vi\/x\/hq\.jpg"/.test(benignGrid) &&
        (benignGrid.match(/<img /g) || []).length === 1,
        benignGrid);

    s.cachedResults = [goodItem, evilItem];
    s.renderResults();
    const evilGrid = gridEl.innerHTML;
    assertInert('search card (hostile)', evilGrid);
    assertEscapedOnce('search card', evilGrid);
    t('SEC-M/search card: a javascript: thumbnail is neutralised',
        evilGrid.indexOf('javascript:') === -1 &&
        evilGrid.indexOf('/assets/pieces/wN.svg') !== -1,
        evilGrid.slice(0, 600));
    t('SEC-M/search card: no inline onerror handler is emitted',
        !/\sonerror\s*=/.test(evilGrid) && evilGrid.indexOf('data-fallback-src') !== -1,
        evilGrid.slice(0, 400));
    t('SEC-M/search card: the click target keeps its data-source-id',
        /class="card-panel scout-card" data-source-id="dQw4w9WgXcQ"/.test(evilGrid),
        evilGrid.slice(0, 400));

    // A REAL dispatched click through the real wiring (only possible because
    // `#scoutResultsGrid` parses its own innerHTML here).
    let analyzed = null;
    s.analyzeSource = function (src) { analyzed = src; return Promise.resolve(); };
    const card = gridEl.querySelector('.scout-card');
    t('SEC-M/search card: the renderer\'s own querySelector(\'.scout-card\') finds a card',
        !!card, gridEl.children.length);
    if (card) {
        card.dispatchEvent({ type: 'click', target: card, preventDefault() { } });
        t('SEC-M/search card: clicking a card still calls analyzeSource with the video URL',
            analyzed === 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', String(analyzed));
    }

    // =======================================================================
    // 2. renderTreeLines()  —  the line card (SEC-02 surface)
    // =======================================================================
    const line = hostileLine();
    line.name = EVIL;
    line.eco = EVIL;
    line.studyName = EVIL;
    s.currentStudy = EVIL;
    s.treeOverlapMap = {
        white: { [line.lineId]: overlapWhite },
        black: { [line.lineId]: overlapBlack }
    };
    s.renderTreeLines();
    const card0 = listEl.innerHTML;
    assertInert('line card (hostile)', card0);
    assertEscapedOnce('line card', card0);
    t('SEC-M/line card: the §4E badge wording is present verbatim',
        /⚡ White: 1\/2 in Rep \(\+1 new\)/.test(card0) &&
        /✓ In Black/.test(card0),
        card0.slice(0, 900));
    t('SEC-M/line card: every click-target class and its data-idx are still emitted',
        ['btn-load-tree-line', 'btn-import-tree-white', 'btn-import-tree-black',
            'btn-del-tree-white', 'btn-del-tree-black', 'btn-del-session-line',
            'btn-assign-line-study'].every(function (c) {
            return new RegExp('class="btn-var-action ' + c + '" data-idx="0"').test(card0);
        }),
        card0.slice(0, 900));
    t('SEC-M/line card: the overlap key rides on data-line-id (CODE-06)',
        new RegExp('data-line-id="' + line.lineId + '"').test(card0), card0.slice(0, 400));
    t('SEC-M/line card: the ply count is rendered',
        /class="scout-var-plies">2 plies<\/span>/.test(card0), card0.slice(0, 600));

    // A REAL dispatched click through the real wiring.
    let loadedLine = null;
    s.loadTreeLine = function (l) { loadedLine = l; };
    const loadBtn = listEl.querySelector('.btn-load-tree-line');
    t('SEC-M/line card: the renderer\'s own querySelector(\'.btn-load-tree-line\') finds the button',
        !!loadBtn, listEl.children.length);
    if (loadBtn) {
        loadBtn.dispatchEvent({ type: 'click', target: loadBtn, preventDefault() { }, stopPropagation() { } });
        t('SEC-M/line card: clicking "Load" still calls loadTreeLine with that line',
            !!loadedLine && loadedLine.plyCount === 2, String(loadedLine && loadedLine.plyCount));
    }
    let importedFor = null;
    s.importToRepertoire = function (rep, moves) { importedFor = rep + ':' + moves.length; return Promise.resolve(); };
    const impBtn = listEl.querySelector('.btn-import-tree-white');
    if (impBtn) {
        impBtn.dispatchEvent({ type: 'click', target: impBtn, preventDefault() { }, stopPropagation() { } });
        t('SEC-M/line card: clicking "Import -> White" still calls importToRepertoire',
            importedFor === 'white:2', String(importedFor));
    }

    // Benign line card, byte-for-byte expectations (regression guard).
    const goodLine = (function () {
        const t2 = s.lineTree;
        t2.reset();
        t2.addMove({ fen_before: 'f0', fen_after: 'f1', uci: 'e2e4', san: 'e4', color: 'white', opening_name: 'Sicilian Defense', eco: 'B20' });
        t2.addMove({ fen_before: 'f1', fen_after: 'f2', uci: 'c7c5', san: 'c5', color: 'black', opening_name: 'Sicilian Defense', eco: 'B20' });
        return t2.getAllLines()[0];
    })();
    s.currentStudy = 'My Study';
    s.treeOverlapMap = { white: { [goodLine.lineId]: { status: 'already_present', matched_plies: 2, total_plies: 2, message: 'present' } }, black: {} };
    s.renderTreeLines();
    const benignCard = listEl.innerHTML;
    t('SEC-M/line card: the benign card is unchanged',
        /class="scout-var-badge">#1<\/span>/.test(benignCard) &&
        benignCard.indexOf('Sicilian Defense') !== -1 &&
        /font-weight:700;">B20<\/span>/.test(benignCard) &&
        /class="scout-var-pgn" title="1\. e4 c5">1\. e4 c5<\/div>/.test(benignCard) &&
        benignCard.indexOf('2 plies') !== -1 &&
        /✓ In White \(\d+p\)/.test(benignCard) &&
        /📥 Line → Black/.test(benignCard),
        benignCard);
    assertInert('line card (benign)', benignCard);

    // =======================================================================
    // 3. checkActiveLineOverlap()  —  #scoutActiveLineOverlap strip
    // =======================================================================
    s.lineTree.reset();
    s.lineTree.addMove({ fen_before: 'f0', fen_after: 'f1', uci: 'e2e4', san: EVIL_SAN, color: 'white', opening_name: EVIL, eco: EVIL });
    s.lineTree.addMove({ fen_before: 'f1', fen_after: 'f2', uci: 'c7c5', san: 'c5', color: 'black', opening_name: EVIL, eco: EVIL });

    let pair = 0;
    sandbox.fetch = makeFetch({
        '/api/scout/check_line': {
            queued: true,
            queue: [
                { body: overlapWhite }, { body: overlapBlack }
            ]
        }
    });
    await s.checkActiveLineOverlap();
    const strip = q('scoutActiveLineOverlap').innerHTML;
    assertInert('overlap strip (hostile)', strip);
    assertEscapedOnce('overlap strip', strip);
    t('SEC-M/overlap strip: the §4E wording is present verbatim',
        /⚡ White: 1\/2 in Rep \(\+1 new\)/.test(strip) && /✓ In Black/.test(strip), strip);

    // The numeric hardening: a hostile count must not reach the badge text.
    pair = 0;
    sandbox.fetch = makeFetch({
        '/api/scout/check_line': {
            queued: true,
            queue: [
                { body: { status: 'partially_present', matched_plies: EVIL, total_plies: '1e999', new_plies: null, message: 'm' } },
                { body: { status: 'already_present', matched_plies: {}, total_plies: [], new_plies: undefined, message: 'm' } }
            ]
        }
    });
    await s.checkActiveLineOverlap();
    const strip2 = q('scoutActiveLineOverlap').innerHTML;
    t('SEC-M/overlap strip: a hostile ply count cannot become badge text',
        strip2.indexOf('NaN') === -1 &&
        strip2.indexOf('Infinity') === -1 &&
        strip2.indexOf('&lt;img') === -1 &&
        /⚡ White: 0\/0 in Rep \(\+0 new\)/.test(strip2),
        strip2);
    assertInert('overlap strip (hostile counts)', strip2);

    // The ply-0 placeholder (8.5) is a literal, not a collapsed box.
    s.lineTree.reset();
    await s.checkActiveLineOverlap();
    t('SEC-M/overlap strip: ply 0 renders the §8.5 placeholder, not an empty box',
        /Repertoire: Starting Position/.test(q('scoutActiveLineOverlap').innerHTML) &&
        q('scoutActiveLineOverlap').style.display === 'flex',
        q('scoutActiveLineOverlap').style.display);

    // =======================================================================
    // 4. checkTreeOverlap() + _applyTreeBadges()  —  the async refresh path
    // =======================================================================
    const l2 = hostileLine();
    s.treeOverlapMap = {};
    sandbox.fetch = makeFetch({
        '/api/scout/check_tree': {
            queued: true,
            queue: [
                { body: [{ id: l2.lineId, status: 'partially_present', matched_plies: 1, total_plies: 2, new_plies: 1, message: EVIL }] },
                { body: [{ id: l2.lineId, status: 'new', message: EVIL }] }
            ]
        }
    });
    s.renderTreeLines();
    await s.checkTreeOverlap();
    // `_applyTreeBadges` patches the badge row and the two import buttons IN PLACE, so the
    // assertion reads those child elements rather than the container's stale string.
    const cardEl = listEl.querySelector('.scout-var-card');
    const badgesEl = cardEl ? cardEl.querySelector('.scout-card-overlap-badges') : null;
    const importWEl = cardEl ? cardEl.querySelector('.btn-import-tree-white') : null;
    const refreshed = (badgesEl ? badgesEl.innerHTML : '') + '\n' + (importWEl ? importWEl.textContent : '');
    t('SEC-M/async badge refresh: the real DOM nodes the wiring addresses were found',
        !!cardEl && !!badgesEl && !!importWEl, String(!!cardEl) + '/' + String(!!badgesEl) + '/' + String(!!importWEl));
    assertInert('async badge refresh (hostile)', refreshed);
    t('SEC-M/async badge refresh: the same §4E wording reaches the live card',
        /⚡ White: 1\/2 in Rep \(\+1 new\)/.test(refreshed) &&
        /★ New Line/.test(refreshed),
        refreshed.slice(0, 900));
    t('SEC-M/async badge refresh: the import button label is refreshed, not re-escaped',
        /📥 Extend White \(\+1\)/.test(refreshed) && refreshed.indexOf('&amp;#128229;') === -1,
        refreshed.slice(0, 900));
    t('SEC-M/async badge refresh: the live card keeps its click-target classes after the patch',
        /class="scout-var-card/.test(cardEl ? listEl.innerHTML : '') &&
        (importWEl.getAttribute('data-idx') === '0'),
        String(importWEl && importWEl.getAttribute('data-idx')));
    assertEscapedOnce('async badge refresh', refreshed);

    // =======================================================================
    // 5. The import-status contract (FIXER-I / SCOUT-11)
    // =======================================================================
    const moves = [{ fen_before: 'f0', fen_after: 'f1', uci: 'e2e4', san: 'e4', color: 'white' }];

    function noticesFor(status, extra) {
        toasts.length = 0;
        sandbox.fetch = makeFetch({
            '/api/scout/import_repertoire': { body: Object.assign({ status: status }, extra || {}) }
        });
        s.loadStudies = function () { return Promise.resolve(); };
        s.renderTreeLines = function () { };
        s.scheduleOverlapCheck = function () { };
        return s._submitLineImport('white', moves, 'My Study', 'Line ');
    }

    await noticesFor('success', { imported_count: 3, inserted_count: 2, total_moves: 3, message: 'ok' });
    t('SEC-M/import: `success` reports inserted_count and no red toast',
        toasts.some(function (x) { return /✓ Line added 2 moves to White \(1 already stored, updated\) \[My Study\]/.test(x.msg) && x.color === '#4ade80'; }) &&
        !toasts.some(function (x) { return /✗/.test(x.msg); }),
        JSON.stringify(toasts));

    await noticesFor('success', { imported_count: 3, inserted_count: 0, total_moves: 3, message: 'ok' });
    t('SEC-M/import: an idempotent re-import says "already stored, updated"',
        toasts.some(function (x) { return /already stored, updated\)/.test(x.msg); }) &&
        !toasts.some(function (x) { return /✗/.test(x.msg); }),
        JSON.stringify(toasts));

    await noticesFor('already_present', { imported_count: 0, inserted_count: 0, total_moves: 4, already_present: true, message: 'Nothing to do' });
    t('SEC-M/import: `already_present` is a SUCCESS (blue), not a red rejection',
        toasts.some(function (x) { return /already in White \(4 plies\)/.test(x.msg) && x.color === '#38bdf8'; }) &&
        !toasts.some(function (x) { return /✗ Import rejected/.test(x.msg); }),
        JSON.stringify(toasts));

    await noticesFor('success', { imported_count: 1, inserted_count: 1, errors: ['ply 3: unresolvable SAN'] });
    t('SEC-M/import: `errors` is surfaced even on a partial success',
        toasts.some(function (x) { return /⚠ 1 move\(s\) skipped: ply 3: unresolvable SAN/.test(x.msg) && x.color === '#f59e0b'; }),
        JSON.stringify(toasts));

    await noticesFor('error', { imported_count: 0, inserted_count: 0, errors: ['parent FEN invalid', 'ply 2: illegal'] });
    t('SEC-M/import: `error` is a red failure and lists every error',
        toasts.some(function (x) { return /✗ Import into White repertoire failed: parent FEN invalid \| ply 2: illegal/.test(x.msg); }) ||
        toasts.some(function (x) { return /✗ Import rejected:/.test(x.msg) && /parent FEN invalid/.test(x.msg); }),
        JSON.stringify(toasts));

    // The builder's two import branches (item 2 of the brief).
    const bToasts = [];
    b.notify = function (msg, color) { bToasts.push({ msg: msg, color: color }); };
    b.loadRepertoire = function () { return Promise.resolve(); };
    b.loadStudies = function () { return Promise.resolve(); };
    b.refreshTree = function () { return Promise.resolve(); };
    b.scheduleOverlapCheck = function () { };
    b.beginWrite = function () { return true; };
    b.endWrite = function () { };
    b.currentStudy = 'My Study';
    b.lineTree.reset();
    b.lineTree.addMove({ fen_before: 'f0', fen_after: 'f1', uci: 'e2e4', san: 'e4', color: 'white' });

    bToasts.length = 0;
    sandbox.fetch = makeFetch({
        '/api/repertoire/import_line': { body: { status: 'already_present', already_present: true, total_moves: 5, imported_count: 0, inserted_count: 0 } }
    });
    await b.importToRepertoire('white');
    t('SEC-M/builder import: `already_present` is a blue "nothing to add", not an amber warning',
        bToasts.some(function (x) { return /nothing to add, all 5 plies already stored/.test(x.msg) && x.color === '#38bdf8'; }) &&
        !bToasts.some(function (x) { return /^Import:/.test(x.msg); }),
        JSON.stringify(bToasts));

    bToasts.length = 0;
    sandbox.fetch = makeFetch({
        '/api/repertoire/import_line': { body: { status: 'success', imported_count: 4, inserted_count: 4, total_moves: 4 } }
    });
    await b.importToRepertoire('white');
    t('SEC-M/builder import: `success` reports the inserted count',
        bToasts.some(function (x) { return /✓ Added 4 moves to White Repertoire \(My Study\)/.test(x.msg) && x.color === '#4ade80'; }),
        JSON.stringify(bToasts));

    bToasts.length = 0;
    sandbox.fetch = makeFetch({
        '/api/repertoire/import_line': { body: { status: 'error', imported_count: 0, inserted_count: 0, errors: ['ply 1: unresolvable SAN'] } }
    });
    await b.importToRepertoire('white');
    t('SEC-M/builder import: `error` is red and surfaces `errors`',
        bToasts.some(function (x) { return /Import failed:/.test(x.msg) && /ply 1: unresolvable SAN/.test(x.msg) && x.color === '#ef4444'; }),
        JSON.stringify(bToasts));

    bToasts.length = 0;
    sandbox.fetch = makeFetch({
        '/api/repertoire/import_line': { body: { status: 'success', imported_count: 2, inserted_count: 1, total_moves: 2, errors: ['ply 2 skipped'] } }
    });
    await b.importAllLinesToRepertoire('black');
    t('SEC-M/builder bulk import: `already_present` is accepted and partial errors are reported',
        bToasts.some(function (x) { return /Added all lines \(1 move\)/.test(x.msg); }) &&
        bToasts.some(function (x) { return /⚠ 1 move\(s\) skipped: ply 2 skipped/.test(x.msg); }),
        JSON.stringify(bToasts));

    bToasts.length = 0;
    sandbox.fetch = makeFetch({
        '/api/repertoire/import_line': { body: { status: 'already_present', already_present: true, imported_count: 0, inserted_count: 0 } }
    });
    await b.importAllLinesToRepertoire('black');
    t('SEC-M/builder bulk import: `already_present` is a blue no-op, not an amber warning',
        bToasts.some(function (x) { return /already in Black Repertoire/.test(x.msg) && x.color === '#38bdf8'; }) &&
        !bToasts.some(function (x) { return /^Import:/.test(x.msg); }),
        JSON.stringify(bToasts));

    // =======================================================================
    // 6. #trainNoteBox geometry (FIXER-H's blocked item)
    // =======================================================================
    const box = q('trainNoteBox');
    const noteEl = q('trainNoteContent');
    const labelEl = document.createElement('div');
    labelEl.className = 'train-note-label';
    box.appendChild(labelEl);
    box.appendChild(noteEl);
    // The shim exposes `firstChild` but not `firstElementChild`, which is what the
    // browser (and therefore the real code) reads.
    Object.defineProperty(box, 'firstElementChild', {
        configurable: true, get: function () { return this.children[0] || null; }
    });
    const t2 = new RepertoireTrainer();
    t2._updateNoteBox('A plan that is long enough to need an ellipsis indeed');
    t('SEC-M/trainNoteBox: it still reserves a constant 44px box with display:block (§8.5 / CODE-12.c)',
        box.style.display === 'block' && box.style.minHeight === '44px' &&
        box.style.maxHeight === '44px' && box.style.overflow === 'hidden' &&
        box.style.visibility === 'visible',
        JSON.stringify(box.style));
    t('SEC-M/trainNoteBox: the label is floated so the note cannot take a second line',
        labelEl.style.float === 'left' && labelEl.style.marginBottom === '0' &&
        /calc\(100% - \d+px\)/.test(noteEl.style.maxWidth),
        JSON.stringify(labelEl.style) + ' | ' + noteEl.style.maxWidth);
    t('SEC-M/trainNoteBox: the note is still clamped with a full copy in `title` (§8.2)',
        noteEl.style.whiteSpace === 'nowrap' && noteEl.style.overflow === 'hidden' &&
        noteEl.style.textOverflow === 'ellipsis' && noteEl.title.length > 0 &&
        noteEl.style.display === 'inline-block',
        JSON.stringify(noteEl.style));
    t2._updateNoteBox('');
    t('SEC-M/trainNoteBox: an empty note hides the box with visibility, never display (§8.4)',
        box.style.visibility === 'hidden' && box.style.display === 'block',
        JSON.stringify(box.style));

    return {
        allPass: results.every(function (r) { return r.pass; }),
        total: results.length,
        failures: results.filter(function (r) { return !r.pass; })
            .map(function (r) { return r.name + ' -> ' + r.detail; }),
        checks: results
    };
};

return run();
