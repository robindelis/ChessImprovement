# FIXER-F-01 — Repertoire Builder frontend patch (CODE3-01 … CODE3-22)

**Files owned and edited (exclusively mine):**
`frontend/repertoire_builder.js`, `frontend/board.js`, `frontend/index.html`, `frontend/style.css`

**Files added:**
`tests/test_builder_frontend_contracts.py`, `audit/scripts/FIXER-F_logic_probe.js`,
`audit/scripts/FIXER-F_integration_scenario.js`

**Not touched:** `frontend/scout.js`, `repertoire_trainer.js`, `app.js`, `audio.js`, `backend/**`,
and every pre-existing test.

**Diff size:** `repertoire_builder.js` +1024/−427, `board.js` +183/−54, `index.html` +63/−7,
`style.css` +205/−3.

---

## Verification summary

| Evidence | Before | After |
|---|---|---|
| `tests/test_builder_frontend_contracts.py` (34 static/contract tests) | `Ran 34 … FAILED (failures=32, errors=1)` | `Ran 34 … OK` |
| `audit/scripts/FIXER-F_logic_probe.js` (56 pure-logic checks, real source extracted + executed under `node`) | `32 CHECK(S) FAILED (exit 1)` | `ALL CHECKS PASSED (exit 0)` |
| `tests/frontend_harness.js` + `audit/scripts/FIXER-F_integration_scenario.js` (39 **runtime** checks against the real shipped `board.js`/`repertoire_builder.js`) | `allPass=false, failures=27` | `allPass=true, total=39, failures=0` |
| `python3 -m unittest discover tests` (full suite, 266 tests) | 104 pre-audit baseline | `Ran 266 … FAILED (failures=10)` — **all 10 are other agents' backend/scout tests, none reference `frontend/`; my module is 34/34 OK** |

Reproduce the before/after with:

```bash
# pre-fix sources are preserved read-only in /tmp/opencode/fixerf/ (board.js, repertoire_builder.js,
# index.html, style.css exactly as found at 18:20)
FRONTEND_DIR=/tmp/opencode/fixerf /home/robin/miniconda3/bin/python3 -m unittest tests.test_builder_frontend_contracts
node audit/scripts/FIXER-F_logic_probe.js --dir /tmp/opencode/fixerf
/home/robin/miniconda3/bin/python3 -m unittest tests.test_builder_frontend_contracts   # post-fix
node audit/scripts/FIXER-F_logic_probe.js
node tests/frontend_harness.js audit/scripts/FIXER-F_integration_scenario.js
```

**Honesty note on the DOM-contract checks.** `test_every_literal_dom_lookup_resolves_in_index_html`,
`test_blunder_banner_container_exists_and_is_wired`, `test_conditional_controls_do_not_use_display`,
`test_eval_bar_label_is_clamped_and_flip_aware` etc. are **static source scans** — there is no DOM in a
Python process, so they compare a literal selector/id/class/attribute found in the JS or CSS against
`index.html`. That is exactly the class of bug that produced CODE3-01 and CODE3-03, and a static check
is the only way to catch it deterministically. It is **not** a rendering assertion. To compensate, all
behavioural claims are additionally proven **at runtime** by `FIXER-F_integration_scenario.js`, which
loads the real shipped files into a Node `vm` behind a DOM shim and exercises the actual methods
(39/39 green post-fix, 27 of them red pre-fix).

---

## CODE3-03 (HIGH) — `#blunderBannerAlert` missing ⇒ the whole §4B/§4D blunder-review feature was unreachable

### Root cause
The banner container was deleted from `index.html` while ~200 lines of JS that target it were kept, and
every lookup was individually `if (x)`-guarded, so each one silently degraded to a no-op. With the
banner dead, `addressGameGap()` (83 lines) and `addressGap()` (40 lines) had no reachable caller,
`treeData.gaps` / `treeData.game_gaps` were never rendered, `showPatchedGaps` was never set to `true`,
and the backend kept paying the full `get_game_gaps()` cost on every `/api/repertoire/tree`.

### Fix — the feature is restored end to end (not deleted)

**`frontend/index.html:160-165`** — the missing container, with a **constant height** and always in flow
per §8.5 (a muted placeholder instead of `display:none`):

```html
<!-- Root-Cause Blunder Banner (AGENTS.md 4.B / 4.D).
     Constant height + always in flow: a subtle placeholder is rendered when there is
     nothing to report, so showing/hiding it never shifts the layout (AGENTS.md 8.5). -->
<div id="blunderBannerAlert" class="blunder-banner-alert" role="status" aria-live="polite">
    <span class="blunder-banner-placeholder">No opening blunder at this position</span>
</div>
```

**`frontend/index.html:303-308, 313-322, 361-386`** — a third sidebar tab so the data has somewhere to
live, and the header actions for §4.D:

```html
<button id="btnTabRepGaps" class="rep-subcard-tab" title="Root-cause opening blunders …">
    🚨 Blunders (<span id="repGapsCount">0</span>)
</button>
…
<button id="btnRepTogglePatchedGaps" … style="visibility:hidden;" aria-pressed="false" …>
    Show Patched (<span id="repPatchedGapsCount">0</span>)
</button>
<button id="btnRepNextBlunder" class="btn-primary" … min-width:118px; …>Address #1 Blunder</button>
…
<div id="repGapsContainer" …>
  <div class="gaps-section-header">…<span id="repGapsMeta">--</span></div>
  <div id="repGapsList" …></div>
  <div class="gaps-section-header">…<span id="repCoverageGapsMeta">--</span></div>
  <div id="repCoverageGapsList" …></div>
</div>
```

**`frontend/repertoire_builder.js:2084-2176`** — `renderBlunderBanner(curr)` now *always* renders
content and builds it with DOM APIs (so the text is escaped by construction and the action buttons are
guaranteed to exist — the old code wrote `innerHTML` and then could not even find the container it had
just written). States: game review → game banner; a gap loaded → summary + "↗ Game"; current step **is**
a blunder → `has-blunder` styling + `btnAdoptSolvedMove` ("+ Adopt g6") + `btnNextBlunderReviewSolve`
("Address Next Blunder (#1) →", revealed with `visibility` + `disabled` per §8.4); otherwise the
placeholder.

**`frontend/repertoire_builder.js:2251-2340`** — new `renderGapLists()` renders **both** payloads the
backend already returns and the frontend discarded:

* `treeData.game_gaps.gaps` → `#repGapsList` (played → engine-preferred move, `cpl_loss`,
  `opening_name`/`eco`, `patched_count`, `earlier_mistake` rendered as "↳ root cause: move N (…)", and a
  `min-width:110px` "Review & Fix →" button that calls `addressGameGap(gap)`);
* `treeData.gaps` (top-20 unprepared frequent opponent replies) → `#repCoverageGapsList` with a button
  that calls **`addressGap(gap)`** — this finally gives `addressGap()` its first real caller;
* the tab badge, the `Show Patched (N)` toggle (now flip-ppable via
  `this.showPatchedGaps = !this.showPatchedGaps` → `include_patched=${this.showPatchedGaps}` on the next
  `refreshTree()`), and the "Address #1 Blunder" button.

**`frontend/repertoire_builder.js:2213-2250`** — `adoptBlunderSolution(blunder, btn)` is the single
"Adopt <move> in Repertoire" handler; it calls `saveMove(..., true)`, then re-renders the banner *and*
the gap list, so §4.D's "patched blunders are filtered out and #2 auto-promotes to #1" holds.

**`frontend/repertoire_builder.js:2556-2564`** — `onBoardMove` no longer hand-rolls a 50-line innerHTML
banner; it marks the (now existing) banner `has-solution`, rewrites its text, and toasts.

**`frontend/repertoire_builder.js:2042-2049, 2886-2896, 2977-3026`** — `renderBreadcrumbs` now adds
`blunder-step` to chips whose step is a blunder (revives the orphan CSS);
`addressGameGap`/`addressGap` gained null/legality guards, a toast, and `activeReviewGame = null`;
`switchSidebarTab('gaps')` added.

**`frontend/style.css:1531-1642, 2755-2800`** — `.blunder-banner-alert` (fixed 46 px, `overflow:hidden`),
`.blunder-banner-placeholder`, `.blunder-banner-text` (nowrap+ellipsis, §8.2), `.blunder-banner-actions`
(fixed-width children), `.gaps-section-header/sub`, `.rep-gaps-list`, `.badge-critical`, `.badge-patched`.
This makes the 7 previously-dead CSS rules from CODE3-17 live.

**`frontend/repertoire_builder.js:453-473`** — the `btnTabRepGaps` / `btnRepTogglePatchedGaps` /
`btnRepNextBlunder` listeners, bound in `init()`.

**Verified by:** `test_blunder_banner_container_exists_and_is_wired`,
`test_gap_list_containers_exist_and_are_rendered`, `test_patched_gaps_toggle_is_reachable`,
`test_blunder_banner_has_constant_height` (static) + 9 runtime checks in
`FIXER-F_integration_scenario.js` (placeholder, `has-blunder` class, adopt button text
`+ Adopt g6`, next-blunder button, `↗ Game` link, gap list contents, root-cause line, `addressGap`
replays `Nf6`, HTML escaping, `Show Patched (3)` visible, tab badge `2`).

### Also restored while in the same area (were dead, now live)
`#btnResyncGames` (full "🔄 Refresh All Games" button) and the sync card's "New Blunders" counter +
pill — see CODE3-17. `#repOpeningThreshold` (the §4.B.3 10/20/30/50/100 dropdown) — see CODE3-17.

---

## CODE3-01 (HIGH) — "🗑️ Remove Line" permanently invisible

### Root cause
`index.html:210` hard-coded `display:none` **inline**. An inline `display:none` cannot be overridden by
`style.visibility = 'visible'` (visibility only affects rendering of an already-boxed element), so
`renderMoveToolsPanel()`'s reveal logic and `deleteCurrentMoveAndLine()` were dead.

### Fix — the §8.4 way, not by fighting inline styles
**`frontend/index.html:233`** — inline `display:none` replaced with `visibility:hidden`:

```diff
-… style="font-size:0.75rem; padding:0.25rem 0.55rem; display:none;" title="Remove this move …">
+… style="font-size:0.75rem; padding:0.25rem 0.55rem; visibility:hidden;" title="Remove this move …">
```

**`frontend/index.html:126, 148-151`** — same treatment for the sync "New Blunders" pill
(`visibility:hidden`, with `#syncBlunderPill` added), whose reveal in `resyncGames` was switched from
`style.display` to `style.visibility` (`repertoire_builder.js:740, 804`).

**`frontend/style.css:2755-2800`** — fixed `min-width` + `justify-content:center` for
`#btnRepDeleteCurrentMove`, `#btnRepToggleImportant`, `#btnRepTogglePatchedGaps`, `.btn-review-game`,
`.btn-quick-add-opening`, `.sync-blunder-pill`.

**Verified by:** `test_conditional_controls_do_not_use_display`,
`test_conditional_controls_have_fixed_widths` (static) + the runtime check
*"CODE3-01/02: the Remove Line button becomes visible for a repertoire move"* —
`style.visibility === 'visible' && disabled === false` after `updatePosition()` on a move that is in the
tree (red pre-fix).

---

## CODE3-02 (HIGH) — `loadFEN` in the animation callback wiped the arrows the fast path had restored

### Root cause
`jumpToStep()`'s animated fast path re-implemented 9 of the 12 statements of `updatePosition()` and
**inverted** their order: it ran `renderMoveToolsPanel()` (which restores the saved arrows) *before*
`animateMove(..., () => this.board.loadFEN(curr.fen))`, and `loadFEN` clears `this.arrows`. Result: the
SVG arrow overlay was empty while `#repArrowsCount` still read "2 arrows". (It also silently omitted the
opening-title/ECO/turn-indicator updates, so pressing "Next ▶" left the header stale.)

### Fix — one canonical render path, animation is decoration only
**`frontend/repertoire_builder.js:1594-1660`** — `updatePosition(options)` documents and enforces the
ordering invariant: model/`loadFEN` **first**, `renderMoveToolsPanel()` **last**, candidate fetch last of
all. Everything up to and including the arrows restore runs synchronously.

**`frontend/repertoire_builder.js:2369-2400`** — the duplicated fast path is gone:

```diff
-            if (this.board && typeof this.board.animateMove === 'function') {
-                this.renderBreadcrumbs();
-                this.renderBlunderBanner(curr);
-                this.startEvalStream(curr.fen);
-                …
-                this.renderMoveToolsPanel(curr);
-                this.renderTreeLines();
-                this.board.animateMove(from, to, () => {
-                    this.board.loadFEN(curr.fen);
-                });
-                return;
-            }
+            if (this.board && typeof this.board.animateGhostMove === 'function') {
+                this.updatePosition({ animateFromTo: [from, to] });
+                return;
+            }
```

**`frontend/board.js:511-568`** — new `animateGhostMove(from, to)`: appends a temporary, pointer-events-
none ghost `<img>` at `from`, slides it to `to` and removes it. It mutates **no** state, so it can
neither desynchronise the board (CODE3-13) nor clobber restored arrows.

**`frontend/repertoire_builder.js:1728-1749`** — the counter is now derived from what the board actually
kept, so it cannot lie:

```js
const kept = this.board.setArrows(parsed);
arrowsCountEl.textContent = `${kept} arrow${kept === 1 ? '' : 's'}`;
arrowsCountEl.title = text;
```

**Verified by:** `test_navigation_does_not_defer_loadfen_into_an_animation_callback`,
`test_update_position_loads_the_fen_before_restoring_arrows` (static) + runtime
*"CODE3-02: arrows survive the animated step-forward even after the animation ends"* — the scenario runs
`jumpToStep(1)` **and then manually fires every pending `anim.onfinish()`**, which is exactly what the
pre-fix implementation deferred its `loadFEN()` into. Red pre-fix, green post-fix.

---

## CODE3-04 (HIGH) — two sync streams shared one `syncAbortController` slot

### Root cause
`resyncGames()` and `syncRecentGames()` both did `if (this.syncAbortController) this.syncAbortController.abort();
this.syncAbortController = new AbortController()` and both cleared it **unconditionally** in `finally`.
When stream A was aborted by B, A's `finally` still ran and set the shared field to `null`, destroying
B's handle — so the `✕` button aborted nothing while the UI *looked* cancelled and the server kept
streaming and writing games/blunders.

### Fix
**`frontend/repertoire_builder.js:227-231`** — two independent fields:
```js
this.resyncAbortController = null;      // full "sync_games_stream"
this.recentSyncAbortController = null;  // "sync_recent_stream"
```
**`repertoire_builder.js:774-780, 903-906`** and **`repertoire_builder.js:1256-1260, 1420-1424`** — each stream takes its own
handle and clears it behind an **identity check**:
```js
const abortCtrl = new AbortController();
this.resyncAbortController = abortCtrl;      // (and …recentSyncAbortController)
…
} finally {
    if (this.resyncAbortController === abortCtrl) { this.resyncAbortController = null; }
    this.endWrite(resyncKey);
}
```
**`repertoire_builder.js:672-700`** — `cancelSync()` aborts **both**, and the two 8 s/3.5 s auto-dismiss
guards now test `!this.resyncAbortController && !this.recentSyncAbortController` so neither stream can
hide the other's card. Dead `repGamesInlineProgress` cleanup removed (CODE3-10).

**Verified by:** `test_two_independent_abort_handles`, `test_finally_uses_identity_check`,
`test_cancel_sync_aborts_both` (static) + runtime `cancelSync` aborts both streams, releases both
handles, and a simulated loser's `finally` leaves the winner's handle intact.

---

## CODE3-05 (MEDIUM) — `/api/games/recent` unsequenced

### Root cause
`loadRecentGames()` was fired from the filter pills, the 250 ms search debounce and the tab switch with
**no** `AbortController`, **no** request token and **no** `res.ok` check; a slower earlier response
overwrote both the filtered list and `this.recentGames`. Separately, `renderCoverageAndGaps()` fetched
`/api/games/recent?limit=1` **unfiltered** and overwrote the tab badge, so the badge snapped back to the
all-colours count while the list below showed only White games.

### Fix
**`frontend/repertoire_builder.js:230-234`** — `this.recentGamesSeq`, `this.recentGamesAbort`,
`this.recentCountAbort`.
**`repertoire_builder.js:1058-1099, 1102-1178`** — `loadRecentGames()` and the new
`loadRecentGamesCount()` both `++this.recentGamesSeq`, abort the previous request, check `res.ok`, and
`if (seq !== this.recentGamesSeq) return;` before any DOM or state write.
**`repertoire_builder.js:1083-1101`** — one URL builder `_recentGamesUrl(limit)` so the badge and the
list describe **the same query**; `loadRecentGamesCount()` also sets a `title` explaining the filter
(`_recentGamesQueryLabel()`).

**Verified by:** `test_recent_games_requests_are_sequenced` + runtime: two `loadRecentGames()` calls with
`search=a` / `search=ab`, the **new** response resolved first, then the **old** one — `recentGames`
keeps `NEW` and the badge reads `1`, not the stale `99`. Red pre-fix.

---

## CODE3-06 (MEDIUM) — 4 un-aborted overlap POSTs per navigation, positional ids

### Root cause
Every position update scheduled two checks, each firing White+Black (4 POSTs), none abortable, none
carrying an identity; `checkTreeOverlap()` identified lines by `id: String(idx)` (DFS position), so a
late response repainted a *different* line's badges and button labels.

### Fix
**`frontend/repertoire_builder.js:235-237, 3138-3181`** — `_beginOverlapBatch()` returns
`{ gen, abortCtrl }` after `++this.overlapGen` and aborting the previous batch; `_isStaleOverlap(gen)`
is checked after every `await`; `_overlapFetch()` passes `signal: abortCtrl.signal`. All four requests of
a navigation share one generation and one abort handle.
**`repertoire_builder.js:3235-3284`** — `id: String(l.leafNode ? l.leafNode.id : '')` (stable, node-derived)
instead of the DFS index, and `_applyTreeBadges` resolves the key from the card's own
`data-idx` → `lines[idx].leafNode.id` (was DOM-order index).
**`repertoire_builder.js:3138-3158, 3594-3597, 3675-3678`** — the two post-import un-sequenced double-checks now go
through the debounce.

**Verified by:** `test_overlap_checks_are_sequenced_with_stable_line_ids`,
`test_overlap_badge_renderer_is_not_duplicated` + logic-probe runtime checks on `_isStaleOverlap`
(`_isStaleOverlap(3) === false`, `_isStaleOverlap(2) === true`).

---

## CODE3-07 (MEDIUM) — cross-colour contamination of the candidate table

### Root cause
`loadCandidateMoves()`'s staleness guard compared **FEN only**, and `loadRepertoire()`/`switchColor()`
never aborted `candidateAbortController`. Because `history` is reset to the start FEN on every colour
switch / tab switch / `loadTreeLine()`, a White request issued before the switch passed the guard
(`curr.fen === targetFen`) and rendered White data — with White "★ Your Move" badges — into the Black tab.

### Fix
**`frontend/repertoire_builder.js:2567-2611`** — the request identity is now `(colour, position,
generation)`:
```js
const reqColor = this.activeColor;
const reqGen = ++this.candidateGen;
const isStale = () => (this.activeColor !== reqColor || this.candidateGen !== reqGen);
```
checked after the fetch **and** before the DOM write. `res.ok` is now checked.
**`repertoire_builder.js:951-961`** — `loadRepertoire()` aborts `candidateAbortController` and bumps
`candidateGen` on every colour switch.

**Verified by:** `test_candidate_staleness_guard_includes_colour` (static) + logic-probe regex check.

---

## CODE3-08 (MEDIUM) — unbounded `evalCache`, full-FEN key, unused `depth`

### Root cause
`evalCache` was a plain `Map` only ever added to, keyed on the **full 6-field FEN** (so transpositions
never hit and every new fullmove counter created a new entry) — and `renderEvalBar(scoreCp, mate, depth)`
never read `depth`.

### Fix
**`frontend/repertoire_builder.js:186-189, 235-244`** — `evalCacheMax = 400`, plus a bounded LRU writer:
```js
_evalCachePut(cacheKey, data) {
    const max = this.evalCacheMax || 400;
    const prev = this.evalCache.get(cacheKey);
    if (prev && (prev.depth || 0) > (data.depth || 0)) return;   // never downgrade
    this.evalCache.delete(cacheKey);
    this.evalCache.set(cacheKey, data);
    while (this.evalCache.size > max) {
        const oldest = this.evalCache.keys().next().value;
        if (oldest === undefined) break;
        this.evalCache.delete(oldest);
    }
}
```
**`repertoire_builder.js:1955-1966, 2014-2029`** — key is `this.normalizeFen(fen)` (3 fields), so
transpositions hit; the stream writes through `_evalCachePut`.
**`repertoire_builder.js:1896-1941`** — `renderEvalBar` now *uses* `depth`: it sets
`labelEl.title` / `aria-label` to `"<full score> · Stockfish depth <n>"` (§8.2 recoverable text) and
memoises into `this.lastEval`, which the Flip handler replays so the label placement survives a flip
(CODE3-21).

**Verified by:** `test_eval_cache_is_bounded_and_normalised` + logic probe (`_evalCachePut` executed with
`evalCacheMax=3`: bounded to 3, oldest evicted first, no depth downgrade, recency refreshed on write) +
runtime (bound of 4 respected, oldest evicted, shallower write ignored).

---

## CODE3-09 (MEDIUM) — unguarded `window.promptStudySelection` → unhandled rejection + silent no-op

### Root cause
`onNewStudyClick()` guarded the cross-file global with `typeof … !== 'function'`; the two import entry
points did not. `window.promptStudySelection` is defined in `app.js`, the **last** script tag, so a
partial load made every "📥 Add Line → Repertoire" button an invisible no-op with an unhandled rejection.

### Fix
**`frontend/repertoire_builder.js:3566-3582, 3647-3663`** — both `importToRepertoire` and
`importAllLinesToRepertoire` now guard and toast:
```js
if (typeof window.promptStudySelection !== 'function') {
    this.notify('Study selection is unavailable (app.js failed to load) — cannot import.', '#ef4444');
    return;
}
try { studyName = await window.promptStudySelection({…}); }
catch (e) { this.notify(`Study selection failed: ${e.message}`, '#ef4444'); return; }
```

**Verified by:** `test_prompt_study_selection_is_guarded` (guards ≥ calls; pre-fix `calls=3 guards=1`).

---

## CODE3-10 (MEDIUM, §8.1) — in-flow sync banner → toast

### Root cause
`syncRecentGames()` built a ~60 px widget and `gamesList.prepend()`ed it into the scrollable games list,
then `setTimeout(…, 2500)` removed it — **two full-height layout shifts per sync** (§8.1), on top of a
`createElement`-only-if-absent race that let sync #1's timer delete sync #2's widget.

### Fix
**`frontend/repertoire_builder.js:1262-1266`** — the whole in-flow widget and its three
`repGamesInline*` update sites are deleted. Progress is reported through the already-existing fixed
`#syncProgressContainer` card plus `this.notify(...)` (a `window.showToast` wrapper,
`repertoire_builder.js:226-234`) on `stage === 'completed'` and on error. The 2.5 s removal timer is gone.

**Verified by:** `test_sync_progress_uses_toast_not_in_flow_banner` — asserts
`repGamesInlineProgress` / `gamesList.prepend(` / `repGamesInlineBar` are absent and `window.showToast`
is used.

---

## CODE3-11 (MEDIUM, §8.4) — "📥 Add Opening" relabels twice, no `min-width`, never re-enabled

### Root cause
Three intrinsic widths (≈118 px → ≈74 px → ≈66 px) propagated into a `space-between` card's action
group, and `btnAdd.disabled = true` was never reset — the button was permanently inert after one click.

### Fix
**`frontend/style.css:2770-2786`** — `.btn-review-game { min-width:132px }`,
`.btn-quick-add-opening { min-width:118px }`, both `justify-content:center; white-space:nowrap`.
**`frontend/repertoire_builder.js:1181-1206`** — the handler is latched, restores the label in every
exit path and re-enables on failure:
```js
if (!this.beginWrite(key)) return;
const originalLabel = btnAdd.textContent;
btnAdd.disabled = true; btnAdd.textContent = 'Adding...';
try {
    const ok = await this.importGameOpening(g.id, g.user_color);
    btnAdd.textContent = ok ? '✓ Added!' : 'Failed';
    btnAdd.classList.toggle('added', ok);
    if (ok) { btnAdd.disabled = true; }
    else { btnAdd.disabled = false; setTimeout(() => { if (btnAdd.isConnected) btnAdd.textContent = originalLabel; }, 1800); }
} finally { this.endWrite(key); }
```
(`style.color = '#4ade80'` replaced by the `.added` class so it can be reset.)

**Verified by:** `test_game_card_add_opening_button_is_restored_after_failure` (the handler block must
contain both `btnAdd.disabled = false` and a `finally`).

---

## CODE3-12 (MEDIUM) — no in-flight latch on write buttons

### Root cause**
`+ Set as My Move` / `+ Add Branch` / `✕` were bare one-liners, each success fanning out into three more
awaited requests (`loadStudies` + `refreshTree` + `loadCandidateMoves`), and every failure was
`console.error`-only.

### Fix
**`frontend/repertoire_builder.js:236-244`** — `beginWrite(key)` / `endWrite(key)` over a `Set`, with
`notify()` as the toast wrapper.
Latches added to `saveMove` (`2801-2843`), `removeBranch` (`2845-2884`),
`toggleCurrentMoveImportant` (`1746-1788`), `importToRepertoire`, `importAllLinesToRepertoire`,
`removeTreeLineFromRepertoire`, plus keyed latches on `adoptBlunderSolution`, `importGameOpening`
(per game id), `resyncGames` and `syncRecentGames`; each releases in `finally`.
**`repertoire_builder.js:2755-2783`** — a `withButtonLatch(btn, fn)` helper used by all three
candidate-table row buttons, and **each per-card "📥 Line → White/Black" import button now disables
itself** (`frontend/repertoire_builder.js:3470-3496`).

**Verified by:** `test_write_actions_have_in_flight_latch` (every write must contain `beginWrite(` and
`finally`) + logic probe (`beginWrite`/`endWrite` executed: first wins, second rejected, independent
keys, reusable after release) + runtime.

---

## CODE3-13 (MEDIUM) — `oncancel` dropped `onComplete` → board desync

### Root cause
`animateMove` delivered `onComplete` from `onfinish` and the early returns, but `oncancel` deliberately
did not. `jumpToStep()` used that callback as its *only* re-synchronisation step, so any
`stopAnimations()`/`loadFEN()` inside the 180 ms window cancelled the sync and left the board DOM
permanently on a stale position.

### Fix
**`frontend/board.js:474-500`** — a once-guard shared by `onfinish`, `oncancel` and the `catch`:
```js
let settled = false;
const finishOnce = () => {
    if (settled) return;
    settled = true;
    const idx = this.activeAnimations.indexOf(anim);
    if (idx !== -1) this.activeAnimations.splice(idx, 1);
    if (onComplete) onComplete();
};
anim.onfinish = () => { …; finishOnce(); };
anim.oncancel = () => { …; finishOnce(); };
```
**`frontend/board.js:92-101`** — `setOrientation()` now `stopAnimations()` + `_cleanupDragState()` before
rebuilding `this.squares` (it previously left `activeAnimations` pointing at detached elements).

**Verified by:** `test_cancelled_animations_still_deliver_oncomplete`,
`test_board_has_a_teardown` (static) + runtime: `animateMove('e2','e4',cb)` then `stopAnimations()` →
`delivered === 1` (pre-fix: 0).

---

## CODE3-14 (MEDIUM) — unvalidated `setArrows` / `addArrow`

### Root cause
`setArrows()` only checked `Array.isArray`; `_getSquareCenterPercent()` did `sq.charCodeAt(0)`, so one
`null` entry threw from inside `render()`. Out-of-bounds squares produced `x = 281%`.

### Fix
**`frontend/board.js:171-210`** — `_isValidArrow(a)` / `_isValidSquare(sq)` (exact `a1..h8`),
`setArrows()` filters **and** normalises `{from,to,color}` and **returns the number kept**,
`addArrow()` returns `false` and warns instead of pushing garbage.
**`frontend/board.js:713-720`** — `_getSquareCenterPercent` degrades to the board centre on an invalid
square.
**`frontend/board.js:131-155`** — `loadFEN(fen, { keepArrows })` so a caller that has just restored
per-move state can say so (this is the `loadFEN`-is-a-full-reset footgun the finding describes; the
builder's ordering fix means it no longer needs the flag, but the trainer/scout do).

**Verified by:** `test_arrows_are_validated_at_the_render_boundary` + logic probe **executing the
extracted methods**: `setArrows([null])`, `[{e2,j9}]`, `[{i4,e7}]`, `[{from:undefined,…}]` → 0;
`[{e2,e4},{g1,f3}]` → 2; missing colour normalised; `addArrow('e2','z9')` → `false`;
`_getSquareCenterPercent(null)` → `{50,50}`; `_getSquareCenterPercent('e2')` → `{56.25, 81.25}` + runtime
`updatePosition()` with `arrows = '[null, {e2→j9}, {e2→e4}]'` does not throw and leaves exactly 1 arrow
with the counter reading `1 arrow`.

---

## CODE3-15 (LOW) — global `arrow-marker-${i}` ids collide across the 3 live boards

### Root cause**:** marker ids derived only from the per-board arrow index, resolved document-wide.

### Fix
**`frontend/board.js:25-27`** — a per-instance namespace:
```js
InteractiveChessboard._instanceCounter = (InteractiveChessboard._instanceCounter || 0) + 1;
this._markerPrefix = `ab${InteractiveChessboard._instanceCounter}`;
```
**`frontend/board.js:679`** — `` const markerId = `${this._markerPrefix}-arrow-marker-${i}`; ``

**Verified by:** `test_marker_ids_are_namespaced_per_instance` + runtime: two boards each holding one
arrow produce two different marker ids.

---

## CODE3-16 (LOW) — leaks in `board.js`

### Root cause**:** 22 lines of dead `_onSquareClick`; two `window` listeners per instance with no
teardown; `_rightClickStartSquare` initialised *after* the listeners and not cleared when the right
button is released outside any square (the early `return` also bypassed the drag-ghost cleanup, leaving
a `z-index:9999` ghost and a `opacity:0.2` piece).

### Fix
**`frontend/board.js:41-53`** — `destroy()` removes both `window` listeners, stops animations, calls
`_cleanupDragState()`, clears arrows and the square map (idempotent via `_destroyed`).
**`frontend/board.js:19, 92-101, 112-126`** — `_rightClickStartSquare = null` in the constructor (before
the listeners), `_cleanupDragState()` extracted, `setOrientation()` cancels + cleans before `_initDOM()`.
**`frontend/board.js:351-378`** — `_onPointerUp` now clears the anchor on **every** right-button exit
path (a right release also ends any in-flight left drag), and an off-board release restores the piece
opacity before re-rendering.
**deleted from `frontend/board.js:339-360` (pre-fix)** — the dead `_onSquareClick` is deleted.

**Verified by:** `test_board_has_a_teardown` (no `_onSquareClick`; `destroy()` present) +
`test_pointerup_always_clears_the_right_click_anchor` (in the node probe) + runtime
`destroy()` clears `squares`.

---

## CODE3-17 (LOW) — dead cluster

**Selectors bound instead of removed (feature restored, not deleted):**

| Selector | Fix |
|---|---|
| `repOpeningThreshold` (§4.B.3 dropdown) | `frontend/index.html:92-101` adds the *Opening Cutoff* select (All/10/20/30/50/100); `repertoire_builder.js:291-303` binds it and drives `min_games` on the next `refreshTree()`. |
| `repOpeningThresholdSelect`, `repOpeningThresholdLabel` | `repertoire_builder.js:289-303` — the mirroring `setupOpeningThreshold` helper (and its second never-existing element) removed. |
| `btnResyncGames` | `frontend/index.html:328-330` adds a real "🔄 Refresh All Games" button next to "🔄 Sync Latest", so `resyncGames()` is reachable again. |
| `syncCountNewBlunders`, `syncBlunderPill`, `syncBlunderCount` | `frontend/index.html:148-151, 126` add the "New Blunders" counter and the `🚨 N` pill to the sync card; `repertoire_builder.js:740, 804` toggle the pill with `visibility`. |
| `repGapThresholdDisplay` | `repertoire_builder.js:1002-1003` — the lookup removed (the `repGapThreshold` select already shows the value). |
| `#repGapsList`, `.gap-item`, `.gap-san`, `.gap-meta`, `.badge-critical`, `.badge-patched`, `.breadcrumb-chip.blunder-step` | made live by CODE3-03 + `renderBreadcrumbs` (`repertoire_builder.js:2042-2049`). |
| `.scout-overlap-banner` | **not** made live — see hand-off. |

**Removed as genuinely dead:**
`showNotice()` alias (`repertoire_builder.js:3768-3772`, zero callers);
`#repQuickAddNotice` and `#lblMyStatsHeader` dead DOM nodes (`index.html`);
the always-false `|| this.candidateCache.get(normParent)` fallback (`repertoire_builder.js:2455-2470`) —
`candidateCache` is only ever written as `${color}_${normFen}`, so the second key never existed. It is
replaced by an explicit "No prepared repertoire move here" toast, and the earlier `myStatsLabel` lookup
of `repGapThresholdDisplay` is gone.

**Verified by:** `test_dead_alias_and_dead_dom_nodes_removed`,
`test_no_unmatchable_selectors_remain`, `test_every_literal_dom_lookup_resolves_in_index_html`.

---

## CODE3-18 (LOW) — ~250 duplicated lines

Fixed the two that were already **diverged** (cluster 1) and the one that was **causing a bug**
(cluster 3). See CODE3-02 and CODE3-06 for clusters 1 and 3.

**`frontend/repertoire_builder.js:3285-3322`** — one canonical renderer for the overlap badges and
import-button labels, used by **both** `renderTreeLines()` and `_applyTreeBadges()`:
`_overlapBadgeClass`, `_overlapBadgeLabel`, `_overlapBadgeTitle`, `_overlapBadgesHtml`,
`_importButtonState`. The two inline copies (≈62 lines, one mutating live DOM and one regenerating
`innerHTML`) are deleted.

**Not deduped (documented, not fixed):** the ~85 lines of identical SSE scaffolding shared by
`resyncGames` / `syncRecentGames` / `startEvalStream`, and the `importToRepertoire` /
`importAllLinesToRepertoire` clone. Extracting them is a structural refactor of code I do not fully own
(`scout.js` has its own third copy of the same SSE loop), and the brief says keep the diff surgical.

**Verified by:** `test_overlap_badge_renderer_is_not_duplicated` — strips the canonical helpers from the
source and asserts none of the six inline label literals (`★ White New`, `⚡ Black:`, `📥 Extend White`,
`✓ In White (`, `★ Black New`, `⚡ White:`) survives anywhere else.

---

## CODE3-19 (LOW) — `SessionTreeNode` / `SessionLineTree` duplicated with `scout.js`

**Not fixed — `frontend/scout.js` is owned by another agent.** See the hand-off list. My side keeps the
`if (typeof SessionTreeNode === 'undefined')` guard and is the canonical copy
(`frontend/repertoire_builder.js:3-172`); the two cosmetic comment differences the finding notes are
already reconciled in my copy (`arrows`, `is_important`, `comment` are all documented).

---

## CODE3-20 (LOW, §8.2/§8.5) — eval-bar label and overlap strip wrap and shift

### Root cause
`.eval-bar-label` had no clamp at all (a 28 px bar fits 4 mono glyphs, so `+10.0` wrapped over the
board), and `.scout-line-overlap-strip` had only a `min-height: 28px` floor with `flex-wrap: wrap` and
badges without `nowrap`, so it grew to 46-64 px on narrow windows.

### Fix
**`frontend/style.css:327-381`** — `.eval-bar-label { font-size: 0.62rem; white-space: nowrap;
overflow: hidden; text-overflow: ellipsis; }` (both the lead and the `equal` variant), and the
`title`/`aria-label` set dynamically in `renderEvalBar` (§8.2 recoverable text).
**`frontend/style.css:2524-2540, 2553-2566`** — `.scout-line-overlap-strip { flex-wrap: nowrap;
height: 28px; min-height: 28px; max-height: 28px; overflow: hidden; white-space: nowrap; }` and
`.scout-overlap-badge { white-space: nowrap; max-width: 48%; overflow: hidden; text-overflow: ellipsis;
flex: 0 1 auto; }`.
**`frontend/repertoire_builder.js:3182-3234`** — the strip's badges get a `title` from
`_overlapBadgeTitle()`; the root placeholder still renders (never collapses).

**Verified by:** `test_eval_bar_label_is_clamped_and_flip_aware`,
`test_overlap_strip_has_constant_height_and_never_wraps`, `test_dynamic_gap_text_is_clamped` (static) +
runtime *"CODE3-20: the eval label exposes depth + full score on hover (8.2)"*.

---

## CODE3-21 (MEDIUM) — flipping the board did not flip the eval label

### Root cause
`.eval-bar-wrapper.flipped` flipped the **fill** direction, but the label was positioned by *colour*
classes with un-flipped offsets (`white-lead { bottom: 5px }`, `black-lead { top: 5px }`) and there was no
flipped variant — so after a flip the label sat on the wrong background (dark-on-dark / light-on-white).

### Fix
**`frontend/style.css:352-365`** — the missing CSS half of the feature:
```css
.eval-bar-wrapper.flipped .eval-bar-label.white-lead { top: 5px; bottom: auto; }
.eval-bar-wrapper.flipped .eval-bar-label.black-lead { bottom: 5px; top: auto; }
```
**`frontend/repertoire_builder.js:334-345`** — one `applyEvalBarOrientation(orientation)` helper, called
from the Flip handler, `loadRepertoire()` and `reviewGame()` (the three places that used to inline the
`classList.toggle('flipped', …)`).
**`repertoire_builder.js:316-330`** — the Flip handler then replays `this.lastEval` through
`renderEvalBar`, because the colour class is orientation-independent and would otherwise keep its old
placement until the next SSE frame.

**Verified by:** `test_eval_bar_label_is_clamped_and_flip_aware` (both flipped rules present) + runtime:
`renderEvalBar(500, null, 16)` → `white-lead`; `applyEvalBarOrientation('black')` + replay → wrapper has
`flipped` **and** the label still reads `+5.0`.

---

## CODE3-22 (MEDIUM) — silent-failure sweep

### Root cause
`seedDefaults()` dereferenced `data.status` on a body the backend returns as JSON `null` on its success
path, sent no `?force=true` (so the endpoint always answered `{"status":"skipped"}` with no else-branch),
had no `res.ok` check, and swallowed the `TypeError` — so "Reset Defaults" could never report anything.
`refreshTree()` assigned a non-2xx body to `this.treeData` as if valid, and every other write
(`toggleCurrentMoveImportant`, `onArrowsChange`, `saveMoveComment`, `saveMove`, `removeBranch`,
`deleteCurrentMoveAndLine`, `removeTreeLineFromRepertoire`, `loadStudies`) ended in `console.error`.

### Fix
**`frontend/repertoire_builder.js:602-630`** — `seedDefaults()` checks `res.ok`, uses
`await res.json().catch(() => null)`, guards `data && data.status === 'ok'`, has a distinct
**"skipped — your repertoire already has moves, so it was left untouched"** message, and toasts the
failure. **It deliberately does NOT add `?force=true`**: per §9.3/§9.4 that would overwrite the user's
prepared `3.e5` Anti-French / `3.f3` Caro-Kann lines with generic ones. `frontend/index.html:109` — the
button's `title` no longer advertises the destructive "seed Italian Game, Sicilian Dragon, and King's
Indian" action.
**`repertoire_builder.js:1013-1026`** — `refreshTree()` checks `res.ok` and the payload shape **before**
assigning, and toasts (a failed refresh no longer silently mislabels every "★ Your Move" badge).
**`repertoire_builder.js:226-234`** — `notify(message, color)`; every write path listed above now toasts
on both the not-ok and the exception branch, and `importGameOpening` returns a boolean so its caller can
report truthfully.
**`repertoire_builder.js:1845-1851`** — `onArrowsChange` now tells the user when arrows drawn on a
position that is not yet in the repertoire will not be persisted (the mirror of the finding's item 2).
**`repertoire_builder.js:886-896, 1418-1424`** — `loadStudies()` and the sync fallback check `res.ok`.

**Verified by:** `test_reset_defaults_handles_a_null_body` (incl. the "never sends `force=true`"
assertion), `test_response_status_is_checked_before_use`, `test_failed_writes_toast_the_user` (9 methods)
+ runtime: `seedDefaults()` with a `null` JSON body throws nothing and toasts
"unexpected response" (pre-fix: silent).

---

## Full suite

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
Ran 266 tests in 11.325s
FAILED (failures=10)
```

**None of the 10 failures are mine, and none of them reference `frontend/`.** The failing set *shifts
between runs* (I observed 7, 8 and 10 failures at different moments during this wave) because other
fixers are mid-edit in `backend/repertoire.py`, `backend/scout.py`, `backend/api.py` and
`tests/fixtures.py`:

```
FAIL: test_all_games_scan_and_count (test_repertoire…)          AssertionError: 18 not greater than or equal to 280
FAIL: test_candidate_moves_2000 (test_repertoire…)
FAIL: test_candidate_moves_white_eval_convention (test_repertoire…)
FAIL: test_scout_import_repertoire (test_scout…)               'already_present' != 'success'
FAIL: test_single_played_move_import (test_scout…)
FAIL: test_moves_follow_spoken_order_not_move_generation_order (test_scout_pipeline…)
FAIL: test_spoken_capture_is_recognised (test_scout_pipeline…)
FAIL: test_pipeline_extracts_moves_and_caches_a_complete_study (test_scout_pipeline…)
FAIL: test_multi_study_trainer_mixing (test_studies…)
FAIL: test_trainer_drill_from_move_one (test_studies…)
```

An earlier run in the same window aborted at import time with
`tests/fixtures.py:249 IllegalMoveError: illegal san: 'dxc4' in rnbqkbnr/pp2pppp/2p5/8/3PP3/8/PPP3PP/RNBQKBNR b KQkq - 0 4`
— i.e. the shared synthetic fixture was broken mid-edit, and the collection itself is in flux
(`test_scout_pipeline` did not exist when I started; `test_evaluator_stream` was failing to import on
one run and present on the next).

My own module is stable and green and adds 34 tests to the suite (no `FAIL:`/`ERROR:` line for it in any
`discover` run):

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_builder_frontend_contracts
Ran 34 tests in 0.293s
OK
```

`tests/test_builder_frontend_contracts.py` never touches a database and does **not** import
`tests.test_setup`, so it cannot perturb the shared fixture for anyone else.

---

## Hand-off — changes required in files I do not own

### `frontend/scout.js` (CODE-4 agent)
1. **CODE3-19** — delete the `SessionTreeNode` / `SessionLineTree` class declarations at `scout.js:3-165`
   and rely on the canonical globals defined (and `window.`-assigned) in
   `frontend/repertoire_builder.js:3-172`, which loads first. Today the unguarded re-declaration shadows the
   `window.*` properties (a classic-script top-level `class` creates a *lexical* binding), so there are
   two distinct class objects at runtime. Do the same for `getAllUniqueMoves()`.
2. **CODE3-14 (part)** — `scout.js:1065, 1075` call `board.setArrows(arr)` and now benefit from the
   validation added in `board.js`; please keep passing raw persisted JSON (no pre-validation needed)
   and switch to the **returned count** for any label you render, so the count cannot disagree with the
   board.
3. **CODE3-14 (part)** — `board.loadFEN()` still clears arrows by default. Pass
   `loadFEN(fen, { keepArrows: true })` where you re-set arrows immediately afterwards
   (`scout.js` redraws plan arrows after a move), otherwise the new `keepArrows` option goes unused.
4. **§8.4 (CODE3-01 sibling)** — `scout.js:1458-1462` and `:1669, 1749` toggle
   `btnScoutRemoveFromRep` / `btnScoutTrainAfterImport` with `style.display`; switch to
   `visibility` + `disabled` (the HTML now bootstraps them `display:none` inline, which I left alone
   precisely because your JS overwrites `display`).
5. **§8.5 / CODE3-20 (part)** — the CSS I added (constant-height, non-wrapping
   `.scout-line-overlap-strip`, `nowrap` badges) also applies to `#scoutActiveLineOverlap`; please set
   `title` on your strip badges too, since the strip now clips instead of wrapping.
6. **CODE3-21 (part)** — the same missing CSS half (`flipped` label) is fixed for `#scoutEvalBarLabel`
   by my `style.css` change; please route scout's flip handler through an
   `applyEvalBarOrientation`-style helper that also re-renders the label, otherwise the Scout bar keeps
   the old placement until the next SSE frame.

### `frontend/repertoire_trainer.js` (CODE-4 agent)
7. **§8.4 (CODE3-01 sibling)** — `repertoire_trainer.js:500-503` and `:771-774` set
   `btnArrows.style.display = … 'inline-block' : 'none'`, an 8.4 violation inside a fixed toolbar. Switch
   to `visibility` + `disabled`; I left `index.html:543` with the inline `display:none` bootstrap because
   your code overwrites `display` and I must not break it unilaterally.
8. **CODE3-13 (part)** — `animateMove`'s completion contract changed (a cancel now *does* call
   `onComplete`). Any place that relied on the callback being *dropped* on cancel needs review; grep
   `animateMove(` and confirm each caller's callback is idempotent.
9. **CODE3-16 (part)** — `InteractiveChessboard` now has `destroy()`. There is no code path that
   rebuilds the trainer board today, but if one is added, call `destroy()` first or the two `window`
   pointer listeners will leak.
10. **CODE3-08 (part)** — if the trainer keeps its own eval cache, apply the same bounded-LRU +
    normalised-FEN key that `_evalCachePut` now provides in the builder.

### `frontend/app.js` (CODE-4 agent)
11. **CODE3-18 (cluster 1)** — `window.showToast` is defined **twice** (≈line 161 and ≈line 434) with
    different defaults and markup; the first is dead. Delete it. `repertoire_builder.js` now depends on
    `window.showToast` for **all** of its user feedback (`notify()` at
    `repertoire_builder.js:226-234`), so a change to the signature or the container id is now
    load-bearing.
12. **CODE3-09** — `window.promptStudySelection` is the cross-file dependency the two import entry
    points now guard against; if it is renamed, the guard's toast is the only user-visible signal, so
    please keep the name.
13. **CODE3-22** — `promptStudySelection` should reject (or resolve `null`) rather than throw on a
    server error; the import paths now catch and toast either way.

### `backend/**` (CODE-1 / CODE-2 agents) — **required** for a complete CODE3-03
14. **`POST /api/repertoire/seed` returns `null` on its success path**
    (`backend/api.py` (the `/api/repertoire/seed` handler): the handler falls off the end after the seeding loops, so FastAPI
    serialises JSON `null`). The frontend now handles it, but the endpoint is still lying to every
    other caller. Please return `{"status": "ok", "seeded_moves": n, "total_moves": m}` (the code that
    builds that dict at `api.py:1288-1293` is unreachable because of the missing `return`).
15. **`get_repertoire_tree()` pays for `get_game_gaps()` on every call**
    (`backend/repertoire.py:1404`). With CODE3-03 fixed the payload is finally rendered, so the cost is
    now justified — but `get_game_gaps()` still performs up to `max_explorer_lookups = 40` **live
    explorer HTTP calls** per `/api/repertoire/tree`, and `refreshTree()` runs on every study change,
    threshold change, import and tab switch. Please move the gap analysis behind an explicit
    opt-in query parameter (e.g. `?include_gaps=true`) or cache it, and have the frontend request it
    only when the Blunders tab is open.
16. **`game_gaps.gaps[].all_blunders[]` is missing `played_san`-adjacent fields the banner wants** — not
    a blocker (the frontend degrades to `?`), but `earlier_mistake` and `all_blunders` rows do not carry
    `best_san` / `cpl_loss`, so a chained root-cause walk would show blanks. Please project the same
    columns as the top-level row.
17. **`gaps[]` (coverage gaps) has no `id`**, only `parent_fen` + `opponent_uci`; the frontend keys the
    "Address this reply" button on `parent_fen` as a result. A composite `id` would be cleaner.
18. **`POST /api/repertoire/import_line` does not report *which* moves it replaced** when a user move
    diverges at a prepared turn (the `replaces` warning the Scout badges imply). The builder's toast
    currently only reflects `status` / `already_present`.

### `tests/**` (CODE-5 agent)
19. `tests/test_user_workflows.py:541` asserts only that the *string* `this.activeReviewGap = null`
    appears in the source — that is what let the whole blunder feature rot unnoticed. It still passes
    (`loadRepertoire()` and `reviewGame()` both assign it); consider strengthening it to assert the
    banner/gap containers exist, as `tests/test_builder_frontend_contracts.py` now does.
20. Add a lint rule for the CODE3-01/03 class: every literal `getElementById`/`querySelector` in
    `frontend/*.js` must resolve against `index.html` (my
    `tests/test_builder_frontend_contracts.py::test_every_literal_dom_lookup_resolves_in_index_html`
    implements exactly that; it can be generalised to `scout.js` / `repertoire_trainer.js` once the
    duplicate-SessionTreeTree hand-off above lands).

---

## Also noticed (not fixed)

* **`.scout-overlap-banner` (4 CSS rules) is still dead.** It is only ever applied by `scout.js`, which I
  do not own; I left it rather than delete a rule another agent's code may be about to use. My builder
  uses `.scout-overlap-badge` + `.scout-card-overlap-badges` instead.
* **`#scoutQuickAddNotice` (`index.html:786`) is a dead DOM node** — same situation (it belongs to
  `scout.js`'s `_showQuickAddNotice` surface, which I could not verify). I removed the builder's
  equivalent (`#repQuickAddNotice`) because I own the code that referenced it (none).
* **XSS hardening not applied to the pre-existing innerHTML builders.** `renderTreeLines()`
  (`repertoire_builder.js:3219-3301`) still interpolates `line.name`, `line.pgn`, `line.eco` and
  `line.plyCount` unescaped, and `renderCandidateTable()` interpolates `m.san`, `m.popularity_pct` etc.
  I added `_escapeHtml` / `_escapeAttr` and used them in the **new** markup (banner, both gap lists,
  overlap badges) but did not retrofit the two existing renderers — that is a separate, larger change
  to code whose escaping assumptions the backend also affects. **Recommend a follow-up ticket.**
* **`repGapThreshold`'s inline `<option value="5" selected>`** can disagree with
  `this.gapThreshold = 5.0` / the Settings form; harmless today, worth a hand-off to whoever owns the
  settings persistence.
* **`getGameGaps` is still called on every tree refresh** even when the user never opens the Blunders
  tab (see hand-off 15) — the UI is now correct, the *cost* is not.
* **A pre-existing race I did not fix:** `renderBreadcrumbs()` calls
  `chip.scrollIntoView({ behavior: 'smooth' })` on every position change, and `startEvalStream()` is
  started for a position the user may have already left. Neither is a correctness bug, but both are
  per-keystroke work; a `requestAnimationFrame` coalesce would help.
