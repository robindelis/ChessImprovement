/*
 * audit/scripts/FIXER-J_xss_scenario.js
 *
 * Runtime verification of the SEC-J XSS retrofit, executed against the REAL shipped
 * `frontend/board.js` + `frontend/repertoire_builder.js` through `tests/frontend_harness.js`.
 *
 * Run:  node tests/frontend_harness.js audit/scripts/FIXER-J_xss_scenario.js
 * Output: JSON on stdout; `{ "allPass": true }` means every assertion held.
 *
 * Only the PUBLIC renderers are driven (`loadRecentGames`, `renderGameReviewBanner`,
 * `renderCandidateTable`, `renderTreeLines`, `renderGapLists`, `updateSortHeaderUI`), never a
 * private helper, so the very same file can be pointed at a pre-fix copy of the sources.
 *
 * Why no real DOM is needed: the shim stores the markup string handed to `innerHTML` verbatim
 * (`FakeElement.innerHTML` is a plain string). That is exactly the artefact an XSS payload needs,
 * so each check below tokenises the produced markup and asserts that the only elements,
 * attributes and URLs it contains are the ones the renderer is meant to emit. A hostile value
 * therefore shows up as an extra `<img>`/`<script>` tag, an `on*` handler or a `javascript:` URL,
 * and never as harmless text.
 */
'use strict';

const results = [];
function t(name, cond, detail) {
    results.push({ name: name, pass: !!cond, detail: cond ? undefined : String(detail) });
}

const b = window.repertoireBuilder;

// ---------------------------------------------------------------------------
// Inject every #id the renderers under test touch (the harness only pre-seeds
// #repertoireBoard). A missing node would make a renderer a silent no-op, which is
// exactly the CODE3-03 failure mode.
// ---------------------------------------------------------------------------
const IDS = [
    'repertoireBoard',
    'blunderBannerAlert', 'repGapsList', 'repCoverageGapsList', 'repGapsCount',
    'repGapsMeta', 'repCoverageGapsMeta', 'repPatchedGapsCount', 'btnRepTogglePatchedGaps',
    'btnRepNextBlunder', 'repArrowsCount', 'repMoveComment', 'repMoveToolSan', 'repMoveToolType',
    'btnRepToggleImportant', 'btnRepDeleteCurrentMove', 'repOpeningTitle', 'repOpeningEco',
    'repTurnIndicator', 'repBreadcrumbs', 'repEvalBarFill', 'repEvalBarLabel',
    'repEvalBarWrapper', 'repExplorerTbody', 'repDbStats', 'repVariationsList',
    'repTreeTabCount', 'repQuickLineCount', 'repGamesList', 'repGamesCount',
    'repActiveLineOverlap', 'repCoveragePct', 'repCoverageFill', 'thSortMove',
    'thSortMaster', 'thSortMyStats', 'thSortEval', 'gameReviewBanner', 'builderStudySelect'
];
IDS.forEach(function (id) {
    if (!document.getElementById(id)) document.byId.set(id, document.createElement('div'));
});
function q(id) {
    const el = document.getElementById(id);
    if (!el) throw new Error('scenario setup: #' + id + ' was not injected');
    return el;
}

// ===========================================================================
// Markup scanner: the only pieces of a hostile value that can execute are extra
// elements, event-handler attributes and script-bearing URLs. `<button>` and `<a>`
// are NOT listed: the renderers legitimately emit them, and their wiring is asserted
// separately (a `.btn-review-game` button is a click target, not an injection).
// ===========================================================================
const DANGEROUS_TAGS = [
    'script', 'img', 'svg', 'math', 'iframe', 'object', 'embed', 'applet', 'form',
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

/** A renderer's markup must contain no executable node at all. */
function assertInert(label, html) {
    const findings = scanMarkup(html || '');
    t('SEC-J/' + label + ': produces no executable node', findings.length === 0,
        JSON.stringify(findings) + ' in ' + String(html).slice(0, 500));
    return findings;
}

const EVIL = '<img src=x onerror=alert(1)>';
const EVIL_ATTR = '" onmouseover="alert(2)';
const EVIL_URL = 'javascript:alert(3)';
const EVIL_RESULT = 'win" onmouseover="alert(9)';

/** Escaped exactly once: the escaped form is present, the double-escaped form is not. */
function assertEscapedOnce(label, html) {
    t('SEC-J/' + label + ': the payload is escaped exactly once',
        html.indexOf('&lt;img') !== -1 && html.indexOf('&amp;lt;img') === -1,
        String(html).slice(0, 500));
}

// ---------------------------------------------------------------------------
// Fetch stub: only the endpoints the renderers under test call.
// ---------------------------------------------------------------------------
function makeFetch(overrides) {
    return function (url) {
        const u = String(url);
        if (overrides) {
            for (const key of Object.keys(overrides)) {
                if (u.indexOf(key) !== -1) return Promise.resolve(overrides[key]);
            }
        }
        return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } });
    };
}

const goodGame = {
    id: 'gAAA', result: 'win', score_text: '1-0', user_color: 'white', opening_ply: 10,
    opponent_name: 'OpponentName', opponent_rating: 2100, speed: 'blitz',
    played_at_formatted: '2026-01-02 10:00', eco: 'C42', opening_name: "King's Pawn",
    game_url: 'https://lichess.org/gAAA'
};
const evilGame = Object.assign({}, goodGame, {
    id: 'gEVIL',
    result: EVIL_RESULT,
    score_text: EVIL,
    opponent_name: EVIL,
    opponent_rating: EVIL,
    speed: EVIL,
    played_at_formatted: EVIL,
    eco: EVIL,
    opening_name: EVIL,
    game_url: EVIL_URL
});

const run = async () => {
    // =======================================================================
    // 1. loadRecentGames()  —  SYNC-13: /api/games/recent rows
    // =======================================================================
    async function renderGamesList(games) {
        b.gamesColorFilter = 'all';
        b.gamesSearchQuery = '';
        b.activeReviewGame = null;
        window.fetch = makeFetch({
            '/api/games/recent': { ok: true, json: function () { return Promise.resolve({ games: games, total: games.length }); } }
        });
        await b.loadRecentGames();
        const list = q('repGamesList');
        return list.children.map(function (c) { return c.innerHTML; }).join('\n');
    }

    const benignCard = await renderGamesList([goodGame]);
    const hostileCard = await renderGamesList([evilGame]);

    assertInert('games card (benign)', benignCard);
    assertInert('games card (hostile)', hostileCard);
    assertEscapedOnce('games card', hostileCard);
    t('SEC-J/games card: the click-target classes the wiring queriesSelector()s for are unchanged',
        /class="btn-primary btn-review-game"/.test(benignCard) &&
        /class="btn-secondary btn-quick-add-opening"/.test(benignCard) &&
        /class="game-outcome-pill win"/.test(benignCard) &&
        /title="Load game on board to explore opening and variations"/.test(benignCard) &&
        /title="Import recognized opening into your white repertoire"/.test(benignCard) &&
        /target="_blank" rel="noopener noreferrer"/.test(benignCard) &&
        String(b.loadRecentGames).indexOf("item.querySelector('.btn-review-game')") !== -1 &&
        String(b.loadRecentGames).indexOf("item.querySelector('.btn-quick-add-opening')") !== -1,
        benignCard);
    // `King&#39;s Pawn` vs `King's Pawn` is an entity-vs-literal difference in the *source*
    // string only; both parse to the same glyph, so nothing moves on screen.
    t('SEC-J/games card: benign rendering is unchanged',
        benignCard.indexOf('vs OpponentName') !== -1 &&
        benignCard.indexOf('<strong style="color:#38bdf8;">C42</strong>') !== -1 &&
        /King&#39;s Pawn|King's Pawn/.test(benignCard) &&
        benignCard.indexOf('href="https://lichess.org/gAAA"') !== -1 &&
        benignCard.indexOf('WIN (1-0)') !== -1 &&
        benignCard.indexOf('>blitz • 2026-01-02 10:00<') !== -1 &&
        benignCard.indexOf('(2100)') !== -1,
        benignCard);
    t('SEC-J/games card: a javascript: game_url is neutralised',
        hostileCard.indexOf('href="javascript:') === -1 && hostileCard.indexOf('href="#"') !== -1,
        hostileCard.slice(0, 500));
    t('SEC-J/games card: a hostile result cannot escape the class attribute',
        /class="game-outcome-pill (win|loss|draw)"/.test(hostileCard) &&
        hostileCard.indexOf('onmouseover="alert(9)"') === -1 &&
        /&quot;\s+onmouseover=&quot;alert\(9\)/i.test(hostileCard),
        hostileCard.slice(0, 500));

    // =======================================================================
    // 2. renderGameReviewBanner()  —  SYNC-13 again, in the review banner
    // =======================================================================
    b.activeReviewGame = null;
    b.renderGameReviewBanner(goodGame);
    const benignBanner = q('gameReviewBanner').innerHTML;
    b.renderGameReviewBanner(evilGame);
    const hostileBanner = q('gameReviewBanner').innerHTML;

    assertInert('game review banner (benign)', benignBanner);
    assertInert('game review banner (hostile)', hostileBanner);
    assertEscapedOnce('game review banner', hostileBanner);
    t('SEC-J/game review banner: the three wired button ids are still emitted',
        /id="btnJumpToOpeningEnd"/.test(benignBanner) &&
        /id="btnAddGameToRep"/.test(benignBanner) &&
        /id="btnExitGameReview"/.test(benignBanner),
        benignBanner);
    t('SEC-J/game review banner: benign rendering is unchanged',
        benignBanner.indexOf('Reviewing vs OpponentName') !== -1 &&
        benignBanner.indexOf('(2100)') !== -1 &&
        /King&#39;s Pawn|King's Pawn/.test(benignBanner) &&
        benignBanner.indexOf('class="game-outcome-pill win"') !== -1 &&
        benignBanner.indexOf('WIN (1-0)') !== -1 &&
        benignBanner.indexOf('Theory: 10 plies') !== -1 &&
        benignBanner.indexOf('End of Theory (Move 5)') !== -1 &&
        benignBanner.indexOf('Add Line &rarr; White Rep') !== -1 &&
        benignBanner.indexOf('href="https://lichess.org/gAAA"') !== -1,
        benignBanner);
    t('SEC-J/game review banner: a javascript: game_url is neutralised',
        hostileBanner.indexOf('href="javascript:') === -1 &&
        hostileBanner.indexOf('href="#"') !== -1,
        hostileBanner.slice(0, 500));

    // =======================================================================
    // 3. renderCandidateTable()  —  /api/repertoire/explore rows (the Lichess
    //    Masters explorer JSON cached verbatim in explorer_cache.data_json)
    // =======================================================================
    const goodMove = {
        san: 'Nf6', uci: 'g8f6', popularity_pct: 55.3, games: 1200, eval: 150,
        white_win_pct: 30, draw_pct: 40, black_win_pct: 30,
        my_stats: { games: 3, wins: 1, draws: 1, losses: 1, win_rate: 33, threat_score: 8.4 }
    };
    const evilMove = {
        san: EVIL, uci: 'g8f6', popularity_pct: EVIL, games: EVIL, eval: null,
        white_win_pct: EVIL, draw_pct: EVIL, black_win_pct: EVIL,
        my_stats: { games: EVIL, wins: EVIL, draws: EVIL, losses: EVIL, win_rate: EVIL, threat_score: EVIL }
    };
    const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

    function renderCandidateRows(moves, isUserTurn) {
        b.currentCandidateData = { total_games: 100, moves: moves };
        b.treeData = { moves_by_parent: {} };
        b.candidateSort = { col: 'threat', dir: 'desc' };
        b.renderCandidateTable(START_FEN, isUserTurn);
        return q('repExplorerTbody').children.map(function (tr) { return tr.innerHTML; }).join('\n');
    }

    const benignRows = renderCandidateRows([goodMove], true);
    const hostileRows = renderCandidateRows([evilMove], true);
    assertInert('candidate row (benign)', benignRows);
    assertInert('candidate row (hostile)', hostileRows);
    assertEscapedOnce('candidate row', hostileRows);
    t('SEC-J/candidate row: benign rendering is unchanged',
        /<span class="explorer-move-san">Nf6<\/span>/.test(benignRows) &&
        benignRows.indexOf('style="width:55.3%;"') !== -1 &&
        benignRows.indexOf('55.3%') !== -1 &&
        benignRows.indexOf('1,200 games') !== -1 &&
        benignRows.indexOf('<span class="eval-badge plus" title="White advantage (+1.50)">+1.50</span>') !== -1 &&
        benignRows.indexOf('title="White 30% | Draw 40% | Black 30%"') !== -1 &&
        benignRows.indexOf('style="width:30%;"') !== -1 &&
        benignRows.indexOf('3 games') !== -1 &&
        benignRows.indexOf('⚠️ 33%') !== -1 &&
        benignRows.indexOf('Threat Score: 8.4') !== -1 &&
        benignRows.indexOf('1W • 1D • 1L') !== -1 &&
        /class="btn-add-move btn-set-move"/.test(benignRows),
        benignRows);
    t('SEC-J/candidate row: the "+ Add Branch" click target is still emitted',
        /class="btn-add-move btn-add-branch"/.test(renderCandidateRows([goodMove], false)),
        'branch button missing');
    function renderCandidateRowsWithTree(moves, isUserTurn, treeData) {
        b.currentCandidateData = { total_games: 100, moves: moves };
        b.treeData = treeData;
        b.candidateSort = { col: 'threat', dir: 'desc' };
        b.renderCandidateTable(START_FEN, isUserTurn);
        return q('repExplorerTbody').children.map(function (tr) { return tr.innerHTML; }).join('\n');
    }
    // `renderCandidateTable` looks the parent up by the NORMALISED (3-field) FEN.
    const NORM_FEN = b.normalizeFen(START_FEN);
    function treeWith(flag) {
        const o = {}; o[NORM_FEN] = [{ move_uci: 'g8f6', is_user_move: flag }]; return { moves_by_parent: o };
    }
    t('SEC-J/candidate row: the "★ Your Move" / "✕" pair still appears for a prepared move',
        (function () {
            const none = renderCandidateRowsWithTree([goodMove], true, { moves_by_parent: {} });
            const mine = renderCandidateRowsWithTree([goodMove], true, treeWith(1));
            const asOpp = renderCandidateRowsWithTree([goodMove], false, treeWith(0));
            return /class="badge-in-tree">★ Your Move<\/span>/.test(mine) &&
                /class="btn-del-branch btn-delete-move"/.test(mine) &&
                /class="badge-opp-tree">✓ In Tree<\/span>/.test(asOpp) &&
                /class="btn-del-branch btn-delete-branch"/.test(asOpp) &&
                /class="btn-add-move btn-set-move"/.test(none);
        })(),
        'prepared-move actions missing');

    // The row click listener is bound on the <tr> itself, so it survives the shim.
    let playedFromRow = null;
    b.playMoveFromUci = function (uci, san) { playedFromRow = uci + '/' + san; };
    renderCandidateRows([goodMove], true);
    const firstRow = q('repExplorerTbody').children[0];
    firstRow.dispatchEvent({ type: 'click', target: firstRow, preventDefault() { } });
    t('SEC-J/candidate row: clicking a row still calls playMoveFromUci',
        playedFromRow === 'g8f6/Nf6', String(playedFromRow));

    // =======================================================================
    // 4. renderTreeLines()  —  session-tree line name / eco / pgn
    // =======================================================================
    function seedLine(name, eco, sanA, sanB) {
        const lt = b.lineTree;
        lt.reset();
        lt.addMove({ fen_before: 'f0', fen_after: 'f1', uci: 'e2e4', san: sanA, color: 'white', move_number: 1, opening_name: name, eco: eco });
        lt.addMove({ fen_before: 'f1', fen_after: 'f2', uci: 'c7c5', san: sanB, color: 'black', move_number: 1, opening_name: name, eco: eco });
        b.renderTreeLines();
        return q('repVariationsList').innerHTML;
    }

    const benignTree = seedLine('Sicilian Defense', 'B20', 'e4', 'c5');
    assertInert('tree line (benign)', benignTree);
    t('SEC-J/tree line: benign rendering is unchanged',
        /class="scout-var-card/.test(benignTree) &&
        benignTree.indexOf('Sicilian Defense') !== -1 &&
        /<span style="font-size:0\.7rem; color:var\(--accent-primary\); font-weight:700;">B20<\/span>/.test(benignTree) &&
        benignTree.indexOf('2 plies') !== -1 &&
        /<div class="scout-var-pgn" title="1\. e4 c5">1\. e4 c5<\/div>/.test(benignTree) &&
        benignTree.indexOf('<span class="scout-var-badge">#1</span>') !== -1,
        benignTree);
    t('SEC-J/tree line: every click-target class and its data-idx are still emitted',
        ['btn-load-rep-tree-line', 'btn-import-rep-tree-white', 'btn-import-rep-tree-black',
            'btn-del-rep-session-line'].every(function (c) {
            return new RegExp('class="btn-var-action ' + c + '" data-idx="0"').test(benignTree);
        }) && (benignTree.match(/data-idx="0"/g) || []).length === 5,
        benignTree);

    const hostileTree = seedLine(EVIL, EVIL, EVIL, EVIL_ATTR);
    assertInert('tree line (hostile)', hostileTree);
    assertEscapedOnce('tree line', hostileTree);
    t('SEC-J/tree line: a quote in a SAN cannot open a new attribute',
        hostileTree.indexOf('onmouseover="alert(2)"') === -1 &&
        hostileTree.indexOf('&quot; onmouseover=&quot;alert(2)') !== -1,
        hostileTree.slice(0, 800));

    // =======================================================================
    // 5. updateSortHeaderUI()  —  the label embeds a number from the payload
    // =======================================================================
    b.currentCandidateData = { user_games_total: 99 };
    b.treeData = { game_gaps: { total_games_analyzed: 99 } };
    b.candidateSort = { col: 'threat', dir: 'desc' };
    b.updateSortHeaderUI();
    const sortHeader = q('thSortMyStats');
    t('SEC-J/sort header: the active label is still "My Stats (N Games) + arrow span"',
        sortHeader.textContent.indexOf('My Stats (99 Games)') !== -1 &&
        sortHeader.textContent.indexOf('▾') !== -1 &&
        sortHeader.children.length === 2 &&
        sortHeader.children[1].className === 'sort-arrow' &&
        sortHeader.children[1].textContent === '▾',
        JSON.stringify({ text: sortHeader.textContent, kids: sortHeader.children.map(function (c) { return c.className; }), html: sortHeader.innerHTML }));
    assertInert('sort header (benign)', sortHeader.innerHTML);
    // `total_games_analyzed` is user-controlled: a hostile string must never become markup.
    b.currentCandidateData = { user_games_total: EVIL };
    b.treeData = { game_gaps: { total_games_analyzed: EVIL } };
    b.updateSortHeaderUI();
    t('SEC-J/sort header: a hostile count never becomes markup',
        sortHeader.innerHTML === '' && scanMarkup(sortHeader.innerHTML).length === 0 &&
        sortHeader.textContent.indexOf('<img') === -1,
        JSON.stringify({ text: sortHeader.textContent, html: sortHeader.innerHTML }));
    b.candidateSort = { col: 'threat', dir: 'desc' };
    b.updateSortHeaderUI();
    t('SEC-J/sort header: an inactive column is a plain text label',
        q('thSortMaster').children.length === 0 &&
        q('thSortMaster').textContent === 'Master (2000+)',
        JSON.stringify({ text: q('thSortMaster').textContent, html: q('thSortMaster').innerHTML }));
    b.updateSortHeaderUI();
    t('SEC-J/sort header: re-sorting does not accumulate labels',
        sortHeader.children.length === 2 && sortHeader.textContent.split('My Stats').length === 2,
        JSON.stringify({ text: sortHeader.textContent }));

    // =======================================================================
    // 6. board.js — the persisted arrow colour must be a paint value
    // =======================================================================
    const board = new InteractiveChessboard(document.getElementById('repertoireBoard'),
        { orientation: 'white', interactive: true });
    let arrowThrew = null;
    let kept = null;
    try {
        kept = board.setArrows([
            { from: 'e2', to: 'e4', color: 'url(javascript:alert(1))' },
            { from: 'g1', to: 'f3', color: '#22c55e' },
            { from: 'e4', to: 'e5' }
        ]);
    } catch (e) { arrowThrew = e; }
    t('SEC-J/board: a hostile arrow colour is replaced, every valid arrow survives',
        arrowThrew === null && kept === 3 &&
        board.arrows.every(function (a) { return a.color === '#22c55e'; }),
        arrowThrew ? String(arrowThrew) : JSON.stringify(board.arrows));
    const painted = board.svgEl._descendants([]).map(function (el) {
        return el.getAttribute('fill') || el.getAttribute('stroke');
    }).filter(function (v) { return v !== null; });
    t('SEC-J/board: no SVG paint attribute carries a quote or a url()',
        painted.length > 0 && painted.every(function (c) { return c === '#22c55e'; }),
        JSON.stringify(painted));

    // =======================================================================
    // 7. FIXER-F's gap lists / overlap badges must stay inert and escaped ONCE
    // =======================================================================
    b.activeReviewGame = null;
    b.activeReviewGap = null;
    b.treeData = {
        game_gaps: {
            gaps: [{
                id: 1, move_number: 5, ply: 9, played_san: EVIL, best_san: EVIL, best_move: 'g7g6',
                cpl_loss: 180, opening_name: EVIL, eco: EVIL, opening_games: EVIL,
                opponent_name: EVIL, game_moves: 'e4 e5', is_patched: false,
                earlier_mistake: { move_number: EVIL, played_san: EVIL }
            }],
            patched_count: 0, total_games_analyzed: 0
        },
        gaps: [{
            parent_fen: 'f', opponent_san: EVIL, opponent_uci: 'g8f6',
            popularity_pct: EVIL, games: EVIL, opening_name: EVIL, eco: EVIL
        }],
        coverage_pct: 50
    };
    b.renderGapLists();
    assertInert('blunder gap list', q('repGapsList').innerHTML);
    assertInert('coverage gap list', q('repCoverageGapsList').innerHTML);
    assertEscapedOnce('blunder gap list', q('repGapsList').innerHTML);
    t('SEC-J/gap list: the address buttons and their data attributes are still emitted',
        /class="btn-secondary btn-address-blunder"[^>]*data-gap-id="1"/.test(q('repGapsList').innerHTML) &&
        /class="btn-secondary btn-address-coverage-gap"[^>]*data-parent="f"/.test(q('repCoverageGapsList').innerHTML),
        q('repGapsList').innerHTML + ' || ' + q('repCoverageGapsList').innerHTML);

    b.treeOverlapMap = {
        white: { node_x: { status: 'partially_present', matched_plies: 2, total_plies: 5, new_plies: 3, message: EVIL } },
        black: {}
    };
    b.renderTreeLines();
    const badgeTree = q('repVariationsList').innerHTML;
    assertInert('overlap badges', badgeTree);
    t('SEC-J/overlap badges: the backend message is escaped exactly once (no double escaping)',
        badgeTree.indexOf('&amp;lt;img') === -1 && badgeTree.indexOf('&lt;img') !== -1,
        badgeTree.slice(0, 800));

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
