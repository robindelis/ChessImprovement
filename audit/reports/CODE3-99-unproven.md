# CODE3-99 — Unproven suspicions (static reviewer CODE-3, frontend)
- **Severity:** low
- **Type:** ui-regression
- **File:** frontend/repertoire_builder.js, frontend/board.js, frontend/index.html
- **Line(s):** see each item
- **Component:** Repertoire Builder / InteractiveChessboard
- **Repro script:** n/a (not confirmed; would need a browser session or a hostile-data fixture)
- **Detected by:** CODE-3

These are leads I could not confirm statically. They are **not** counted in the 22 confirmed findings.

1. **Latent `innerHTML` injection surface (unconfirmed exploitability).** `loadRecentGames()`
   (`repertoire_builder.js:992-1015`), `renderGameReviewBanner()` (1345-1374) and `renderTreeLines()` (2862-2908)
   interpolate server/Lichess data into `innerHTML` with no escaping: `${g.opponent_name}`,
   `${g.opening_name}`, `href="${g.game_url}"`, `${g.result}` (also into a `class` attribute),
   `${line.name}` / `${line.pgn}` / `${wData.message}`. `backend/api.py:341-380` always populates these from the
   `games` table, and Lichess usernames are charset-restricted (`[A-Za-z0-9_-]`), so I could not construct a
   payload. **The missing escaping layer is the defect**; a single future endpoint that returns free text
   (e.g. a study description, a game comment) turns this into stored XSS. Worth a defensive `textContent`/
   `escapeHtml` sweep regardless of current exploitability.

2. **`sortCandidateMoves` will throw on a null `san`** (`repertoire_builder.js:533`:
   `return mul * a.san.localeCompare(b.san);`). `backend/repertoire.py:283-305` appends
   `{"san": san, ...}` where `san = m.get("san")` straight from the explorer response — `None` if the field is
   absent. If it ever is, the comparator throws: from `loadCandidateMoves` the throw is caught and the table shows
   "Failed to fetch candidate moves.", but from the sort-header click handler (`bindSortHeader`, line 397) it is an
   **unhandled exception in an event listener** and the table is left half-rendered. Needs a hostile explorer fixture
   to confirm.

3. **`switchSidebarTab`'s `display: none` ↔ `flex` toggling** (2484-2498) is a literal §8.4 pattern violation
   (`repTreeHeaderActions` / `repGamesHeaderActions` are `space-between` siblings inside a `flex-wrap: wrap`
   `.panel-header`, `index.html:273`). I could not show a *differential* wrap at realistic widths: at
   `--board-size`-driven sidebar widths both action groups either fit or both wrap. Reporting as a rule violation
   only; not in the confirmed list.

4. **`deleteSessionLine()` does not repair `currentNode`** (`repertoire_builder.js:3144-3150`).
   `SessionLineTree.deleteLine()` (115-129) only resets `this.currentNode` when it is *exactly* the deleted leaf. If
   the current node is a **descendant** of the deleted leaf, `currentNode` is left dangling on a detached subtree
   while `this.currentStepIndex` is unchanged, so the next `getActiveLine()`/`importToRepertoire()` can import a
   line that is no longer in the tree. I could not confirm a user-reachable sequence (deleting a line requires
   clicking its own "✕", and the active line is always a leaf-prefix of the tree in the flows I traced).

5. **`renderGameReviewBanner()` rewrites `banner.innerHTML` on every position update** (called from
   `updatePosition` → `renderBlunderBanner`, 1454 → 1834-1841 → 1330). A user who is mid-click on
   "📥 Add Line → Rep" while a navigation re-renders the banner loses the click target. Needs a real browser to time.

6. **`_onSquareClick` removal risk.** It is dead today (`board.js:339-360`), but it is the only place that handles a
   *click* without a pointerdown (e.g. synthetic clicks, or a future keyboard/AT-driven interaction). Deleting it
   without checking the tests that may assert on it (`tests/test_*.py` do string-match frontend source) could break
   the suite.

7. **`#repGapsList` / `.gap-item` / `.badge-patched` dead CSS** — folded into CODE3-17 as corroboration; I did not
   raise it separately.
