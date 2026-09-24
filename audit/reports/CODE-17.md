# CODE-17 — FEN-format split, an unreachable "transposition" branch, and `nextStep()` following the wrong branch
- **Severity:** medium
- **Type:** wrong-result
- **File:** frontend/scout.js
- **Line(s):** 46-67 (`addMove`), 128-145 (`getAllUniqueMoves`), 755-793 (`nextStep` / `lastStep`), 666-670 (`_onUserBoardMove`)
- **Component:** SessionLineTree FEN handling + board navigation
- **Repro script:** `audit/scripts/CODE4_fen_mismatch_repro.js`
- **Detected by:** CODE-4

## Expected
1. Every FEN stored in a tree node must be in one canonical form (the
   backend's `normalize_fen`, i.e. the first 4 FEN fields) so identity
   comparisons and `(fen_before, uci)` de-duplication work across node sources.
2. `addMove`'s child matching must not advertise support it does not have.
3. "Next ▶" / "End ⏭" must follow the branch the user is on (or at least a
   deterministic, stated one), not blindly the first-created child.

## Actual

**(a) Two FEN formats coexist in the same tree.**

* Study-seeded nodes carry the backend's *normalized* 4-field FEN
  (`backend/repertoire.py:23-29`, `" ".join(parts[:4])`).
* User-played nodes carry chess.js's *full* 6-field FEN, because
  `_onUserBoardMove` reads the board directly:
  ```js
  // frontend/scout.js:666-670
  const fenBefore = this.lastBoardFen || this.lineTree.rootFen;
  const fenAfter = this.board.getFEN();      // <-- 6 fields: ... - 0 1
  this.lastBoardFen = fenAfter;
  ```
* Navigation writes the node's own format back into `lastBoardFen`
  (`scout.js:719`, `742`, `760`, `781`, `798`, `812`, `822`), so `fen_before`
  on the next recorded move is 4-field after a study-seeded step and 6-field
  after a user step.

**(b) The FEN clause of `addMove` is unreachable dead code.**

```js
// frontend/scout.js:48-52
// Check if an existing child from currentNode matches by uci or fen_after
let child = this.currentNode.children.find(c =>
    (c.move && moveObj.uci && c.move.uci === moveObj.uci) ||
    (c.fen && moveObj.fen_after && c.fen === moveObj.fen_after)
);
```

Two *distinct* legal moves from one position can never reach the same FEN (the
origin square differs, so the piece placement differs), so the second disjunct
can never fire. It looks like transposition support and is not. Even if it
could, the 4-vs-6-field split guarantees the strings would not be equal.

**(c) `getAllUniqueMoves()` de-duplicates on the raw string.**

```js
// frontend/scout.js:132-137
if (node.move && node.move.uci && node.move.fen_before) {
    const key = `${node.move.fen_before}_${node.move.uci}`;
    if (!seen.has(key)) { seen.add(key); moves.push(node.move); }
}
```

**(d) `nextStep()` / `lastStep()` follow `children[0]` unconditionally.**

```js
// frontend/scout.js:755-757
nextStep() {
    if (this.lineTree.currentNode.children.length > 0) {
        const nextNode = this.lineTree.currentNode.children[0];
```
```js
// frontend/scout.js:776-778
while (this.lineTree.currentNode.children.length > 0) {
    this.lineTree.currentNode = this.lineTree.currentNode.children[0];
}
```

## Root cause
No canonicalisation at the tree boundary, plus array-order-based branch
traversal. The backend normalises every FEN it stores *and* every FEN it
receives (`normalize_fen` in `_evaluate_line_against_index`,
`update_move_metadata`, `delete_repertoire_branch`, …) so the server is immune —
but the client tree, the only place doing raw string comparison and raw
`children[0]` traversal, is not. The halfmove/fullmove counters are
semantically irrelevant to position identity but participate in the equality.

## Exploit / escalation
Reproduced under node (`audit/scripts/CODE4_fen_mismatch_repro.js`):

```
(a) c1 === c2 (FEN clause matched)? false      -> clause unreachable
(b) getAllUniqueMoves().length = 2 | keys = ["...RNBQKBNR w KQkq -|e2e4",
                                              "...RNBQKBNR w KQkq - 0 1|e2e4"]
(c) nextStep() would walk into children[0] = e2e4 (the user just played d2d4)
```

1. **"Next ▶" walks the wrong branch.** Any time the tree has a fork, "Next ▶"
   jumps into the *first-created* child, not the one the user is on or the one
   the study seeded last. After `1.e4` → `1...e5` → `2.Nf3` the user presses
   "⏮ Start", then "Next ▶" three times expecting to retrace their line; if
   the study also seeded `2.Nc3` under `1...e5`, "Next ▶" from `1...e5` lands on
   `2.Nc3`. "End ⏭" (`lastStep`) has the same defect compounded over the whole
   subtree, and the overlap badges then describe a line the user is not on.
2. **`📥 All → White/Black` POSTs the same move twice.** Proven above: the same
   `(position, e2e4)` pair appears once with a 4-field and once with a 6-field
   `fen_before`. The backend's own dedup uses the *unnormalised* tuple
   (`pair = (parent_fen, move_uci)` in `backend/scout.py`), so it does not
   dedup them either, and `add_repertoire_move` runs twice for one position.
3. **`lastBoardFen` alternates formats**, so `findMoveInTree` only works by
   luck: `repertoire_builder.js:1489` retries with a normalised key
   (`moves_by_parent[parentFen] || moves_by_parent[norm]`), but any other
   consumer of the raw FEN (e.g. a future cache keyed on FEN) will miss ~half
   the moves.
4. Adjacent weaker variant: the *same* two problems exist in the builder's forked
   copy — and worse, `repertoire_builder.js:2557` builds nodes with
   `new SessionTreeNode(moveObj.fen || moveObj.fen_after, curr, moveObj)`, i.e.
   the opposite `fen`/`fen_after` preference order from `scout.js:55`
   (`moveObj.fen_after || moveObj.fen`). See DUP-01.
