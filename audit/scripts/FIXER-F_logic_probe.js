#!/usr/bin/env node
/**
 * FIXER-F pure-logic probe — re-runnable, idempotent.
 *
 * Extracts the pure functions/methods under test out of frontend/board.js and
 * frontend/repertoire_builder.js by *evaluating the real source* (no copy-pasted reimplementation),
 * then asserts the behaviour the patches are supposed to guarantee.
 *
 * Usage:
 *   node audit/scripts/FIXER-F_logic_probe.js [--dir frontend]
 * Exits 0 on success, 1 on failure. Prints one PASS/FAIL line per check.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const dirIdx = process.argv.indexOf('--dir');
const FRONTEND = dirIdx !== -1 ? process.argv[dirIdx + 1] : 'frontend';

let failures = 0;
function check(name, cond, extra) {
    const ok = !!cond;
    if (!ok) failures++;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra !== undefined && !ok ? '  -> ' + extra : ''}`);
}

// ---------------------------------------------------------------------------
// 1. board.js: arrow validation + marker-id namespacing
// ---------------------------------------------------------------------------
const boardSrc = fs.readFileSync(path.join(FRONTEND, 'board.js'), 'utf8');

// Pull the arrow-mutator + geometry methods out of the real class body.
function grabMethod(name) {
    const re = new RegExp(`^    ${name}\\(([^)]*)\\)\\s*\\{`, 'm');
    const m = boardSrc.match(re);
    if (!m) return null;
    const start = m.index + m[0].length - 1;
    let depth = 0;
    for (let i = start; i < boardSrc.length; i++) {
        const c = boardSrc[i];
        if (c === '{') depth++;
        else if (c === '}') {
            depth--;
            if (depth === 0) return boardSrc.slice(m.index, i + 1);
        }
    }
    return null;
}

const pieces = ['_isValidArrow', '_isValidSquare', '_sanitizeArrowColor', 'addArrow', 'clearArrows', 'getArrows', 'setArrows', '_getSquareCenterPercent']
    .map(n => ({ n, src: grabMethod(n) }));
const missing = pieces.filter(p => !p.src).map(p => p.n);
check('board.js: arrow validation methods present', missing.length === 0, missing.join(','));

{
    // The static checks below must still run even when the methods are absent.
    const prefixDecl = boardSrc.match(/this\._markerPrefix\s*=\s*`([^`]+)`/);
    const markerUse = boardSrc.match(/const markerId = `\$\{this\._markerPrefix\}([^`]*)`/);
    check('board.js: marker id is namespaced per instance',
        !!prefixDecl && !!markerUse,
        `prefixDecl=${!!prefixDecl} markerUse=${!!markerUse}`);
    check('board.js: the old global "arrow-marker-${i}" id is gone',
        !/markerId = `arrow-marker-\$\{i\}`/.test(boardSrc));

    check('board.js: oncancel delivers the completion callback via the once-guard',
        /let settled = false;/.test(boardSrc) &&
        /anim\.oncancel = \(\) => \{[\s\S]*?finishOnce\(\);[\s\S]*?\n {12}\};/.test(boardSrc));

    check('board.js: dead _onSquareClick removed', !/_onSquareClick/.test(boardSrc));
    check('board.js: destroy() removes both window listeners',
        /destroy\(\)\s*\{[\s\S]*removeEventListener\('pointermove'/.test(boardSrc) &&
        /destroy\(\)\s*\{[\s\S]*removeEventListener\('pointerup'/.test(boardSrc));
    check('board.js: loadFEN supports keepArrows (no silent arrow loss)',
        /loadFEN\(fen, options = \{\}\)[\s\S]*keepArrows/.test(boardSrc));
    check('board.js: setOrientation cancels animations before rebuilding the square map',
        /setOrientation\(color\)\s*\{[\s\S]*?this\.stopAnimations\(\);[\s\S]*?this\._initDOM\(\);/.test(boardSrc));
    check('board.js: pointerup always clears the right-click anchor',
        /if \(e\.button === 2\) \{\s*const startSquare = this\._rightClickStartSquare;\s*this\._rightClickStartSquare = null;/.test(boardSrc));
}

if (missing.length === 0) {
    // Minimal harness: record what _renderArrows would have drawn.
    const drawn = [];
    const ctx = vm.createContext({
        console: { warn() {}, log() {}, error() {} },
    });
    // Build a real class from the extracted method sources so `this` semantics are authentic.
    vm.runInContext(
        'class __Extracted {\n' + pieces.map(p => p.src).join('\n') + '\n}\n__Extracted',
        ctx
    );
    const Extracted = vm.runInContext('__Extracted', ctx);
    const boardObj = new Extracted();
    boardObj.options = { orientation: 'white' };
    boardObj.arrows = [];
    boardObj._renderArrows = function () { drawn.length = 0; this.arrows.forEach(a => drawn.push([a.from, a.to])); };
    const boardProto = Extracted.prototype;

    check('setArrows([null]) keeps 0 arrows and does not throw',
        boardObj.setArrows([null]) === 0 && boardObj.arrows.length === 0);
    check('setArrows([{from:"e2",to:"j9"}]) rejects out-of-bounds "to"',
        boardObj.setArrows([{ from: 'e2', to: 'j9' }]) === 0);
    check('setArrows([{from:"i4",to:"e7"}]) rejects out-of-bounds "from"',
        boardObj.setArrows([{ from: 'i4', to: 'e7' }]) === 0);
    check('setArrows([{from:undefined,to:"e7"}]) rejects undefined from',
        boardObj.setArrows([{ from: undefined, to: 'e7' }]) === 0);
    check('setArrows(non-array) -> 0 arrows', boardObj.setArrows({ from: 'a1' }) === 0);
    check('setArrows keeps 2 valid entries', boardObj.setArrows([
        { from: 'e2', to: 'e4' }, { from: 'g1', to: 'f3' }
    ]) === 2);
    check('valid arrows survive into the render list',
        drawn.length === 2 && drawn[0][0] === 'e2' && drawn[0][1] === 'e4');
    check('setArrows normalises missing colour to the default',
        boardObj.arrows[0].color === '#22c55e');
    check('addArrow("e2","z9") is rejected (returns false, no state change)',
        boardObj.addArrow('e2', 'z9') === false && boardObj.arrows.length === 2);
    check('addArrow("e2","e4") is accepted (returns true, appended)',
        boardObj.addArrow('e2', 'e4') === true && boardObj.arrows.length === 3);

    // SEC-J: the persisted `color` is written into SVG presentation attributes and re-serialised
    // back to the DB, so only real paint values survive.
    check('setArrows keeps every legitimate paint value verbatim', [
        boardObj._sanitizeArrowColor('#22c55e'),
        boardObj._sanitizeArrowColor('#ABC'),
        boardObj._sanitizeArrowColor('#22c55eff'),
        boardObj._sanitizeArrowColor('rgb(1, 2, 3)'),
        boardObj._sanitizeArrowColor('rgba(1,2,3,0.5)')
    ].join(',') === '#22c55e,#ABC,#22c55eff,rgb(1, 2, 3),rgba(1,2,3,0.5)');
    boardObj.setArrows([{ from: 'e2', to: 'e4', color: 'url(javascript:alert(1))' }]);
    check('a non-paint arrow colour is replaced by the default',
        boardObj.arrows[0].color === '#22c55e', boardObj.arrows[0].color);
    boardObj.setArrows([{ from: 'e2', to: 'e4', color: 'red" onload="alert(1)' }]);
    check('a colour carrying an attribute break-out is replaced by the default',
        boardObj.arrows[0].color === '#22c55e', boardObj.arrows[0].color);

    const p = boardObj._getSquareCenterPercent('e2');
    check('_getSquareCenterPercent("e2") = {x:56.25,y:81.25}',
        Math.abs(p.x - 56.25) < 1e-9 && Math.abs(p.y - 81.25) < 1e-9, JSON.stringify(p));
    const bad = boardObj._getSquareCenterPercent(null);
    check('_getSquareCenterPercent(null) does not throw and degrades to centre',
        bad && bad.x === 50 && bad.y === 50, JSON.stringify(bad));
}

// ---------------------------------------------------------------------------
// 2. repertoire_builder.js: eval LRU + write latch + overlap generation
// ---------------------------------------------------------------------------
const rbSrc = fs.readFileSync(path.join(FRONTEND, 'repertoire_builder.js'), 'utf8');

// evalCache LRU
const lruSrc = rbSrc.match(/^    _evalCachePut\(([^)]*)\)\s*\{[\s\S]*?^    \}/m);
check('repertoire_builder.js: _evalCachePut present', !!lruSrc);
if (lruSrc) {
    const cache = { evalCache: new Map(), evalCacheMax: 3 };
    const fn = vm.runInNewContext(
        '({' + lruSrc[0].replace(/^    /gm, '') + '})._evalCachePut',
        { Map, console }
    );
    fn.call(cache, 'fen0', { score_cp: 0, depth: 10 });
    for (let i = 1; i < 5; i++) fn.call(cache, 'fen' + i, { score_cp: i, depth: 10 });
    check('evalCache is bounded to evalCacheMax', cache.evalCache.size === 3, cache.evalCache.size);
    check('evalCache evicts oldest first (LRU order preserved)',
        [...cache.evalCache.keys()].join(',') === 'fen2,fen3,fen4', [...cache.evalCache.keys()].join(','));
    fn.call(cache, 'fen4', { score_cp: 9, depth: 3 });
    check('evalCache never downgrades a deeper result',
        cache.evalCache.get('fen4').depth === 10, cache.evalCache.get('fen4').depth);
    fn.call(cache, 'newFen', { score_cp: 1, depth: 14 });
    check('evalCache write refreshes recency (moves key to the end)',
        [...cache.evalCache.keys()].slice(-1)[0] === 'newFen');
}

// write latch
const latchSrc = rbSrc.match(/^    beginWrite\(([^)]*)\)\s*\{[\s\S]*?^    \}/m);
const endSrc = rbSrc.match(/^    endWrite\(([^)]*)\)\s*\{[\s\S]*?^    \}/m);
check('repertoire_builder.js: beginWrite/endWrite latch present', !!latchSrc && !!endSrc);
if (latchSrc && endSrc) {
    const state = { writeInFlight: new Set() };
    const begin = vm.runInNewContext('({' + latchSrc[0].replace(/^    /gm, '') + '}).beginWrite', { Set });
    const end = vm.runInNewContext('({' + endSrc[0].replace(/^    /gm, '') + '}).endWrite', { Set });
    check('beginWrite: first call wins', begin.call(state, 'k') === true);
    check('beginWrite: second concurrent call is rejected', begin.call(state, 'k') === false);
    check('beginWrite: a different key is independent', begin.call(state, 'k2') === true);
    end.call(state, 'k');
    check('endWrite releases the key', begin.call(state, 'k') === true);
}

// overlap generation guard
const genSrc = rbSrc.match(/^    _isStaleOverlap\(([^)]*)\)\s*\{[\s\S]*?^    \}/m);
const beginOverlap = rbSrc.match(/^    _beginOverlapBatch\([\s\S]*?^    \}/m);
check('repertoire_builder.js: overlap generation + abort batch present', !!genSrc && !!beginOverlap);
if (genSrc && beginOverlap) {
    const state = { overlapGen: 3 };
    const isStale = vm.runInNewContext('({' + genSrc[0].replace(/^    /gm, '') + '})._isStaleOverlap', {});
    check('_isStaleOverlap(3) is false (this batch is current)', isStale.call(state, 3) === false);
    check('_isStaleOverlap(2) is true (a newer navigation happened)', isStale.call(state, 2) === true);
    check('checkTreeOverlap identifies lines by leaf-node id, not DFS index',
        /id: String\(l\.leafNode \? l\.leafNode\.id : ''\)/.test(rbSrc) &&
        !/id: String\(idx\)/.test(rbSrc));
}

// recent-games sequencing
check('loadRecentGames uses a sequence token',
    /const seq = \+\+this\.recentGamesSeq;/.test(rbSrc));
check('loadRecentGames aborts the previous request',
    /if \(this\.recentGamesAbort\) this\.recentGamesAbort\.abort\(\);/.test(rbSrc));
check('a stale recent-games response cannot write the DOM',
    (rbSrc.match(/if \(seq !== this\.recentGamesSeq\) return;/g) || []).length >= 2);

// candidate staleness guard must include colour + generation
check('candidate staleness guard compares colour AND generation',
    /const isStale = \(\) => \(\s*this\.activeColor !== reqColor \|\|\s*this\.candidateGen !== reqGen\s*\)/.test(rbSrc));

// eval cache key must be normalised
check('eval cache key is the normalised FEN (transpositions hit)',
    /const cacheKey = this\.normalizeFen\(fen\);[\s\S]{0,400}this\.evalCache\.has\(cacheKey\)/.test(rbSrc));

// renderEvalBar must actually use `depth`
check('renderEvalBar consumes the depth parameter (title/aria-label)',
    /renderEvalBar\(scoreCp, mate, depth\)[\s\S]{0,2000}labelEl\.title = title/.test(rbSrc));

// sync controllers must be independent
check('resyncGames and syncRecentGames use SEPARATE abort handles',
    /this\.resyncAbortController = abortCtrl/.test(rbSrc) &&
    /this\.recentSyncAbortController = abortCtrl/.test(rbSrc) &&
    /this\.syncAbortController = new AbortController\(\)/.test(rbSrc) === false);
check('each sync finally clears its own handle behind an identity check',
    (rbSrc.match(/=== abortCtrl\) \{/g) || []).length >= 2);
check('cancelSync aborts BOTH streams',
    /cancelSync\(\)\s*\{[\s\S]*resyncAbortController\.abort\(\)[\s\S]*recentSyncAbortController\.abort\(\)/.test(rbSrc));

// jumpToStep must not defer loadFEN into an animation callback
check('jumpToStep no longer calls loadFEN inside an animation callback',
    !/animateMove\([\s\S]{0,200}loadFEN\(/.test(rbSrc));
check('jumpToStep delegates to the single canonical render path',
    /this\.updatePosition\(\{ animateFromTo: \[from, to\] \}\);/.test(rbSrc));
check('updatePosition loads the FEN before restoring arrows',
    (function () {
        const m = rbSrc.match(/async updatePosition\([\s\S]*?\n    \}/);
        if (!m) return false;
        const i = m[0].indexOf('this.board.loadFEN(curr.fen)');
        const j = m[0].indexOf('this.renderMoveToolsPanel(curr)');
        return i !== -1 && j !== -1 && i < j;
    })());
check('the arrow counter is derived from what setArrows actually kept',
    /const kept = this\.board\.setArrows\(parsed\);/.test(rbSrc));

// promptStudySelection guard: every `await window.promptStudySelection(` must be preceded by a
// `typeof window.promptStudySelection !== 'function'` guard somewhere in the same function body.
const promptCalls = (rbSrc.match(/await window\.promptStudySelection\(/g) || []).length;
const promptGuards = (rbSrc.match(/typeof window\.promptStudySelection !== 'function'/g) || []).length;
check('every promptStudySelection call is behind a typeof guard',
    promptCalls > 0 && promptGuards >= promptCalls,
    `calls=${promptCalls} guards=${promptGuards}`);

// coverage gaps feature wiring
check('addressGap now has a real caller (coverage gap list)',
    /this\.addressGap\(gap\);/.test(rbSrc));
check('renderGapLists renders tree gaps in repCoverageGapsList',
    /repCoverageGapsList/.test(rbSrc));

// dead code removal
check('dead showNotice alias removed', !/\n    showNotice\(/.test(rbSrc));

console.log(`\n${failures === 0 ? 'ALL CHECKS PASSED' : failures + ' CHECK(S) FAILED'} (frontend dir: ${FRONTEND})`);
process.exit(failures === 0 ? 0 : 1);
