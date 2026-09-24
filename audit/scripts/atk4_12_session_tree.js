/*
 * SCOUT-12/13/14 repro: the REAL SessionTreeNode / SessionLineTree classes are
 * extracted verbatim from frontend/scout.js and exercised under node with a
 * minimal DOM stub, so the observed behaviour is the shipped behaviour.
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..', '..');   // audit/scripts -> audit -> repo
const src = fs.readFileSync(path.join(ROOT, 'frontend', 'scout.js'), 'utf8');

function extractClass(name) {
    const start = src.indexOf('class ' + name + ' {');
    if (start < 0) throw new Error('class ' + name + ' not found');
    let i = src.indexOf('{', start), depth = 0, end = -1;
    for (let j = i; j < src.length; j++) {
        if (src[j] === '{') depth++;
        else if (src[j] === '}') { depth--; if (depth === 0) { end = j + 1; break; } }
    }
    return src.slice(start, end);
}
const treeSrc = extractClass('SessionTreeNode') + '\n' + extractClass('SessionLineTree');
const { SessionLineTree } = new Function(treeSrc + '\nreturn { SessionLineTree };')();
console.log('--- extracted ' + treeSrc.length + ' chars of SessionTreeNode + SessionLineTree');

const START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
let fenSeq = 0;
function mkTree() { fenSeq = 0; return new SessionLineTree(START); }
// crude but sufficient: a unique fen per (path) so children never collide accidentally
function mv(uci, san, extra) {
    fenSeq++;
    return Object.assign({
        fen_before: 'FB' + fenSeq,
        fen_after: 'FA' + fenSeq,
        uci: uci, san: san,
        color: fenSeq % 2 === 1 ? 'white' : 'black',
        move_number: Math.floor(fenSeq / 2) + 1,
        opening_name: 'King\'s Pawn Game', eco: 'C50'
    }, extra || {});
}
function hdr(t) { console.log('\n=== ' + t); }

let fails = 0;
function check(cond, msg) { console.log((cond ? '  [ok]   ' : '  [FAIL] ') + msg); if (!cond) fails++; }

// ---------------------------------------------------------------- 1
hdr('1. undo past ply 0 / repeated undo');
{
    const t = mkTree();
    t.addMove(mv('e2e4', 'e4')); t.addMove(mv('e7e5', 'e5'));
    for (let i = 0; i < 10; i++) t.stepBack();
    check(t.currentNode === t.root, 'after 10x stepBack currentNode is root (no crash)');
    check(t.getActiveLine().length === 0, 'getActiveLine() is empty at ply 0');
    check(t.getAllLines().length === 1, 'tree still holds 1 line');
}

// ---------------------------------------------------------------- 2
hdr('2. step back and replay -> forks?');
{
    const t = mkTree();
    t.addMove(mv('e2e4', 'e4')); t.addMove(mv('e7e5', 'e5')); t.addMove(mv('g1f3', 'Nf3'));
    t.stepBack(); t.stepBack();
    check(t.getActiveLine().map(m => m.san).join(' ') === 'e4', 'active line is 1.e4');
    t.addMove(mv('c7c5', 'c5'));   // different reply -> should fork
    const lines = t.getAllLines();
    check(lines.length === 2, 'a divergent reply forks the tree (2 lines), got ' + lines.length);
    check(t.root.children.length === 1, 'root still has a single 1.e4 child');
    check(t.root.children[0].children.length === 2, '1.e4 has 2 replies, got ' + t.root.children[0].children.length);
}

// ---------------------------------------------------------------- 3  (the real bug)
hdr('3. deleteLine(currentNode) does NOT re-sync the board / lastBoardFen');
{
    const t = mkTree();
    t.addMove(mv('e2e4', 'e4')); t.addMove(mv('e7e5', 'e5')); t.addMove(mv('g1f3', 'Nf3'));
    const leaf = t.currentNode;
    // scout.js deleteSessionLine(): only calls renderTreeLines(); the board keeps
    // showing leaf.fen and this.lastBoardFen keeps pointing at the deleted leaf.
    let lastBoardFen = leaf.fen;
    t.deleteLine(leaf);
    check(t.currentNode === t.root, 'lineTree.currentNode was reset to root');
    check(lastBoardFen !== t.rootFen, 'BUT the board/lastBoardFen still shows the deleted position');
    // now the user plays one more move (scout.js _onUserBoardMove uses lastBoardFen)
    const boardFenBefore = lastBoardFen;
    const moveObj = { fen_before: boardFenBefore, fen_after: 'AFTER-NEW-MOVE', uci: 'd2d4', san: 'd4', color: 'black' };
    t.addMove(moveObj);
    const lines = t.getAllLines();
    check(t.root.children.length === 1, 'one child of root');
    const child = t.root.children[0];
    check(child.move.fen_before === 'FA3' || child.move.fen_before !== t.rootFen,
        'the new root child claims fen_before = ' + child.move.fen_before + ' (root fen is ' + t.rootFen.slice(0, 12) + '...)');
    console.log('    line PGN rendered for the corrupted branch:', JSON.stringify(lines[0].pgn));
    check(/^1\. d4/.test(lines[0].pgn), 'PGN claims it is move 1 although it is really ply 4');
    console.log('    getAllUniqueMoves():', JSON.stringify(t.getAllUniqueMoves().map(m => m.uci)));
    // and the corrupted move can now be imported into the repertoire
    console.log('    -> importToRepertoire() would POST fen_before=' + child.move.fen_before +
        ' uci=' + child.move.uci + ' (a branch detached from the repertoire root)');
}

// ---------------------------------------------------------------- 4
hdr('4. getAllLines() cost + check_tree payload growth');
{
    const t = mkTree();
    const REPLIES = ['a7a6', 'b7b6', 'c7c6', 'd7d6', 'e7e6', 'f7f6', 'g7g6', 'h7h6'];
    t.currentNode = t.root;
    t.addMove(mv('e2e4', 'e4'));                            // one shared 1.e4
    for (const r of REPLIES) {                             // 8 replies ...
        t.currentNode = t.root.children[0];
        t.addMove(mv(r, r.slice(2, 3) + r.slice(3, 4), { color: 'black' }));
        for (const r2 of REPLIES) {                        // ... x 8 second moves = 64 leaves
            t.currentNode = t.root.children[0].children[t.root.children[0].children.length - 1];
            t.addMove(mv(r2, r2.slice(2, 3) + r2.slice(3, 4), { color: 'white' }));
        }
    }
    const t0 = Date.now();
    const lines = t.getAllLines();
    const dt = Date.now() - t0;
    console.log('    (a) leaves=' + lines.length + '  getAllLines() = ' + dt + ' ms');
    const payload = lines.map((l, idx) => ({ id: String(idx), moves: l.moves.map(m => ({ fen_before: m.fen_before, uci: m.uci, san: m.san })) }));
    console.log('        check_tree JSON payload = ' + JSON.stringify(payload).length + ' bytes, re-posted on EVERY board move');
    const t1 = Date.now();
    for (let i = 0; i < 20; i++) t.getAllLines();
    console.log('        20x getAllLines() = ' + (Date.now() - t1) + ' ms');

    const t3 = mkTree();
    for (let i = 0; i < 400; i++) t3.addMove(mv('e2e4' + i, 'm' + i));   // one 400-ply line
    const t2 = Date.now();
    const l2 = t3.getAllLines();
    console.log('    (b) single 400-ply line: getAllLines() = ' + (Date.now() - t2) +
        ' ms, pgn length = ' + l2[0].pgn.length + ' chars (array copied once per node: O(n^2))');
    console.log('    innerHTML re-rendered by renderTreeLines() = ' + (lines.length * 40) + ' elements per board move');
}

// ---------------------------------------------------------------- 5
hdr('5. _buildPgn disambiguation / numbering with odd-length and mixed input');
{
    const t = mkTree();
    t.addMove(mv('e2e4', 'e4'));
    t.addMove(mv('b8c6', 'Nc6', { color: 'black' }));
    t.addMove(mv('g1f3', 'Nf3'));
    t.addMove(mv('d7d5', 'd5', { color: 'black' }));
    t.addMove({ fen_before: 'x', fen_after: 'y', uci: 'e1g1', san: 'O-O', color: 'white' });
    t.addMove({ fen_before: 'y', fen_after: 'z', uci: 'e8c8', san: 'O-O-O', color: 'black' });
    console.log('    pgn =', JSON.stringify(t.getAllLines()[0].pgn));
    check(/1\. e4 Nc6 2\. Nf3 d5 3\. O-O O-O-O/.test(t.getAllLines()[0].pgn), 'PGG numbering correct for castling');
    const t2 = mkTree();
    t2.addMove({ fen_before: 'a', fen_after: 'b', uci: 'e2e4', san: '12...e4' });  // SAN with a stray number
    console.log('    pgn with dirty san =', JSON.stringify(t2.getAllLines()[0].pgn));
    check(!/12\.\.\./.test(t2.getAllLines()[0].pgn), 'stray move number stripped');
}

console.log('\nFAILS: ' + fails);
process.exit(fails ? 1 : 0);
