# DUP-01 — `SessionTreeNode`/`SessionLineTree` are copy-pasted between `scout.js` and `repertoire_builder.js`, with behavioural drift and a load-order-dependent guard
- **Severity:** medium
- **Type:** ui-regression
- **File:** frontend/scout.js (4-165) / frontend/repertoire_builder.js (3-171)
- **Component:** Session line tree (both consumers)
- **Repro script:** `audit/scripts/CODE4_dup_scan.sh`
- **Detected by:** CODE-4

## Expected
One canonical `SessionLineTree` implementation, loaded once and shared by both
`OpeningScout` and `RepertoireBuilder`.

## Actual
`repertoire_builder.js` contains a **forked copy** of the two classes, wrapped
in a guard that only works in one script order:

```js
// frontend/repertoire_builder.js:3, 31, 34, 171
if (typeof SessionTreeNode === 'undefined') {
    class SessionTreeNode { ... }
    window.SessionTreeNode = SessionTreeNode;
}
if (typeof SessionLineTree === 'undefined') {
    class SessionLineTree { ... }
    window.SessionLineTree = SessionLineTree;
}
```

`scout.js` then **unconditionally re-declares both at top level**:

```js
// frontend/scout.js:4
class SessionTreeNode { ... }
// frontend/scout.js:33
class SessionLineTree { ... }
```

Because `index.html:922` loads `repertoire_builder.js` **before**
`index.html:924` `scout.js`, and because a top-level `class` declaration in a
classic script creates a *script-scope lexical binding* (not a property of the
global object), the builder's `window.SessionTreeNode` property and scout's
script-scope `SessionTreeNode` binding **coexist**. `OpeningScout` instantiates
scout's copy (`scout.js:171`: `this.lineTree = new SessionLineTree();`),
`RepertoireBuilder` instantiates the builder's copy
(`repertoire_builder.js:193-194`: `white: new SessionLineTree(), black: new
SessionLineTree()`). Two divergent classes are live at runtime.

The divergence is behavioural, not cosmetic. `addMove`'s metadata sync:

```js
// frontend/scout.js:58-63  (scout's copy)
} else {
    // Update metadata if child already exists
    if (moveObj.san && (!child.move || !child.move.san)) child.move.san = moveObj.san;
    if (moveObj.opening_name && (!child.move || !child.move.opening_name)) child.move.opening_name = moveObj.opening_name;
    if (moveObj.eco && (!child.move || !child.move.eco)) child.move.eco = moveObj.eco;
}
```

```js
// frontend/repertoire_builder.js:66-70  (builder's copy — carries three MORE fields)
} else {
    if (moveObj.san && ...) child.move.san = moveObj.san;
    if (moveObj.opening_name && ...) child.move.opening_name = moveObj.opening_name;
    if (moveObj.eco && ...) child.move.eco = moveObj.eco;
    if (moveObj.comment && (!child.move || !child.move.comment)) child.move.comment = moveObj.comment;
    if (moveObj.is_important && child.move) child.move.is_important = moveObj.is_important;
    if (moveObj.arrows && child.move) child.move.arrows = moveObj.arrows;
}
```

so a move re-played in the Scout never inherits the comment / `is_important` /
`arrows` that the Builder stored for the same UCI. The child-matching predicate
also differs:

```js
// frontend/scout.js:49-52
this.currentNode.children.find(c =>
    (c.move && moveObj.uci && c.move.uci === moveObj.uci) ||
    (c.fen && moveObj.fen_after && c.fen === moveObj.fen_after)
);
// frontend/repertoire_builder.js:47-50
this.currentNode.children.find(c =>
    (c.move && moveObj.uci && c.move.uci === moveObj.uci) ||
    (c.fen && (moveObj.fen_after || moveObj.fen) && c.fen === (moveObj.fen_after || moveObj.fen))
);
```

and `getAllLines()` differs too (scout tracks `studyName`/`node.lineName`,
the builder does not; the builder guards `if (child && child.move)` in the DFS,
scout does not).

`_buildPgn` is a byte-identical copy of the same buggy routine, so **CODE-03
must be fixed twice** or one of the two panels keeps emitting
`"e5 2. d4"`.

The same fork exists at the *method* level, at **~690 duplicated lines**
(measured by `audit/scripts/CODE4_dup_scan.sh`):

| Cluster | scout.js | repertoire_builder.js |
|---|---|---|
| `SessionTreeNode` + `SessionLineTree` | 4-167 (164 L) | 3-174 (172 L) |
| `loadStudies` | 841-881 (41 L) | 785-817 (33 L) |
| `updateActiveLineCounts` | 944-957 (14 L) | 2576-2586 (11 L) |
| `scheduleOverlapCheck` | 1385-1395 (11 L) | 2586-2596 (11 L) |
| `checkActiveLineOverlap` | 1395-1474 (80 L) | 2596-2655 (60 L) |
| `checkTreeOverlap` | 1474-1524 (51 L) | 2655-2775 (121 L) |
| `renderTreeLines` (+`_applyTreeBadges`) | 1165-1594 (430 L) | 2775-2988 (214 L) |
| `importToRepertoire` | 1594-1695 (102 L) | 2988-3054 (67 L) |
| `importAllLinesToRepertoire` | 1695-1774 (80 L) | 3054-3179 (126 L) |
| `markLastMoveAsImportant` / delete flows | 1782-1912 (131 L) | 3120-3165 (46 L) |
| **smaller-of-pair total** | | **~690** |

The `addMove` metadata sync is measurably asymmetric (scan section 3):

```
  frontend/repertoire_builder.js   san=1 opening_name=1 eco=1 comment=1 is_important=1 arrows=1
  frontend/scout.js                san=1 opening_name=1 eco=1 comment=0 is_important=0 arrows=0
```

**Canonical version:** the `repertoire_builder.js` copy is the more evolved one
(it has the comment/importance/arrow sync, the `child.move` null guard and the
`fen`-or-`fen_after` fallback). It should become the single shared class, loaded
in a file that both consumers can rely on, with the scout's `studyName` /
`lineName` additions merged in. `scout.js` should not redeclare it at all.

The API layer is forked the same way:
`/api/scout/check_line` + `/api/scout/check_tree` (`backend/api.py:1067`,
`:1076`) vs `/api/repertoire/check_line` + `/api/repertoire/check_tree`
(`backend/api.py:1100`, `:1109`), with Scout using the former and the Builder
the latter for byte-identical logic.

## Root cause
The class was copy-pasted instead of extracted, and the `typeof … === 'undefined'`
guard was defeated because `scout.js` (loaded later) redeclares at top level
rather than reusing `window.SessionLineTree`.

## Exploit / escalation
1. **Fix divergence → new bug.** Patching `_buildPgn` (CODE-03) in
   `scout.js` only leaves the Builder emitting invalid PGN; patching
   `deleteLine` (CODE-02) in one file leaves the other able to orphan
   `currentNode`. Any future patch to the tree must be applied twice or the
   classes must first be unified.
2. **Load-order landmine.** If the `<script>` order in `index.html` is ever
   swapped (e.g. `scout.js` before `repertoire_builder.js`), then
   `typeof SessionLineTree === 'undefined'` at `repertoire_builder.js:34`
   evaluates against scout's script-scope lexical binding, the guard skips
   definition, and `window.SessionLineTree` is never set — silent behaviour
   change with no error.
3. **Cross-panel inconsistency is user-visible.** Import a line in the Scout
   with a comment, then re-play the same move in the Scout: `addMove` reuses
   the existing child and, per `scout.js:60-62`, only fills *empty* `san`/
   `opening_name`/`eco` — the comment is not restored, whereas the Builder
   would restore it. The note bar shows empty and saving it **overwrites** the
   repertoire comment with `''`.
4. Adjacent weaker variant: the two panels maintain independent
   `treeOverlapMap`s against the same data, so a Scout import never refreshes
   the Builder's badges and vice versa (see CODE-06, variant 4).
