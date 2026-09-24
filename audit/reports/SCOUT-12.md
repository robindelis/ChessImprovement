# SCOUT-12 — Deleting a session line leaves the board desynced; the next move is grafted onto a detached FEN with a broken PGN
- **Severity:** medium
- **Type:** wrong-result
- **File:** frontend/scout.js
- **Line(s):** 112-126 (`SessionLineTree.deleteLine`), 666-713 (`_onUserBoardMove`), 147-164 (`_buildPgn`), 1907-1912 (`deleteSessionLine`)
- **Component:** Opening Scout — session line tree
- **Repro script:** `audit/scripts/atk4_12_session_tree.js` (section 3; the *real* classes are extracted verbatim from `frontend/scout.js` and run under node)
- **Detected by:** ATTACKER-4 (SCOUT)

## Expected
Deleting the line the board is currently showing must return the board to a state consistent
with `lineTree.currentNode`. The next move played must hang off `rootFen` and be numbered
correctly.

## Actual
`deleteLine` resets `currentNode` to the root, but the caller never re-syncs the board:

```js
deleteSessionLine(line) {
    if (!line || !line.leafNode) return;
    this.lineTree.deleteLine(line.leafNode);
    this.renderTreeLines();                       // <-- no board.loadFEN, no _updateBoardLabels
    this._showQuickAddNotice('Line removed from session tree', '#94a3b8');
}
```

```js
deleteLine(leafNode) {
    ...
    if (this.currentNode === leafNode || !this.currentNode) {
        this.currentNode = this.root;              // tree says "root", board still shows the leaf
    }
}
```

`_onUserBoardMove` then takes the parent FEN from `this.lastBoardFen`, which is still the
**deleted** position:

```js
const fenBefore = this.lastBoardFen || this.lineTree.rootFen;
```

Observed (node harness driving the shipped classes; `FB*`/`FA*` are stand-ins for the real
FENs the board hands out):

```
=== 3. deleteLine(currentNode) does NOT re-sync the board / lastBoardFen
  [ok]   lineTree.currentNode was reset to root
  [ok]   BUT the board/lastBoardFen still shows the deleted position
  [ok]   one child of root
  [ok]   the new root child claims fen_before = FA3 (root fen is rnbqkbnr/ppp...)
    line PGN rendered for the corrupted branch: "d4"
  [FAIL] PGN claims it is move 1 although it is really ply 4
    getAllUniqueMoves(): ["d2d4"]
    -> importToRepertoire() would POST fen_before=FA3 uci=d2d4 (a branch detached from the repertoire root)
```

Consequences, all of them user-visible:

1. The tree now contains a child of the **start position** whose `fen_before` is a mid-game
   position. `getPathFromRoot()` / `getActiveLine()` still returns a 1-ply "line".
2. `_buildPgn` emits the invalid PGN fragment **`"d4"`** with no move number at all
   (`isWhite = (m.color === 'white' || (i % 2 === 0 && !m.color))` and the move really is a
   Black move, so no number is prefixed). That string is what `renderTreeLines()` prints and
   what a user copy-pastes.
3. `move_number` is computed as `Math.floor(activeLine.length / 2) + 1` = **1**, so the move
   note panel and the "Move N" label are wrong.
4. `importToRepertoire()` / `importAllLinesToRepertoire()` will POST that detached `fen_before`,
   and `add_repertoire_move` happily accepts an unreachable parent (SCOUT-11 §5), creating a
   floating branch in the repertoire.
5. `_updateBoardLabels()` / `updateActiveLineCounts()` are not called either, so the ply badge
   and the overlap strip keep showing the deleted line's values.

Everything else about the tree behaved correctly in the same harness (recorded so it is not
re-chased): 10 × `stepBack()` at ply 0 is a safe no-op; stepping back and replaying a different
move forks correctly (2 leaves, 1 child under root, 2 replies under `1.e4`); `_buildPgn`
handles castling (`1. e4 Nc6 2. Nf3 d5 3. O-O O-O-O`) and strips stray move numbers
(`"12...e4"` → `1. e4`).

## Root cause
`deleteLine` mutates the tree but the board/lastBoardFen/label state is only refreshed on
`undoUserMove`, `firstStep`, `lastStep`, `resetBoard` and `loadTreeLine` — not on delete.
`lastBoardFen` is a redundant cache of board state with no invariant tying it to
`lineTree.currentNode`.

## Exploit / escalation
1. Delete any line that the board is currently showing (the "✕" button on the active card) and
   then play one move → the tree is permanently corrupt for that session.
2. The corrupted line still shows a `★ White New` / `⚡ White: 1/1` overlap badge (it is POSTed
   to `/api/scout/check_line` like any other), so the user is told the line is importable.
3. Because `renderTreeLines()` runs `getAllLines()` fresh, the corrupted node is rendered with a
   PGN that cannot be re-parsed by the PGN importer, so a re-import round-trip is lossy.
