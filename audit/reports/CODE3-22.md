# CODE3-22 — Silent-failure sweep: unguarded `res.json()` bodies, a `null`-dereference on the "Reset Defaults" button, and no user feedback on any failed write
- **Severity:** medium
- **Type:** crash
- **File:** frontend/repertoire_builder.js
- **Line(s):** 540-551 (`seedDefaults`), 897-905 (`refreshTree`), 1566-1599, 1622-1653, 2335-2357, 3113-3142
- **Component:** Repertoire Builder — error handling
- **Repro script:** `audit/scripts/CODE-22_error_handling.js`
- **Detected by:** CODE-3

## Expected
Every mutating action either reports success or reports failure to the user; a non-JSON error body is not allowed to
turn into a `TypeError` that is swallowed by `console.error`.

## Actual

**1. "Reset Defaults" can never report anything, on either path.** The frontend:

```js
// repertoire_builder.js:540
async seedDefaults() {
    try {
        const res = await fetch('/api/repertoire/seed', { method: 'POST' });   // no ?force=true
        const data = await res.json();
        if (data.status === 'ok') {
            alert("Default repertoires seeded successfully! ...");
            await this.loadRepertoire(this.activeColor);
        }
    } catch (e) { console.error('Error seeding defaults:', e); }             // silent
}
```

The backend returns `{"status": "skipped", "message": "..."}` when the repertoire is non-empty
(`backend/repertoire.py:1873`) — which fails the `=== 'ok'` test with **no** else-branch — and on the actual seeding
path it **falls off the end of the function and returns `None`** (`backend/repertoire.py:1865-1910`, no `return` after
the seeding loops), which FastAPI serialises as JSON `null`. So on success `data.status` throws
`TypeError: Cannot read properties of null (reading 'status')`, the `catch` swallows it, and the user sees a
`confirm()` dialog and then nothing: no alert, no toast, and no `loadRepertoire()` — the seeded lines stay invisible
until a manual reload. `res.ok` is also unchecked.

**2. `refreshTree()` is the app's central state loader and has neither an `ok` check nor user feedback.**

```js
// repertoire_builder.js:897
async refreshTree() {
    try {
        const res = await fetch(`/api/repertoire/tree?...`);
        this.treeData = await res.json();        // non-JSON 500 -> SyntaxError; HTTP 500 JSON -> assigned as if valid
        this.renderCoverageAndGaps();
    } catch (e) { console.error('Error loading repertoire tree:', e); }
}
```

`treeData` is then read by `renderCandidateTable` (2145), `findMoveInTree` (1486), `stepForward` (1916) and
`renderMoveToolsPanel` (1528) — so a failed refresh silently produces wrong "★ Your Move" / "✓ In Tree" badges with no
indication that anything went wrong.

**3. Every other write is `console.error`-only.** `toggleCurrentMoveImportant` (1596-1598),
`onArrowsChange` (1649-1651), `saveMoveComment` (1694-1696), `saveMove` (2330-2332), `removeBranch` (2354-2356),
`deleteCurrentMoveAndLine` (1617-1619) and `removeTreeLineFromRepertoire` (3139-3141) all end in
`console.error` with no `window.showToast`, while the *import* paths (3050, 3109) correctly do toast. The result is
inconsistent behaviour: a failed import is visible, a failed delete or failed comment save is not, and in the
`removeBranch` case the row's "✕" button remains in place implying the branch still exists.

**4. `loadStudies()` swallows HTTP errors by design** (`if (!res.ok) return;`, line 788), leaving the study
`<select>` showing options for a repertoire that may no longer exist.

## Root cause
No uniform response-validation helper and no single error-reporting convention; each `async` method rolls its own
`try/catch` and most of them degrade to `console.error`. The backend's `None`-returning seed endpoint is a
contract mismatch that the frontend's `data.status` access turns into a swallowed `TypeError`.

## Exploit / escalation
1. **Data-loss escalation (important):** the "Reset Defaults" button's `title` still reads *"Reset and seed Italian
   Game, Sicilian Dragon, and King's Indian"* (`index.html:92`). Today it is inert because the guard is missing
   (`AGENTS.md` §9.4: the endpoint requires `?force=true`). The naive fix — appending `?force=true` to satisfy the
   documented contract — would make the button actually seed, and `AGENTS.md` §9.3 explicitly warns that this
   **overwrites the user's prepared `3. e5` Anti-French and `3. f3` Caro-Kann lines** with generic `3. Nc3` /
   Dragon lines. Combined with finding 1 (no feedback whatsoever on that path), a user clicking it would lose their
   repertoire with zero confirmation beyond a `confirm()` string that says "seed", and no post-action report.
2. Any transient 500 from `/api/repertoire/tree` (which runs the expensive `get_game_gaps()` analysis,
   `backend/repertoire.py:1287`) leaves the UI in the wrong-but-plausible state described in (2) for as long as the
   user does not switch tabs.
3. `saveMoveComment` fires on both the 600 ms input debounce and `blur` (lines 346-355), so a failed save can be
   attempted twice with no user signal either time.
