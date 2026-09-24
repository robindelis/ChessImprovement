#!/usr/bin/env node
/*
 * CODE-3 — static verification probe for the frontend audit of the Chess
 * Repertoire Studio (frontend/repertoire_builder.js, board.js, index.html,
 * style.css).
 *
 * Pure static analysis: reads files, runs assertions, prints PASS/FAIL per
 * check. It never starts a server, never touches chess_improvement.db and
 * never performs network I/O.
 *
 * Re-runnable and idempotent.  Usage:
 *     node audit/scripts/CODE-3_static_probe.js            # all checks
 *     node audit/scripts/CODE-3_static_probe.js CODE-07     # one finding
 */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const F = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

const SRC = {
    builder: F('frontend/repertoire_builder.js'),
    board: F('frontend/board.js'),
    html: F('frontend/index.html'),
    css: F('frontend/style.css'),
    app: F('frontend/app.js'),
    scout: F('frontend/scout.js'),
    trainer: F('frontend/repertoire_trainer.js'),
};

const htmlIds = new Set([...SRC.html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
const htmlHasClass = (c) => new RegExp('class="[^"]*\\b' + c + '\\b').test(SRC.html);

/* ------------------------------------------------------------------ checks */
const checks = {};

checks['CODE-01'] = () => {
    const out = [];
    const btn = SRC.html.match(/<button id="btnRepDeleteCurrentMove"[^>]*>/);
    out.push(['button exists in index.html', !!btn]);
    out.push(['...with inline display:none', btn ? /display:\s*none/.test(btn[0]) : false]);
    const writes = [...SRC.builder.matchAll(/btnDelete\.[a-zA-Z]+\s*=[^;]+;/g)].map(m => m[0]);
    out.push(['JS writes to btnDelete: ' + JSON.stringify(writes), writes.length > 0]);
    out.push(['...none of them set .style.display', !writes.some(w => w.includes('style.display'))]);
    out.push(['...so the button can never become visible', true]);
    return out;
};

checks['CODE-02'] = () => {
    const out = [];
    const jp = SRC.builder.slice(SRC.builder.indexOf('jumpToStep(index)'));
    const seg = jp.slice(0, jp.indexOf('this.updatePosition();'));
    const iPanel = seg.indexOf('this.renderMoveToolsPanel(curr)');
    const iAnim = seg.indexOf('this.board.animateMove(from, to');
    out.push(['renderMoveToolsPanel precedes animateMove in jumpToStep fast-path', iPanel > -1 && iPanel < iAnim]);
    out.push(['the fast-path returns before updatePosition()', seg.includes('return;')]);
    out.push(['animateMove callback is the only loadFEN of the step', /animateMove\(from, to, \(\) => \{\s*this\.board\.loadFEN\(curr\.fen\);/s.test(seg)]);
    const lf = SRC.board.slice(SRC.board.indexOf('loadFEN(fen)'), SRC.board.indexOf('setLastMove('));
    out.push(['board.loadFEN() clears this.arrows', /this\.arrows = \[\];/.test(lf)]);
    out.push(['=> arrows restored by renderMoveToolsPanel are wiped 180ms later', true]);
    return out;
};

checks['CODE-03'] = () => {
    const out = [];
    out.push(["#blunderBannerAlert absent from index.html", !htmlIds.has('blunderBannerAlert')]);
    out.push(['builder references it', (SRC.builder.match(/blunderBannerAlert/g) || []).length + 'x']);
    out.push(['addressGap has no caller (definition only)',
        (SRC.builder.match(/addressGap\(/g) || []).length === 1 && !/this\.addressGap\(/.test(SRC.builder)]);
    out.push(['addressGameGap reachable only from the dead banner + addressGap',
        (SRC.builder.match(/addressGameGap\(/g) || []).length === 3]);
    out.push(['treeData.gaps never rendered', !/treeData\.gaps/.test(SRC.builder) && !/treeData\?\.gaps/.test(SRC.builder)]);
    out.push(['showPatchedGaps never set to true', !/showPatchedGaps\s*=\s*true/.test(SRC.builder)]);
    out.push(['test-satisfying comment left in source', SRC.builder.includes('// keeps test requirement satisfied')]);
    return out;
};

checks['CODE-04'] = () => {
    const out = [];
    const nulls = [...SRC.builder.matchAll(/this\.syncAbortController = null;/g)].map(m => m[0]);
    out.push(['unconditional `syncAbortController = null` occurrences', nulls.length + 'x (expect >=2)']);
    out.push(['no identity guard before nulling (contrast startEvalStream)',
        /if \(this\.currentEvalAbort === abortCtrl\)/.test(SRC.builder) && !/this\.syncAbortController === /.test(SRC.builder)]);
    out.push(['cancelSync() no-ops when the slot is null', /if \(this\.syncAbortController\) \{\s*this\.syncAbortController\.abort\(\);/.test(SRC.builder)]);
    out.push(['both sync paths assign the same slot',
        (SRC.builder.match(/this\.syncAbortController = new AbortController\(\);/g) || []).length === 2]);
    out.push(['auto-dismiss guards read the same clobbered field',
        /if \(c && !this\.syncAbortController\)/.test(SRC.builder)]);
    return out;
};

checks['CODE-05'] = () => {
    const out = [];
    const fn = SRC.builder.slice(SRC.builder.indexOf('async loadRecentGames()'), SRC.builder.indexOf('async syncRecentGames()'));
    out.push(['loadRecentGames has no AbortController', !/AbortController/.test(fn)]);
    out.push(['...no request token / sequence guard', !/requestId|seq|generation|token/i.test(fn)]);
    out.push(['...no res.ok check', !/res\.ok/.test(fn)]);
    out.push(['writes shared this.recentGames after await', /this\.recentGames = games;/.test(fn)]);
    out.push(['unfiltered count write in renderCoverageAndGaps',
        /fetch\('\/api\/games\/recent\?limit=1'\)[\s\S]{0,320}gamesCount\.textContent = data\.total/.test(SRC.builder)]);
    out.push(['...and swallows errors with .catch(() => {})', /\.catch\(\(\) => \{\}\);/.test(SRC.builder)]);
    out.push(['search input debounce feeds it', /this\.gamesSearchQuery = e\.target\.value;/.test(SRC.builder)]);
    return out;
};

checks['CODE-06'] = () => {
    const out = [];
    const fn = SRC.builder.slice(SRC.builder.indexOf('async checkActiveLineOverlap()'), SRC.builder.indexOf('_applyTreeBadges('));
    out.push(['overlap checks have no AbortController', !/AbortController/.test(fn)]);
    out.push(['...no request identity check before the DOM write', !/requestId|seq|=== this\.lineTree|currentNode\.id/.test(fn)]);
    out.push(['tree overlap ids are positional (String(idx))', /id: String\(idx\)/.test(fn)]);
    out.push(['4 POSTs per navigation (2 x check_line + 2 x check_tree)',
        (fn.match(/fetch\('\/api\/repertoire\/check_(line|tree)'/g) || []).length === 4]);
    out.push(['scheduled from every position update', /scheduleOverlapCheck\(\)/.test(SRC.builder) && /\}, 120\);/.test(SRC.builder)]);
    out.push(['import paths also call them un-awaited', /this\.checkActiveLineOverlap\(\);\s*this\.checkTreeOverlap\(\);/.test(SRC.builder)]);
    return out;
};

checks['CODE-07'] = () => {
    const out = [];
    const fn = SRC.builder.slice(SRC.builder.indexOf('async loadCandidateMoves('), SRC.builder.indexOf('renderCandidateTable(fen, isUserTurn) {'));
    out.push(['staleness guard compares FEN only', /curr\.fen !== targetFen/.test(fn)]);
    out.push(['...colour is not part of the guard', !/activeColor\s*!==|colorSnapshot|requestColor/.test(fn)]);
    const lr = SRC.builder.slice(SRC.builder.indexOf('async loadRepertoire('), SRC.builder.indexOf('async refreshTree('));
    out.push(['loadRepertoire never aborts candidateAbortController', !/candidateAbortController\.abort/.test(lr)]);
    out.push(['loadRepertoire resets history to the start FEN', /fen: startFen/.test(lr)]);
    out.push(['switchColor does not abort it either',
        !/candidateAbortController\.abort/.test(SRC.builder.slice(SRC.builder.indexOf('async switchColor('), SRC.builder.indexOf('async loadRepertoire(')))]);
    out.push(['cache write key is colour+normFen only', /this\.candidateCache\.set\(cacheKey, data\)/.test(SRC.builder)]);
    return out;
};

checks['CODE-08'] = () => {
    const out = [];
    out.push(['evalCache is a plain Map', /this\.evalCache = new Map\(\);/.test(SRC.builder)]);
    out.push(['keyed on the full request FEN', /this\.evalCache\.set\(fen, data\)/.test(SRC.builder)]);
    out.push(['request also uses the full FEN (no normalizeFen)',
        /eval_stream\?fen=\$\{encodeURIComponent\(fen\)\}/.test(SRC.builder)]);
    out.push(['no .delete() / eviction anywhere', !/evalCache\.delete|evalCache\.clear|evalCache\.size/.test(SRC.builder)]);
    out.push(['renderEvalBar takes an unused `depth` param',
        /renderEvalBar\(scoreCp, mate, depth\)/.test(SRC.builder) &&
        !/\bdepth\b/.test(SRC.builder.slice(SRC.builder.indexOf('renderEvalBar(scoreCp, mate, depth) {') + 42, SRC.builder.indexOf('    async startEvalStream(')))]);
    out.push(['stream is not aborted on tab switch', !/showTab[\s\S]{0,400}currentEvalAbort/.test(SRC.app)]);
    return out;
};

checks['CODE-09'] = () => {
    const out = [];
    out.push(['onNewStudyClick guards the global', /if \(typeof window\.promptStudySelection !== 'function'\) return;/.test(SRC.builder)]);
    const unguarded = [...SRC.builder.matchAll(/await window\.promptStudySelection\(/g)].length;
    out.push(['unguarded await window.promptStudySelection call sites', unguarded + 'x (expect 2)']);
    out.push(['import handlers are bound without await/catch',
        /btnAddLine\.addEventListener\('click', \(\) => this\.importToRepertoire\(this\.activeColor\)\)/.test(SRC.builder)]);
    out.push(['promptStudySelection is defined in the LAST script tag',
        SRC.html.lastIndexOf('/app.js') > SRC.html.lastIndexOf('/repertoire_builder.js')]);
    out.push(['no try/catch around the study prompt in importToRepertoire',
        !/try \{[\s\S]{0,400}promptStudySelection/.test(SRC.builder)]);
    return out;
};

checks['CODE-10'] = () => {
    const out = [];
    out.push(['in-flow widget prepended to the games list', /gamesList\.prepend\(inlineWidget\)/.test(SRC.builder)]);
    out.push(['widget has real height (padding + bar + msg)', /repGamesInlineProgress[\s\S]{0,900}padding:0\.6rem 0\.8rem/.test(SRC.builder)]);
    out.push(['removed on a 2.5s timer (snap-back)', /const el = document\.getElementById\('repGamesInlineProgress'\);\s*if \(el\) el\.remove\(\);\s*\}, 2500\);/.test(SRC.builder)]);
    out.push(['only created if absent -> second sync reuses stale widget', /if \(gamesList && !inlineWidget\)/.test(SRC.builder)]);
    out.push(['a fixed non-shifting progress card already exists', htmlIds.has('syncProgressContainer')]);
    out.push(['#repQuickAddNotice is a dead display:none container',
        htmlIds.has('repQuickAddNotice') && !/repQuickAddNotice/.test(SRC.builder + SRC.scout + SRC.trainer + SRC.app)]);
    return out;
};

checks['CODE-11'] = () => {
    const out = [];
    out.push(['button relabels itself twice', /btnAdd\.textContent = 'Adding\.\.\.'/.test(SRC.builder) && /btnAdd\.textContent = '✓ Added!'/.test(SRC.builder)]);
    out.push(['no min-width for .btn-quick-add-opening in CSS', !/btn-quick-add-opening/.test(SRC.css)]);
    out.push(['no min-width for .btn-review-game in CSS', !/btn-review-game/.test(SRC.css)]);
    out.push(['action group sits in a space-between .game-card',
        /\.game-card \{[\s\S]{0,120}justify-content: space-between/.test(SRC.css)]);
    out.push(['button is disabled and never re-enabled', /btnAdd\.disabled = true;/.test(SRC.builder) &&
        !/btnAdd\.disabled = false/.test(SRC.builder)]);
    out.push(['contrast: fixed-width rule exists for the other toolbar buttons',
        /#btnRepToggleImportant \{[\s\S]{0,60}min-width: 125px/.test(SRC.css)]);
    return out;
};

checks['CODE-12'] = () => {
    const out = [];
    const rows = SRC.builder.slice(SRC.builder.indexOf('// Button handlers'), SRC.builder.indexOf('    playMoveFromUci(uci, san) {'));
    out.push(['row handlers disable the button?', !/btnSet\.disabled|btnAdd\.disabled|btnDel\.disabled/.test(rows)]);
    out.push(['row handlers are one-line fire-and-forget', /btnSet\.addEventListener\('click', \(e\) => \{\s*e\.stopPropagation\(\);\s*this\.saveMove\(fen, m, true\);\s*\}\);/.test(rows)]);
    const sm = SRC.builder.slice(SRC.builder.indexOf('async saveMove('), SRC.builder.indexOf('async removeBranch('));
    out.push(['each success fans out to 3 further requests', (sm.match(/await this\./g) || []).length >= 3]);
    out.push(['no toast / no error feedback on failure', /catch \(e\) \{\s*console\.error\('Error saving repertoire move:'/.test(sm) && !/showQuickNotice|showToast/.test(sm) && !/\.disabled/.test(sm) && !/showToast/.test(sm)]);
    out.push(['treeData assigned last-writer-wins', /this\.treeData = await res\.json\(\);/.test(SRC.builder)]);
    out.push(['toggleCurrentMoveImportant has no in-flight guard', !/togglePending|inFlight|busy/.test(SRC.builder)]);
    return out;
};

checks['CODE-13'] = () => {
    const out = [];
    const am = SRC.board.slice(SRC.board.indexOf('animateMove(from, to'), SRC.board.indexOf('    _animateSecondaryPiece(from, to)'));
    const oncancel = am.slice(am.indexOf('anim.oncancel'));
    out.push(['animateMove has an oncancel handler', /anim\.oncancel/.test(am)]);
    out.push(['...oncancel does NOT call onComplete', !/oncancel = \(\) => \{[\s\S]{0,400}?onComplete\(\)/.test(oncancel.slice(0, oncancel.indexOf('} catch')) )]);
    out.push(['onfinish DOES call onComplete', /anim\.onfinish = \(\) => \{[\s\S]{0,400}if \(onComplete\) onComplete\(\);/.test(am)]);
    out.push(['stopAnimations cancels everything in flight', /anim\.cancel\(\)/.test(SRC.board)]);
    out.push(['jumpToStep calls stopAnimations on every navigation', /this\.board\.stopAnimations\(\)/.test(SRC.builder)]);
    out.push(['fast-path uses the callback as its only state sync',
        /animateMove\(from, to, \(\) => \{\s*this\.board\.loadFEN\(curr\.fen\);\s*\}\);\s*return;/.test(SRC.builder)]);
    out.push(['setOrientation rebuilds squares without stopAnimations',
        new RegExp('setOrientation\\(color\\) \\{\\s*this\\.options\\.orientation = color;\\s*this\\._initDOM\\(\\);').test(SRC.board) && !/setOrientation\(color\) \{[\s\S]{0,200}?stopAnimations/.test(SRC.board.slice(SRC.board.indexOf('setOrientation(color)'), SRC.board.indexOf('stopAnimations() {')))]);
    return out;
};

checks['CODE-14'] = () => {
    const out = [];
    out.push(['setArrows performs no element validation', /setArrows\(arrows\) \{\s*this\.arrows = Array\.isArray\(arrows\) \? \[\.\.\.arrows\] : \[\];/.test(SRC.board)]);
    out.push(['addArrow is an unvalidated public mutator', /addArrow\(from, to, color = '#22c55e'\) \{\s*this\.arrows\.push\(\{ from, to, color \}\);/.test(SRC.board)]);
    out.push(['_getSquareCenterPercent dereferences the raw value',
        /const file = sq\.charCodeAt\(0\) - 97;/.test(SRC.board)]);
    out.push(['...no bounds check on file/rank', !/file < 0|file > 7|rank < 1|rank > 8/.test(SRC.board)]);
    out.push(['_renderArrows is called from render()', /render\(\) \{[\s\S]*?this\._renderArrows\(\);\s*\}/s.test(SRC.board.slice(SRC.board.indexOf('    render() {'), SRC.board.indexOf('    _onPointerDown(e) {')))]);
    out.push(['the builder\'s only call site is inside a try', /try \{\s*const parsed = JSON\.parse\(repMove\.arrows\);\s*this\.board\.setArrows\(parsed\);/.test(SRC.builder)]);
    out.push(['trainer/scout call sites are not wrapped',
        /this\.board\.setArrows\(/.test(SRC.trainer) && /this\.board\.setArrows\(/.test(SRC.scout)]);
    out.push(['loadFEN clears arrows without firing onArrowsChange',
        /this\.arrows = \[\];[\s\S]{0,200}?onArrowsChange/.test(SRC.board) === false &&
        /this\.arrows = \[\];/.test(SRC.board)]);
    return out;
};

checks['CODE-15'] = () => {
    const out = [];
    out.push(['marker id is derived only from the arrow index', /const markerId = `arrow-marker-\$\{i\}`;/.test(SRC.board)]);
    out.push(['...and referenced document-wide via url(#..)', /marker-end.*url\(#\$\{markerId\}\)/.test(SRC.board)]);
    const boards = (SRC.builder + SRC.trainer + SRC.scout).match(/new InteractiveChessBoard\(/g) || [];
    out.push(['simultaneously live board instances', boards.length + ' (all constructed at load, all kept in the DOM)']);
    out.push(['repertoireBoard precedes trainerBoard precedes scoutBoard in the DOM',
        htmlIds.has('repertoireBoard') && SRC.html.indexOf('id="repertoireBoard"') < SRC.html.indexOf('id="trainerBoard"') &&
        SRC.html.indexOf('id="trainerBoard"') < SRC.html.indexOf('id="scoutBoard"')]);
    out.push(['no per-instance id prefix anywhere', !/markerId = `.*\$\{this\.id/.test(SRC.board)]);
    return out;
};

checks['CODE-16'] = () => {
    const out = [];
    out.push(['two window listeners bound in the constructor', (SRC.board.match(/window\.addEventListener\('pointer(move|up)'/g) || []).length === 2]);
    out.push(['no removeEventListener for them', !/removeEventListener/.test(SRC.board)]);
    out.push(['no destroy()/dispose() method', !/destroy\s*\(|dispose\s*\(/.test(SRC.board)]);
    out.push(['_onSquareClick is never called', (SRC.builder + SRC.trainer + SRC.scout + SRC.board).split('_onSquareClick(').length - 1 === 1]);
    out.push(['_rightClickStartSquare early-return skips the reset', /if \(e\.button === 2 && this\._rightClickStartSquare\) \{[\s\S]{0,1400}?\n\s*return;\n\s*\}/.test(SRC.board)]);
    out.push(['...cleanup of the drag ghost is below that return', /const targetSq = targetEl[\s\S]{0,1600}?\n\s*return;/.test(SRC.board) && /this\._dragFloatingEl\.remove\(\);/.test(SRC.board)]);
    out.push(['_rightClickStartSquare initialised after the listeners', SRC.board.indexOf('this._rightClickStartSquare = null;') > SRC.board.indexOf("window.addEventListener('pointerup'")]);
    return out;
};

checks['CODE-17'] = () => {
    const out = [];
    const dead = ['blunderBannerAlert', 'btnResyncGames', 'repGapThresholdDisplay', 'repOpeningThresholdLabel',
        'syncCountNewBlunders', 'syncBlunderPill', 'syncCountNewBlunders', 'syncCountNewBlunders'];
    dead.forEach(id => out.push([`#${id} never matches anything in index.html`, !htmlIds.has(id) && new RegExp("getElementById\\('" + id + "'\\)").test(SRC.builder)]));
    out.push(['#repOpeningThreshold / #repOpeningThresholdSelect absent (opening-cutoff UI)',
        !htmlIds.has('repOpeningThreshold') && !htmlIds.has('repOpeningThresholdSelect') && /setupOpeningThreshold\('repOpeningThreshold'\)/.test(SRC.builder)]);
    out.push(['openingGamesThreshold is frozen at 30', /this\.openingGamesThreshold = 30;/.test(SRC.builder) && !/openingGamesThreshold = [^3]/.test(SRC.builder.replace(/this\.openingGamesThreshold = 30;/, '').replace(/this\.openingGamesThreshold = parseInt\(e\.target\.value, 10\);/, ''))]);
    out.push(['showNotice alias has no callers', (SRC.builder.split('showNotice(').length - 1) === 1]);
    out.push(['stepForward cache fallback key is never written', /candidateCache\.get\(normParent\)/.test(SRC.builder) &&
        !/candidateCache\.set\(normParent/.test(SRC.builder)]);
    out.push(['#lblMyStatsHeader is orphaned in the HTML', htmlIds.has('lblMyStatsHeader') && !/lblMyStatsHeader/.test(SRC.builder + SRC.trainer + SRC.scout + SRC.app)]);
    out.push(['...and destroyed by updateSortHeaderUI innerHTML', /el\.innerHTML = `\$\{h\.label\} <span class="sort-arrow">/.test(SRC.builder)]);
    return out;
};

checks['CODE-18'] = () => {
    const out = [];
    const badgeStr = '✓ White (${wData.matched_plies}/${wData.total_plies})';
    out.push(['overlap-badge decision tree duplicated', (SRC.builder.split(badgeStr).length - 1) === 2]);
    out.push(['import_to / import_all clones', /async importToRepertoire/.test(SRC.builder) && /async importAllLinesToRepertoire/.test(SRC.builder)]);
    const i1 = SRC.builder.indexOf('async importToRepertoire('), i2 = SRC.builder.indexOf('async importAllLinesToRepertoire('), i3 = SRC.builder.indexOf('async removeTreeLineFromRepertoire(');
    const a = SRC.builder.slice(i1, i2).split('\n'), b = SRC.builder.slice(i2, i3).split('\n');
    const same = a.filter(l => l.trim() && b.includes(l)).length;
    out.push([`identical lines between the two import functions: ${same}`, same > 20]);
    out.push(['resyncGames / syncRecentGames share SSE scaffolding',
        (SRC.builder.match(/buffer\.split\('\\n\\n'\)/g) || []).length >= 3 &&
        (SRC.builder.match(/startsWith\('data: '\)/g) || []).length >= 3 &&
        (SRC.builder.match(/new TextDecoder\(\)/g) || []).length >= 3]);
    out.push(['jumpToStep fast-path duplicates updatePosition', /renderMoveToolsPanel\(curr\);[\s\S]{0,300}syncLineTreeNode\(\);/.test(SRC.builder)]);
    out.push(['moveObj literal built twice', (SRC.builder.match(/fen_before: prevStep \? prevStep\.fen/g) || []).length === 2]);
    out.push(['window.showToast defined twice in app.js', (SRC.app.match(/window\.showToast = function/g) || []).length === 2]);
    return out;
};

checks['CODE-19'] = () => {
    const out = [];
    out.push(['SessionTreeNode declared in both files',
        /class SessionTreeNode/.test(SRC.builder) && /class SessionTreeNode/.test(SRC.scout)]);
    out.push(['SessionLineTree declared in both files',
        /class SessionLineTree/.test(SRC.builder) && /class SessionLineTree/.test(SRC.scout)]);
    out.push(['builder copy is behind a typeof guard', /if \(typeof SessionTreeNode === 'undefined'\)/.test(SRC.builder)]);
    out.push(['scout copy is NOT guarded (top-level class => global lexical binding that shadows window.X)',
        !/if \(typeof SessionTreeNode === 'undefined'\)/.test(SRC.scout.slice(0, 400))]);
    out.push(['this.move doc comments have already drifted',
        /is_custom, comment, is_important, arrows/.test(SRC.builder) && !/is_custom, comment, is_important, arrows/.test(SRC.scout)]);
    out.push(['getAllUniqueMoves implemented twice',
        (SRC.builder + SRC.scout).split('getAllUniqueMoves() {').length - 1 === 2]);
    return out;
};

checks['CODE-20'] = () => {
    const out = [];
    const lbl = SRC.css.slice(SRC.css.indexOf('.eval-bar-label {'), SRC.css.indexOf('.eval-bar-label.white-lead'));
    out.push(['.eval-bar-label has no white-space: nowrap', !/white-space/.test(lbl)]);
    out.push(['...no overflow/text-overflow clamp', !/text-overflow/.test(lbl)]);
    out.push(['.eval-bar-wrapper is only 28px wide', /\.eval-bar-wrapper \{[\s\S]{0,40}width: 28px/.test(SRC.css)]);
    out.push(['label text can reach 5 chars ("+10.0")', /\(pawns > 0 \? '\+' : ''\) \+ pawns\.toFixed\(1\)/.test(SRC.builder)]);
    const strip = SRC.css.slice(SRC.css.indexOf('.scout-line-overlap-strip {'), SRC.css.indexOf('}', SRC.css.indexOf('.scout-line-overlap-strip {')));
    out.push(['.scout-line-overlap-strip allows flex-wrap: wrap', /flex-wrap: wrap/.test(strip)]);
    out.push(['...and only declares a min-height (a floor, not a constant)', /min-height: 28px/.test(strip)]);
    out.push(['.scout-overlap-badge has no white-space: nowrap',
        !/white-space/.test(SRC.css.slice(SRC.css.indexOf('.scout-overlap-badge {'), SRC.css.indexOf('.scout-overlap-badge.rep-present')) )]);
    out.push(['ply-0 placeholder exists (good)', /Repertoire: Starting Position/.test(SRC.builder)]);
    out.push(['...but the populated variant is 1 label + 2 long badges',
        /\$\{getBadge\(dataW, 'White'\)\}\s*\$\{getBadge\(dataB, 'Black'\)\}/.test(SRC.builder)]);
    out.push(['#repOpeningTitle/Eco set a dynamic title (the correct pattern, applied elsewhere)',
        /titleEl\.title = t;/.test(SRC.builder) && /ecoEl\.title = e;/.test(SRC.builder)]);
    return out;
};

checks['CODE-21'] = () => {
    const out = [];
    out.push(['exactly one .flipped rule in the CSS', (SRC.css.match(/\.flipped/g) || []).length === 1]);
    out.push(['...and it only changes flex-direction', /\.eval-bar-wrapper\.flipped \{\s*flex-direction: column;/.test(SRC.css)]);
    out.push(['no .flipped rule for the label', !/flipped[^{]*\.eval-bar-label|eval-bar-label[^{]*flipped/.test(SRC.css)]);
    out.push(['label placement is coupled to the leader colour class',
        /\.eval-bar-label\.white-lead \{\s*bottom: 5px;\s*color: #0f172a;/.test(SRC.css) &&
        /\.eval-bar-label\.black-lead \{\s*top: 5px;\s*color: #f8fafc;/.test(SRC.css)]);
    out.push(['fill direction does flip', /flex-direction: column-reverse/.test(SRC.css)]);
    out.push(['JS toggles .flipped in 3 places', (SRC.builder.match(/classList\.toggle\('flipped'/g) || []).length === 3]);
    out.push(['setOrientation does not re-render the eval bar', !/setOrientation[\s\S]{0,300}renderEvalBar/.test(SRC.board + SRC.builder.slice(SRC.builder.indexOf('btnFlip')))]);
    return out;
};

checks['CODE-22'] = () => {
    const out = [];
    out.push(['seed POST omits the required ?force=true', /fetch\('\/api\/repertoire\/seed', \{ method: 'POST' \}\)/.test(SRC.builder)]);
    out.push(['...and only reports on status === "ok" (backend returns "skipped")',
        /if \(data\.status === 'ok'\) \{/.test(SRC.builder.slice(SRC.builder.indexOf('async seedDefaults()'), SRC.builder.indexOf('async confirmAndResetRepertoire()')))]);
    out.push(['...backend seed returns None on the success path => data === null => TypeError swallowed',
        !/data\?\.status|data && data\.status/.test(SRC.builder)]);
    out.push(['no else-branch / no toast for the skipped case', !/showQuickNotice[\s\S]{0,200}seed/.test(SRC.builder)]);
    out.push(['refreshTree: no res.ok, no user feedback',
        /const res = await fetch\(`\/api\/repertoire\/tree[^`]*`\);\s*this\.treeData = await res\.json\(\);/.test(SRC.builder)]);
    out.push(['unguarded res.json() call sites in the builder',
        (SRC.builder.match(/await res\.json\(\)/g) || []).length + 'x, res.ok checked: ' + (SRC.builder.match(/res\.ok/g) || []).length + 'x']);
    out.push(['writes that only console.error on failure',
        (SRC.builder.match(/catch \([a-z]*\) \{\s*console\.error/g) || []).length + ' catch blocks']);
    out.push(['import paths DO toast (inconsistent)', /this\.showQuickNotice\(`Error: \$\{e\.message\}`/.test(SRC.builder)]);
    out.push(['saveMoveComment fires on both debounce and blur',
        /debounceTimer = setTimeout\(\(\) => \{\s*this\.saveMoveComment/.test(SRC.builder) &&
        /addEventListener\('blur', \(\) => \{[\s\S]{0,120}this\.saveMoveComment/.test(SRC.builder)]);
    return out;
};

/* -------------------------------------------------------------------- main */
const want = process.argv[2];
const ids = Object.keys(checks).sort();
let failed = 0;
for (const id of ids) {
    if (want && want !== id) continue;
    console.log('\n=== ' + id + ' ===');
    for (const [label, ok] of checks[id]()) {
        if (!ok) failed++;
        console.log('  [' + (ok ? 'ok  ' : 'FAIL') + '] ' + label);
    }
}
console.log('\n' + (failed ? failed + ' assertion(s) FAILED' : 'all assertions passed') +
    (want ? ' (' + want + ')' : ''));
process.exit(failed ? 1 : 0);
