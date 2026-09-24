# CODE3-19 — DUP: `SessionTreeNode`/`SessionLineTree` (170 lines) is duplicated verbatim in `repertoire_builder.js` and `scout.js`, and the `typeof` guard between them does not work
- **Severity:** low
- **Type:** ui-regression
- **File:** frontend/repertoire_builder.js
- **Line(s):** 3-32, 34-172 (canonical copies); frontend/scout.js:3-165 (duplicate)
- **Component:** Session line tree
- **Repro script:** `audit/scripts/CODE-19_tree_dup.js`
- **Detected by:** CODE-3

## Expected
One definition of the shared line-tree data structure, loaded once.

## Actual
`repertoire_builder.js:3-172` defines both classes behind a `typeof … === 'undefined'` guard:

```js
// repertoire_builder.js:3
if (typeof SessionTreeNode === 'undefined') {
    class SessionTreeNode { ... }
    window.SessionTreeNode = SessionTreeNode;
}
if (typeof SessionLineTree === 'undefined') {
    class SessionLineTree { ... }
    window.SessionLineTree = SessionLineTree;
}
```

and `scout.js:3-165` declares the same two classes again, with no guard and two cosmetic differences (a leading
comment, and `// { fen_before, fen_after, uci, san, color, move_number, opening_name, eco, is_custom }` vs
`…, comment, is_important, arrows }` in the `this.move` comment). A `diff` of the two regions shows only indentation
and comments differ.

The guard is ineffective, and the duplication is not even de-duplicated at runtime: in a classic `<script>`, a
top-level `class` declaration creates a binding in the **global lexical environment**, which *shadows* the
`window.SessionTreeNode` property rather than re-using it. So after both files load there are two distinct class
objects, and `scout.js`'s `new SessionLineTree()` builds nodes with `scout.js`'s class while `repertoire_builder.js`
uses its own. The guard only matters for load order, and both files are classic scripts loaded in a fixed order
(`index.html:922, 924`), so it can never trigger in practice.

## Root cause
The zero-build setup (`AGENTS.md` §6.1: "Do NOT install Webpack, Vite, or TypeScript") invited a copy of the shared
classes into each consumer file instead of a third `frontend/line-tree.js` include. The `typeof` guard reads like a
shared-module mechanism but provides none in a classic-script context.

## Exploit / escalation
1. A real bug class, not just duplication: a `node instanceof SessionTreeNode` check, or code that constructs a node
   with one copy and passes it to a tree built by the other, would silently fail. (`deleteLine(leafNode)` at
   `repertoire_builder.js:115` relies on duck-typed `isRoot()`/`parent`, which happens to work across copies — until
   someone adds a type check.)
2. Field drift is already visible: the builder's `this.move` comment documents `comment`, `is_important` and
   `arrows`; the scout's does not, yet `scout.js` writes `arrows` onto the same shape — exactly the drift a single
   shared definition would prevent.
3. `getAllUniqueMoves()` is likewise implemented twice (builder 131-148, scout 128-145) and only in the builder's copy
   is it used by the "📥 Import All Lines" button, so the two importers can legitimately diverge.
