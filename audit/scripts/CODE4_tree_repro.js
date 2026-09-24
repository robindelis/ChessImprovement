#!/usr/bin/env node
// CODE-4 repro harness: extracts the *verbatim* SessionTreeNode / SessionLineTree
// classes from frontend/scout.js (lines 1-165) and exercises them under node.
// Idempotent, read-only, touches no database and no live server.
//
// Run:  node audit/scripts/CODE4_tree_repro.js
//
// Proves:
//   (A) _buildPgn() emits invalid PGN "e5 2. d4" for a line that starts with a
//       black move (move number is bumped before any "1..." has been emitted).
//   (B) deleteLine() detaches currentNode from the tree; the next addMove() grafts
//       onto an orphan and getAllLines() then returns 0 lines -> the move the user
//       just played is silently lost from the Explored Lines panel.
//   (C) renderTreeLines()' `isActive` predicate flags EVERY line whose UCI string
//       starts with the active line, so several cards show "Active" at once.

const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '..', 'frontend', 'scout.js');
const src = fs.readFileSync(SRC, 'utf8').split('\n').slice(0, 165).join('\n');
// Provide a CommonJS shim for the two classes.
const factory = new Function(
    'module',
    src + '\nmodule.exports = { SessionTreeNode, SessionLineTree };'
);
const mod = { exports: {} };
factory(mod);
const { SessionLineTree } = mod.exports;

const M = (uci, san, color, fen) => ({
    fen_before: 'fb_' + uci, fen_after: 'fa_' + uci, uci, san, color, fen
});

let failures = 0;
function check(name, cond, detail) {
    if (cond) {
        console.log('  [FAIL-REPRODUCED] ' + name + ' -> ' + detail);
    } else {
        console.log('  [no-repro]        ' + name);
    }
    if (cond) failures++;
}

// ---------------------------------------------------------------- (A)
console.log('\n(A) _buildPgn() with a black-first line');
{
    const t = new SessionLineTree();
    t.addMove(M('e7e5', 'e5', 'black'));
    t.addMove(M('d2d4', 'd4', 'white'));
    const pgn = t.getAllLines()[0].pgn;
    console.log('    pgn =', JSON.stringify(pgn));
    // "e5 2. d4" is not valid PGN: black's 1...e5 has no move-number prefix and
    // the following white move is labelled move 2.
    check('A: invalid PGN for black-first line', pgn === 'e5 2. d4', JSON.stringify(pgn));
}

// ---------------------------------------------------------------- (B)
console.log('\n(B) deleteLine() detaches currentNode -> next move is lost');
{
    const t = new SessionLineTree();
    t.addMove(M('e2e4', 'e4', 'white'));                    // root -> A
    const leaf = t.addMove(M('e7e5', 'e5', 'black'));      // A -> B (leaf)
    // User presses the "Back" button once: currentNode moves B -> A, and A is the
    // node that will be pruned when B is deleted.
    t.currentNode = leaf.parent;
    const linesBefore = t.getAllLines().length;
    t.deleteLine(leaf);                                    // user clicked "X" on B's card
    const linesAfterDelete = t.getAllLines().length;
    const detached = !t.currentNode.isRoot();
    // now the user plays a NEW move on the (still displayed) position after 1.e4
    t.addMove(M('d2d4', 'd4', 'white'));
    const linesAfterPlay = t.getAllLines().length;
    console.log('    lines before delete =', linesBefore,
                '| after delete =', linesAfterDelete,
                '| after new move =', linesAfterPlay,
                '| currentNode detached =', detached);
    check('B: played move lost from getAllLines()',
        linesAfterPlay === 0, 'getAllLines().length = ' + linesAfterPlay);
}

// ---------------------------------------------------------------- (C)
console.log('\n(C) isActive prefix match flags multiple cards "Active"');
{
    const t = new SessionLineTree();
    t.addMove(M('e2e4', 'e4', 'white'));            // root -> A
    const afterE5 = t.addMove(M('e7e5', 'e5', 'black'));   // A -> B
    t.addMove(M('g1f3', 'Nf3', 'white'));            // B -> leaf1 (3 plies)
    t.currentNode = afterE5;                         // step back to B
    t.addMove(M('b1c3', 'Nc3', 'white'));            // B -> leaf2 (3 plies)
    t.currentNode = t.root.children[0];              // user is 1 ply in
    const act = t.getActiveLine().map(m => m.uci).join(' ');
    const activeCount = t.getAllLines().filter(l => {
        const lu = l.moves.map(m => m.uci).join(' ');
        return !!(act && (lu === act || lu.startsWith(act)));
    }).length;
    console.log('    lines =', t.getAllLines().length,
                '| activeUcis =', JSON.stringify(act),
                '| cards flagged Active =', activeCount);
    check('C: >1 card flagged Active while 1 ply deep', activeCount > 1,
        'flagged=' + activeCount + ' of ' + t.getAllLines().length);
}

console.log('\nfailures reproduced: ' + failures);
process.exit(0);
