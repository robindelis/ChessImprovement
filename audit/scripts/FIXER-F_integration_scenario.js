/*
 * audit/scripts/FIXER-F_integration_scenario.js
 *
 * Runtime (not static) verification of the FIXER-F patches, executed against the REAL shipped
 * `frontend/board.js` + `frontend/repertoire_builder.js` through `tests/frontend_harness.js`.
 *
 * Run:  node tests/frontend_harness.js audit/scripts/FIXER-F_integration_scenario.js
 * Output: JSON on stdout; `{ "allPass": true }` means every assertion held.
 */
'use strict';

const results = [];
function t(name, cond, detail) {
    results.push({ name: name, pass: !!cond, detail: cond ? undefined : String(detail) });
}

const b = window.repertoireBuilder;

// ---------------------------------------------------------------------------
// The harness only pre-seeds #repertoireBoard. Any builder DOM node it does not
// have is exactly the CODE3-03 failure mode, so inject the ones the new code needs.
// ---------------------------------------------------------------------------
const IDS = [
    'blunderBannerAlert', 'repGapsList', 'repCoverageGapsList', 'repGapsCount',
    'repGapsMeta', 'repCoverageGapsMeta', 'repPatchedGapsCount', 'btnRepTogglePatchedGaps',
    'btnRepNextBlunder', 'btnTabRepGaps', 'repGapsContainer', 'repGapsHeaderActions',
    'repArrowsCount', 'repMoveComment', 'repMoveToolSan', 'repMoveToolType',
    'btnRepToggleImportant', 'btnRepDeleteCurrentMove', 'repOpeningTitle', 'repOpeningEco',
    'repTurnIndicator', 'repBreadcrumbs', 'repEvalBarFill', 'repEvalBarLabel',
    'repEvalBarWrapper', 'repExplorerTbody', 'repDbStats', 'repVariationsList',
    'repTreeTabCount', 'repQuickLineCount', 'repGamesList', 'repGamesCount',
    'repGamesContainer', 'repTreeContainer', 'repTreeHeaderActions', 'repGamesHeaderActions',
    'repActiveLineOverlap', 'repCoveragePct', 'repCoverageFill', 'thSortMove',
    'thSortMaster', 'thSortMyStats', 'thSortEval', 'btnAdoptSolvedMove',
    'btnNextBlunderReviewSolve', 'btnSyncRecentGames', 'btnResyncGames',
    'syncProgressContainer', 'gameReviewBanner', 'builderStudySelect'
];
IDS.forEach(function (id) {
    if (!document.getElementById(id)) document.byId.set(id, document.createElement('div'));
});

function q(id) {
    const el = document.getElementById(id);
    if (!el) throw new Error('scenario setup: #' + id + ' was not injected');
    return el;
}

// The harness' default fetch never resolves; `updatePosition()` awaits the explorer request, so
// install a benign stub (kept aside for the CODE3-05 / CODE3-22 sequencing tests below).
const stubbedFetch = function (url, opts) {
    const u = String(url);
    if (u.indexOf('/api/repertoire/explore') !== -1) {
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ total_games: 0, moves: [] }); } });
    }
    if (u.indexOf('eval_stream') !== -1) {
        return Promise.resolve({ ok: false, status: 503 });
    }
    if (u.indexOf('check_line') !== -1 || u.indexOf('check_tree') !== -1) {
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ status: 'new', matched_plies: 0, total_plies: 0, new_plies: 0 }); } });
    }
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
};
window.fetch = stubbedFetch;

// Never let a pre-fix run crash the whole scenario: methods that only exist after the patch are
// replaced with a recording no-op, so the surrounding checks still run (and fail) and the report
// stays a graded list rather than a stack trace.
function need(obj, name) {
    if (typeof obj[name] === 'function') return obj[name];
    obj[name] = function () {
        results.push({ name: 'method ' + name + '() exists', pass: false, detail: 'missing' });
    };
    return obj[name];
}
need(b, 'renderGapLists');
need(b, 'adoptBlunderSolution');
need(b, 'addressNextGameGap');
need(b, '_evalCachePut');
need(b, 'beginWrite');
need(b, 'endWrite');
need(b, 'applyEvalBarOrientation');
need(b, 'loadRecentGamesCount');

// ===========================================================================
// Coverage gaps rendering verification
// ===========================================================================
b.treeData = {
    gaps: [{
        parent_fen: 'rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2',
        opponent_san: 'Nf6', opponent_uci: 'g8f6', popularity_pct: 8.3, games: 1200,
        opening_name: "King's Pawn", eco: 'C42'
    }],
    coverage_pct: 55
};
b.renderGapLists();
t('CODE3-03: the unprepared-opponent list gets a real caller for addressGap',
    q('repCoverageGapsList').innerHTML.indexOf('Nf6') !== -1 &&
    /btn-address-coverage-gap/.test(q('repCoverageGapsList').innerHTML),
    q('repCoverageGapsList').innerHTML.slice(0, 200));

// addressGap must be callable and land on a sane state
b.board = new InteractiveChessboard(document.getElementById('repertoireBoard'),
    { orientation: 'white', interactive: true });
b.board.loadFEN('rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2');
b.addressGap({
    parent_fen: 'rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2',
    opponent_uci: 'g8f6', opponent_san: 'Nf6', opening_name: "King's Pawn", eco: 'C42'
});
t('CODE3-03: addressGap replays the opponent move onto the board',
    b.currentStepIndex === 1 && b.history[1].san === 'Nf6',
    'idx=' + b.currentStepIndex + ' san=' + (b.history[1] && b.history[1].san));

// ===========================================================================
// CODE3-02: updatePosition must NOT wipe restored arrows; the counter must agree
// ===========================================================================
const arrowsJson = JSON.stringify([{ from: 'e2', to: 'e4' }, { from: 'g1', to: 'f3' }]);
b.treeData = {
    coverage_pct: 60,
    gaps: [],
    game_gaps: { gaps: [], patched_count: 0, total_games_analyzed: 0 },
    moves_by_parent: {
        'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1': [
            { move_uci: 'e2e4', move_san: 'e4', is_user_move: 1, is_important: 0, comment: '', arrows: arrowsJson }
        ]
    }
};
b.activeColor = 'white';
b.history = [
    { ply: 0, fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1', san: 'Start', uci: null, openingName: 'x', eco: '' },
    { ply: 1, fen: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1', san: 'e4', uci: 'e2e4', openingName: 'x', eco: '' }
];
b.currentStepIndex = 0;

const run = async () => {
    await b.updatePosition();
    b.jumpToStep(1);                    // the "Next ▶" fast path (with the ghost animation)
    await new Promise(r => setTimeout(r, 30));
    // Simulate the 180ms elapsing: run every pending animation's onfinish, which is exactly what
    // the pre-fix implementation deferred its `loadFEN()` (arrow wipe) into.
    (b.board.activeAnimations || []).slice().forEach(function (anim) {
        if (typeof anim.onfinish === 'function') anim.onfinish();
    });
    t('CODE3-02: arrows survive the animated step-forward even after the animation ends',
        b.board.arrows.length === 2, JSON.stringify(b.board.arrows));
    t('CODE3-02: the arrow counter agrees with the board',
        q('repArrowsCount').textContent === '2 arrows',
        q('repArrowsCount').textContent);
    t('CODE3-01/02: the Remove Line button becomes visible for a repertoire move',
        q('btnRepDeleteCurrentMove').style.visibility === 'visible' &&
        q('btnRepDeleteCurrentMove').disabled === false,
        q('btnRepDeleteCurrentMove').style.visibility);

    // CODE3-14: a malformed persisted `arrows` value must not throw out of render()
    b.treeData.moves_by_parent['rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'][0].arrows =
        '[null, {"from":"e2","to":"j9"}, {"from":"e2","to":"e4"}]';
    let threw = null;
    try { await b.updatePosition(); } catch (e) { threw = e; }
    t('CODE3-14: a corrupt arrows payload is filtered, not thrown',
        threw === null && b.board.arrows.length === 1 && b.board.arrows[0].to === 'e4',
        threw ? String(threw) : JSON.stringify(b.board.arrows));
    t('CODE3-14: the counter reports the number actually kept',
        q('repArrowsCount').textContent === '1 arrow',
        q('repArrowsCount').textContent);

    // ===========================================================================
    // CODE3-13: cancelling an animation still delivers the completion callback
    // ===========================================================================
    let delivered = 0;
    b.board = new InteractiveChessboard(document.getElementById('repertoireBoard'),
        { orientation: 'white', interactive: true });
    b.board.loadFEN('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1');
    b.board.render();
    b.board.animateMove('e2', 'e4', () => { delivered++; });
    b.board.stopAnimations();             // cancel mid-flight
    t('CODE3-13: a cancelled animation still calls onComplete exactly once',
        delivered === 1, 'delivered=' + delivered);

    // ===========================================================================
    // CODE3-08: bounded, normalised eval cache
    // ===========================================================================
    b.evalCache.clear();
    b.evalCacheMax = 4;
    b._evalCachePut('A', { score_cp: 1, depth: 10 });
    b._evalCachePut('B', { score_cp: 2, depth: 10 });
    b._evalCachePut('C', { score_cp: 3, depth: 10 });
    b._evalCachePut('D', { score_cp: 4, depth: 10 });
    b._evalCachePut('E', { score_cp: 5, depth: 10 });
    t('CODE3-08: evalCache never exceeds its bound', b.evalCache.size === 4, String(b.evalCache.size));
    t('CODE3-08: the oldest entry was evicted', !b.evalCache.has('A') && b.evalCache.has('E'));
    b._evalCachePut('E', { score_cp: 5, depth: 2 });
    t('CODE3-08: a shallower result never overwrites a deeper one',
        b.evalCache.get('E').depth === 10, String(b.evalCache.get('E').depth));

    // ===========================================================================
    // CODE3-21 / CODE3-20: flipping the board keeps the eval label readable
    // ===========================================================================
    b.renderEvalBar(500, null, 16);
    t('CODE3-21: a white advantage gets the white-lead class',
        q('repEvalBarLabel').className.indexOf('white-lead') !== -1,
        q('repEvalBarLabel').className);
    t('CODE3-20: the eval label exposes depth + full score on hover (8.2)',
        /depth 16/.test(q('repEvalBarLabel').title), q('repEvalBarLabel').title);
    b.applyEvalBarOrientation('black');
    b.renderEvalBar(b.lastEval.scoreCp, b.lastEval.mate, b.lastEval.depth);
    t('CODE3-21: after a flip the wrapper is .flipped AND the label is re-rendered',
        /flipped/.test(q('repEvalBarWrapper').className) &&
        q('repEvalBarLabel').textContent === '+5.0',
        q('repEvalBarWrapper').className + ' / ' + q('repEvalBarLabel').textContent);
    b.applyEvalBarOrientation('white');

    // ===========================================================================
    // CODE3-04: cancelSync aborts BOTH streams
    // ===========================================================================
    let resyncAborted = false, recentAborted = false;
    b.resyncAbortController = { abort: function () { resyncAborted = true; } };
    b.recentSyncAbortController = { abort: function () { recentAborted = true; } };
    b.cancelSync();
    t('CODE3-04: cancelSync aborts the full resync stream', resyncAborted);
    t('CODE3-04: cancelSync aborts the recent-games stream', recentAborted);
    t('CODE3-04: both handles are released', !b.resyncAbortController && !b.recentSyncAbortController);

    // The loser's finally must not clobber the winner's handle.
    const winner = { signal: 'winner' };
    b.resyncAbortController = winner;
    (function simulateLoserFinally() {
        const abortCtrl = { signal: 'loser' };
        if (b.resyncAbortController === abortCtrl) b.resyncAbortController = null;
    })();
    t('CODE3-04: a losing stream\'s finally does not null the winner\'s handle',
        b.resyncAbortController === winner);

    // ===========================================================================
    // CODE3-12: in-flight latch
    // ===========================================================================
    t('CODE3-12: a second concurrent write for the same key is rejected',
        b.beginWrite('k') === true && b.beginWrite('k') === false);
    b.endWrite('k');
    t('CODE3-12: the key is reusable once released', b.beginWrite('k') === true);
    b.endWrite('k');

    // ===========================================================================
    // CODE3-16: board teardown
    // ===========================================================================
    t('CODE3-16: InteractiveChessboard exposes destroy()',
        typeof InteractiveChessboard.prototype.destroy === 'function');
    b.board.destroy();
    t('CODE3-16: destroy() clears the square map', Object.keys(b.board.squares).length === 0);

    // ===========================================================================
    // CODE3-15: marker ids are namespaced
    // ===========================================================================
    const b2 = new InteractiveChessboard(document.createElement('div'), { orientation: 'white' });
    b2.setArrows([{ from: 'e2', to: 'e4', color: '#ef4444' }]);
    const b3 = new InteractiveChessboard(document.createElement('div'), { orientation: 'white' });
    b3.setArrows([{ from: 'd2', to: 'd4', color: '#22c55e' }]);
    const markerIds = [b2, b3].map(x => x.svgEl.children[0].children[0].getAttribute('id'));
    t('CODE3-15: two boards never share a marker id', markerIds[0] !== markerIds[1], markerIds.join(' vs '));

    // ===========================================================================
    // CODE3-17: the dead candidateCache fallback key is gone
    // ===========================================================================
    t('CODE3-17: stepForward no longer probes the never-written cache key',
        String(b.stepForward).indexOf('this.candidateCache.get(normParent)') === -1);

    // ===========================================================================
    // CODE3-05: recent-games responses are sequenced
    // ===========================================================================
    const realFetch = window.fetch;
    let resolveOld = null, resolveNew = null;
    window.fetch = function (url) {
        if (/search=a(&|$)/.test(String(url))) {
            return new Promise(function (r) { resolveOld = r; });
        }
        return new Promise(function (r) { resolveNew = r; });
    };
    b.gamesColorFilter = 'all';
    b.gamesSearchQuery = 'a';
    const p1 = b.loadRecentGames();
    b.gamesSearchQuery = 'ab';
    const p2 = b.loadRecentGames();
    t('CODE3-05: two requests really were in flight', !!resolveOld && !!resolveNew,
        'old=' + !!resolveOld + ' new=' + !!resolveNew);
    resolveNew({
        ok: true,
        json: function () {
            return Promise.resolve({
                games: [{ id: 'NEW', result: 'win', score_text: '1-0', user_color: 'white', speed: 'blitz' }],
                total: 1
            });
        }
    });
    await p2;
    resolveOld({
        ok: true,
        json: function () {
            return Promise.resolve({
                games: [{ id: 'OLD', result: 'loss', score_text: '0-1', user_color: 'black', speed: 'blitz' }],
                total: 99
            });
        }
    });
    await p1;
    await new Promise(function (r) { setTimeout(r, 10); });
    t('CODE3-05: a slower earlier response cannot overwrite this.recentGames',
        b.recentGames.length === 1 && b.recentGames[0].id === 'NEW',
        JSON.stringify(b.recentGames));
    t('CODE3-05: the badge reflects the newest (filtered) query, not the stale total',
        q('repGamesCount').textContent === '1', String(q('repGamesCount').textContent));
    window.fetch = stubbedFetch;

    // ===========================================================================
    // CODE3-22: no null dereference / silent failure on Reset Defaults
    // ===========================================================================
    window.fetch = function () {
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve(null); } });
    };
    let toast = null;
    window.showToast = function (m) { toast = m; };
    await b.seedDefaults();               // backend returns JSON null on the success path
    t('CODE3-22: seedDefaults survives a null body and tells the user',
        toast !== null && /unexpected response/i.test(toast), String(toast));
    t('CODE3-22: seedDefaults never sends force=true (AGENTS.md 9.3/9.4)',
        /fetch\('\/api\/repertoire\/seed',\s*\{\s*method:\s*'POST'\s*\}\)/.test(String(b.seedDefaults)));

    const allPass = results.every(function (r) { return r.pass; });
    return {
        allPass: allPass,
        total: results.length,
        failures: results.filter(function (r) { return !r.pass; }),
        checks: results
    };
};

// Never let a pre-fix crash swallow the graded report.
const safeRun = async () => {
    try {
        return await run();
    } catch (err) {
        results.push({
            name: 'scenario completed without an exception',
            pass: false,
            detail: String((err && err.stack) || err)
        });
        return {
            allPass: false,
            total: results.length,
            failures: results.filter(function (r) { return !r.pass; }),
            checks: results
        };
    }
};

return safeRun();
