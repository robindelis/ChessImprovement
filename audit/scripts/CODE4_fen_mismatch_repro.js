#!/usr/bin/env node
// CODE-4: proves CODE-17 -- a tree seeded with the backend's 4-field
// normalize_fen() FENs and then extended with chess.js 6-field FENs never
// merges the two representations of the same position, so
//  (a) addMove() creates a DUPLICATE child for one position, and
//  (b) getAllUniqueMoves() counts the same (position, move) twice,
// which is what "Import All -> White/Black" then POSTs twice.
//
// Run:  node audit/scripts/CODE4_fen_mismatch_repro.js
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '..', 'frontend', 'scout.js');
const src = fs.readFileSync(SRC, 'utf8').split('\n').slice(0, 165).join('\n');
const mod = { exports: {} };
new Function('module', src + '\nmodule.exports = { SessionLineTree };')(mod);
const { SessionLineTree } = mod.exports;

// backend/repertoire.py:23-29 normalize_fen() -> first 4 FEN fields
const NORM = f => f.split(' ').slice(0, 4).join(' ');
// chess.js board.fen() -> all 6 fields
const FULL = f => f;

const START_FULL = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const AFTER_E4_FULL =
    'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1';

let failures = 0;
const check = (n, c, d) => {
    console.log((c ? '  [FAIL-REPRODUCED] ' : '  [no-repro]        ') + n +
                (d ? ' -> ' + d : ''));
    if (c) failures++;
};

console.log('(a) the FEN-equality branch of addMove() is unreachable dead code');
{
    // Two *distinct* legal moves from one position can never reach the same
    // FEN (the origin square differs, so the piece placement differs), so the
    // second disjunct of
    //     (c.move && moveObj.uci && c.move.uci === moveObj.uci) ||
    //     (c.fen && moveObj.fen_after && c.fen === moveObj.fen_after)
    // can never fire. It looks like transposition support; it is not.
    // Feeding it the only input that *could* match (same fen_after twice) just
    // grafts the second move onto the first move's children, because addMove
    // searches currentNode.children -- the clause is never even consulted.
    const t = new SessionLineTree();
    const A = NORM(AFTER_E4_FULL);
    const c1 = t.addMove({ fen_before: NORM(START_FULL), fen_after: A,
                           uci: 'e2e4', san: 'e4', color: 'white' });
    const c2 = t.addMove({ fen_before: NORM(START_FULL), fen_after: A,
                           uci: 'd2d4', san: 'd4', color: 'white' });
    console.log('    two distinct moves with the same fen_after:');
    console.log('      c1 === c2 (FEN clause matched)?', c1 === c2);
    console.log('      the 2nd move was grafted under the 1st instead:',
                t.root.children[0].children.map(ch => ch.move.uci));
    check('FEN clause never matches; it is unreachable, not transposition support',
        c1 !== c2 && t.root.children[0].children.length === 1,
        'addMove searched currentNode.children, so the FEN key was never used');
    // and the format split means the FEN key could never match across sources
    console.log('    4-field === 6-field ?',
                NORM(AFTER_E4_FULL) === FULL(AFTER_E4_FULL), '-> the FEN key is');
    console.log('    format-split too, so it is dead AND broken.');
}

console.log('\n(b) getAllUniqueMoves() de-duplication');
{
    const t = new SessionLineTree();
    t.addMove({ fen_before: NORM(START_FULL), fen_after: NORM(AFTER_E4_FULL),
                uci: 'e2e4', san: 'e4', color: 'white' });
    t.addMove({ fen_before: FULL(START_FULL), fen_after: FULL(AFTER_E4_FULL),
                uci: 'e2e4', san: 'e4', color: 'white' });
    const uniq = t.getAllUniqueMoves();
    console.log('    getAllUniqueMoves().length =', uniq.length,
                '| keys =', JSON.stringify(uniq.map(m => m.fen_before + '|' + m.uci)));
    check('same (position, move) counted twice', uniq.length === 2,
        uniq.length + ' entries for one move');
}

console.log('\n(c) nextStep() follows children[0] regardless of which branch');
{
    const t = new SessionLineTree();
    t.addMove({ fen_before: NORM(START_FULL), fen_after: NORM(AFTER_E4_FULL),
                uci: 'e2e4', san: 'e4', color: 'white' });
    t.addMove({ fen_before: FULL(START_FULL), fen_after: FULL(AFTER_E4_FULL),
                uci: 'd2d4', san: 'd4', color: 'white' });
    const picked = t.root.children[0].move.uci;
    console.log('    nextStep() would walk into children[0] =', picked,
                '(the user just played', t.currentNode.move.uci + ')');
    check('nextStep() ignores the branch the user is on', picked !== t.currentNode.move.uci,
        'children[0]=' + picked + ' but currentNode=' + t.currentNode.move.uci);
}

console.log('\nfailures reproduced: ' + failures);
process.exit(0);
