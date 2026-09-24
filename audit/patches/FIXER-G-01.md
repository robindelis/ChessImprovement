# FIXER-G-01 — frontend fixer patch (`scout.js`, `repertoire_trainer.js`, `app.js`, `audio.js`)

- **Fixer:** FIXER-G
- **Scope (owned exclusively):** `frontend/scout.js`, `frontend/repertoire_trainer.js`,
  `frontend/app.js`, `frontend/audio.js`
- **Findings fixed:** SEC-01, SEC-02, CODE-01 … CODE-22, DUP-01 (intra-file part), DEAD-01
- **New tests:** `tests/test_scout_frontend_contracts.py` (38 tests) +
  `audit/scripts/FIXER-G-01_contracts.js` (90 behavioural checks, run under `node`)
- **Files NOT touched:** `frontend/style.css`, `frontend/index.html`, `frontend/board.js`,
  `frontend/repertoire_builder.js`, `backend/**`, `tests/**` (except the new test file)

> ⚠️ **Ownership collision resolved (read this first).** Another fixer (db.py boot-migration
> work) parked their `TestBootMigrationPreservesRepertoire` module at
> `tests/test_scout_frontend_contracts.py` mid-session. Their content is preserved
> byte-identical (md5 `a98e565b3b1b60b0d6d279265fbb0102`) at
> `/tmp/opencode/FIXER-P_test_backup.py` and `/tmp/opencode/test_migration_safety.py`;
> they have since re-published it themselves as `tests/test_scout_pipeline.py`.
> `tests/test_scout_frontend_contracts.py` now contains **only** the FIXER-G tests.
> Nobody lost work, but if a migration-safety module reappears at that path, move it
> aside — do not overwrite the FIXER-G file.

---

## 1. Per-finding root cause → change

### SEC-01 (critical) — stored XSS in the results grid

**Root cause.** `renderResults()` built every card with one `innerHTML` template literal and
interpolated fully remote fields (`item.title`, `item.channel`, `item.thumbnail`, `item.eco`,
`item.id`) plus an inline `onerror="this.src=…"` event handler. A video title is attacker
controlled and the grid renders on the app's own auto-search (`search('Caro-Kann Defense')`).

**Change.** A single escaping layer was added and the template moved into it.

`frontend/scout.js:25-70` (new pure helpers, delimited by the
`// <BEGIN SCOUT_PURE_HELPERS>` / `// <END SCOUT_PURE_HELPERS>` markers so the tests can
extract them verbatim):

```js
function escapeHtml(value) {                       // escapes & < > " ' ` =  (attribute-safe)
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"'`=]/g, (ch) => SCOUT_HTML_ESCAPES[ch]);
}
const SCOUT_RAW_HTML = Symbol('scoutRawHtml');
function rawHtml(markup) { return { [SCOUT_RAW_HTML]: true, markup: String(markup ?? '') }; }
function safeHtml(strings, ...values) {             // escapes EVERY interpolation
    let out = strings[0];
    for (let i = 0; i < values.length; i++) out += _scoutSerialize(values[i]) + strings[i + 1];
    return out;
}
function safeImageUrl(url) {                       // only /relative and http(s) survive
    const raw = (url ?? '').trim();
    if (!raw) return SCOUT_IMAGE_FALLBACK;
    if (raw.startsWith('/') || raw.startsWith('./') || raw.startsWith('../')) return raw;
    if (/^https?:\/\//i.test(raw)) return raw;
    return SCOUT_IMAGE_FALLBACK;                   // javascript:, data:, vbscript: -> blocked
}
```

`frontend/scout.js:189-210` — `scoutCardMarkup(item)` is now the only card template, written
with `safeHtml`, with the inline handler removed:

```diff
-        <img src="${thumb}" alt="${item.title}" class="scout-card-thumb" onerror="this.src='/assets/pieces/wN.svg'">
+        <img src="${safeImageUrl(src.thumbnail)}" alt="${title}" class="scout-card-thumb" data-fallback-src="${SCOUT_IMAGE_FALLBACK}">
```

`frontend/scout.js:906-949` — `renderResults()` maps through `scoutCardMarkup`, writes
`#scoutResultsCount` (was dead), and attaches the fallback with a real listener:

```js
grid.innerHTML = filtered.map(item => scoutCardMarkup(item)).join('');
grid.querySelectorAll('img.scout-card-thumb').forEach(img => {
    img.addEventListener('error', () => {
        const fallback = img.getAttribute('data-fallback-src') || SCOUT_IMAGE_FALLBACK;
        if (img.getAttribute('src') !== fallback) img.setAttribute('src', fallback);
    });
});
```

`frontend/scout.js:816-849` — the search/`loadRecent` error paths build a node with
`textContent` (they used to interpolate `${e.message}` into `innerHTML`).
`frontend/scout.js:951-986` — `analyzeSource()` replaced the blocking `alert()` with a toast.

**Verified by** `SEC-01.a…e` (harness) and
`test_untrusted_fields_are_not_interpolated_raw`, `test_no_inline_event_handlers_in_generated_markup`,
`test_thumbnail_url_scheme_is_validated`, `test_error_notice_uses_textcontent`.

### SEC-02 (critical) — XSS via line name / study name / server message

**Root cause.** `renderTreeLines()` and `_applyTreeBadges()` each built the line card with
`innerHTML`, interpolating `line.name` (a PGN `Event` header or a YouTube title),
`line.pgn`, `lineStudy`/`line.studyName` (a free-form, stored study name) and
`wData.message` / `bData.message` (server strings that embed client SAN). The same strings
were written twice (see CODE-19), so the fix had to be applied twice as well.

**Change.** `frontend/scout.js:212-292` — `scoutLineCardMarkup(line, index, ctx)` is the single
card template (`safeHtml` throughout, dynamic `title` on every clamped span, `data-line-id` for
CODE-06). `frontend/scout.js:2237-2281` — `_applyTreeBadges()` now produces badge markup with
`overlapBadgeMarkup()` and updates button labels through `overlapActionInfo()` instead of
re-writing a second copy of the strings. `frontend/scout.js:988-1018` — `openStudio()` initialises
`#btnScoutFav` from `study.is_favorite` and drops the dead `scoutCoreIdea` / `scoutKeyPlans` /
`scoutTypicalMistakes` writes (see CODE-15).

**Verified by** `SEC-02.a…c` and `test_escaping_helper_exists_and_is_used`.

### CODE-01 (critical) — board-flip orientation used as the repertoire key

**Root cause.** Six sites derived the *target repertoire* from `this.orientation`, a purely
cosmetic toggle: `saveMoveNote` (1100), `_onArrowsChange` (1145), the Remove button (1460),
`removeCurrentLineFromRepertoire` (1841), `onNewStudyClick` (891), `assignLineStudy` (919) and
`trainThisOpening` (2007). One "Flip Board" click + a typed note made the client POST
`{repertoire:"black", parent_fen:<start>, move_uci:"e2e4"}`; the backend *validates* that as a
legal opponent node (`backend/repertoire.py:483-490`, the is_user_move guard) and INSERTs `1.e4` at the root of the
**Black** repertoire while the UI says "Saved ✓". `markLastMoveAsImportant` was worse: it looped
over **both** repertoires, creating the move in White *and* Black.

**Change.** Orientation is now cosmetic everywhere. Three resolvers, all derived from the
line/tree identity:

```js
// frontend/scout.js:1821-1836
_repertoireForMove(moveObj) {
    if (!moveObj) return this.lastActionRepertoire;
    const builder = window.repertoireBuilder;
    if (builder && typeof builder.findMoveInTree === 'function' && moveObj.fen_before && moveObj.uci) {
        const stored = builder.findMoveInTree(moveObj.fen_before, moveObj.uci);   // authoritative
        if (stored && (stored.repertoire === 'white' || stored.repertoire === 'black')) {
            return stored.repertoire;
        }
    }
    let color = moveObj.color;                     // else: the side that plays this move
    if (color !== 'white' && color !== 'black') {
        const turn = normalizeTreeFen(moveObj.fen_before).split(' ')[1];
        color = (turn === 'b') ? 'black' : 'white';
    }
    return color === 'black' ? 'black' : 'white';
}
_targetRepertoire(explicit)   // :1839 records an explicit button choice (import/delete)
_lineStudyRepertoire(moves)   // :1852 first move's side, else lastActionRepertoire
```

Applied at `scout.js:1858` (`saveMoveNote`), `:1916` (`_onArrowsChange`), `:2117`
(`checkActiveLineOverlap` → Remove button), `:2514` (`markLastMoveAsImportant`, now **one**
repertoire, and it warns if the server stored it elsewhere), `:2595`/`:2676`/`:2696`
(delete flows), `:1578`/`:1604` (study creation/assignment), `:2810` (`trainThisOpening`).
`saveMoveNote` additionally cross-checks the server's echo (`data.move.repertoire`) and refuses
to print "Saved ✓" if the write landed in another repertoire (`scout.js:1883-1900`).

`this.orientation` now survives only in 5 cosmetic reads (field init, board construction,
`flipBoard()`, `board.setOrientation`, eval-bar mirroring) — asserted by
`test_no_orientation_derived_repertoire_key`.

**Verified by** `CODE-01.a[orientation=white|black]`, `CODE-01.b[…]`, `CODE-01.c` and
`CODE-09.d` (the DELETE body), plus the 4 static tests in
`TestScoutRepertoireKeyContracts`.

### CODE-02 (high) — `deleteLine()` orphaned `currentNode`

**Root cause.** After pruning, `currentNode` could stay on a removed node; the next
`addMove()` grafted onto the orphan, so `getAllLines()` returned 0 lines and the move the user
just played silently disappeared.

**Change.** `frontend/scout.js:434-476`: new `isAttached(node)` (walks up *and* verifies the
node is still listed in the parent's `children` — a pruned node keeps its `parent` pointer, so an
upward walk alone reports it attached), and `deleteLine()` now falls back to the highest still
attached ancestor. `frontend/scout.js:2711-2732` — `deleteSessionLine()` also re-syncs the board
(FEN, last-move highlight, breadcrumb, eval) when the current node moved.

**Verified by** `CODE-02.a/b`, `CODE-17.d`, `test_delete_line_keeps_current_node_attached`.

### CODE-03 (high) — invalid PGN for black-first lines

**Root cause.** `_buildPgn` emitted `e5 2. d4`: a black-first line got no `1...` prefix **and**
the move number was bumped after white's move.

**Change.** `frontend/scout.js:493-519` — the move number now advances on Black's move only, and
a line that starts with Black is prefixed `1... `:

```diff
-            if (isWhite) { res += `${moveNum}. ${cleanSan} `; }
-            else { res += `${cleanSan} `; moveNum++; }
+            if (color === 'white') { res += `${moveNum}. ${cleanSan} `; }      // no bump
+            else { res += (i === 0) ? `${moveNum}... ${cleanSan} ` : `${cleanSan} `; moveNum++; }
```

**Verified by** `CODE-03.a` (`"1... e5 2. d4"`), `CODE-03.b` (`"1. e4 e5 2. Nf3"` unchanged),
`test_pgn_is_valid_for_black_first_lines`.

### CODE-04 (high) — import claimed success on total failure; replacement warning never shown

**Root cause.** The success branch keyed only off `data.status === 'success'`, which the backend
returns even when `imported_count === 0` and `errors` is full. The `divergence_info`
("Nc3 replaces your prepared Nf3") lived in `message` and was never surfaced, and a divergent
import silently deleted a prepared move.

**Change.** Three new/rewritten methods in `scout.js`:

- `frontend/scout.js:2286-2309` `_cachedOverlapForMoves()` / `_confirmDestructiveImport()` —
  uses the overlap result that is **already cached** by the debounced check (no extra request) and
  `confirm()`s *before* a destructive import.
- `frontend/scout.js:2311-2339` `_reportImportResult()` — decides from
  `imported_count` / `errors` / `overlap_details`:
  `imported === 0 && !already_present` → red `✗ Import into <Rep> repertoire failed: <errors>` and
  `return false` (no chime, no builder reload, no "Train in Trainer"); partial imports report
  `added N moves`, skipped moves, and the `⚠️ At move N, X REPLACES your prepared Y` line.
- `frontend/scout.js:2341-2399` `_submitLineImport()` — checks `res.ok` and
  `data.status !== 'success'` explicitly; both `importToRepertoire` and
  `importAllLinesToRepertoire` now share it (de-duplication, DEAD-01-adjacent), and both wrap the
  `await window.promptStudySelection(...)` in `try/catch` so a rejected modal (CODE-18) aborts
  **visibly**.

**Verified by** `CODE-04.a…i` and `test_import_reports_imported_count_and_errors`,
`test_divergent_import_is_confirmed_first`.

### CODE-05 (high) — no generation counter (scout + trainer)

**Change.**
- `scout.js:816` `search()` — `const seq = ++this.searchSeq` + `AbortController`, `if (seq !== this.searchSeq) return;` before touching `cachedResults`/DOM, and the loading indicator is only hidden by the current generation.
- `scout.js:951` `analyzeSource()` — `++this.analyzeSeq`; a superseded analysis cannot open the studio.
- `scout.js:2117` / `:2187` `checkActiveLineOverlap()` / `checkTreeOverlap()` — own seq + controller, `AbortError` swallowed.
- `repertoire_trainer.js:301` / `:367` `loadMasteryProgress()` / `loadOpeningsList()` — capture `color` + `study` + `progressSeq`/`openingsSeq` and re-validate before painting; `loadOpeningsList({progress})` accepts the in-flight progress promise so one drill start costs **2** requests instead of 3 (was `Promise.all([loadOpeningsList(), loadMasteryProgress()])` firing the same URL twice).

**Verified by** `CODE-05.a/b`, `DEAD-01.d`, `test_search_and_analyze_are_guarded`,
`test_trainer_loaders_are_guarded_and_deduplicated`.

### CODE-06 (high) — `treeOverlapMap` keyed by positional index

**Root cause.** `checkTreeOverlap` sent `id: String(idx)` and stored the reply under the same
positional id; `renderTreeLines` and `_applyTreeBadges` read it back by `String(idx)`. Inserting a
line shifts every index, so badges land on the wrong card (and the `data-idx` handlers can then
import/delete a different line).

**Change.** `scout.js:2055-2071` `_lineKey(line)` / `_overlapForLine(line, repertoire)`;
`getAllLines()` now exposes `lineId: node.id` (`scout.js:408`). `checkTreeOverlap` sends
`id: this._lineKey(l)` (`scout.js:2192`), the reply is stored under that id, and
`_applyTreeBadges` reads `card.dataset.lineId` (`scout.js:2247`) and bails out entirely when the
card set no longer matches the snapshot it computed for.

**Verified by** `CODE-06.a/b/c`, `test_overlap_is_keyed_by_node_identity`.

### CODE-07 (high) — uncleared 550 ms / 800 ms trainer timers

**Root cause.** The opponent reply and the mistake reset were anonymous `setTimeout`s; only
`autoNextTimeout` was tracked, so a replaced drill got a phantom `playMove`, a FEN overwrite and
stale arrows.

**Change.** `repertoire_trainer.js:26` `drillToken` + `opponentReplyTimeout` + `mistakeResetTimeout`;
`:184` `_clearDrillTimers()`; `:478` `startNewDrill()` bumps the token and clears all three;
`:736` `onUserAttempt` captures the token and every deferred continuation
(`:753` reply, `:804`/`:889`/`:915` auto-advance, `:959` mistake reset) starts with
`if (drillToken !== this.drillToken) return;`.

**Verified by** `CODE-07.a/b/c`, `test_trainer_deferred_continuations_are_owned`.

### CODE-08 (high) — `saveSettings()` reported success unconditionally

**Change.** `frontend/app.js:105-187`: `loadSettings()` checks `res.ok`; `saveSettings()` reads
`res.ok` + the server `detail`, shows `⚠ Settings NOT saved: <detail>` (red, 5 s) and **returns
without** applying the gap threshold; the happy path clears `#inputToken` / `#inputGeminiKey` so
the raw `lip_…` secret is not retained or re-submitted, and a non-numeric gap is rejected with a
warning instead of writing `NaN` into the builder. The dead `#settingsAlert` writes are gone
(§8.1: toasts only) — see hand-off (b).

**Verified by** `CODE-08.a[400]`, `CODE-08.a[500-html]`, `CODE-08.b[…]`, `CODE-08.c`,
`test_save_settings_checks_the_response`.

### CODE-09 (high) — `DELETE /api/repertoire/move` always answered `ok`

**Root cause (backend).** `backend/api.py::delete_repertoire_move` (`:1865` as of this patch) discards
`delete_repertoire_branch()`'s row count and answers `{"status":"ok"}` unconditionally, so the UI
celebrated no-op deletes (and then undid the user's board position).

**Change (client).** `scout.js:2595-2644` `_deleteRepertoireBranch()`:
1. non-2xx → red `✗ Remove failed: <detail>`, nothing else happens;
2. `deleted_count` is used **when the server provides it** (forward-compatible with the backend
   change in hand-off (b));
3. otherwise the move is re-checked with `POST /api/scout/check_line` — if it is still present,
   the result is `removed: 0` and `_reportRemoval()` (`:2646`) says
   *"Nothing was removed — `<move>` is not in your <Rep> repertoire."*
   `removeCurrentLineFromRepertoire()` only calls `undoUserMove()` when something was really
   removed, and the confirm dialog now names the move it will actually delete.

**Verified by** `CODE-09.a…e`.

### CODE-10 / CODE-11 (high) — the YouTube↔board sync did not exist; no API retry

**Implemented, not removed** (the AGENTS §4E feature was specified but never built).
`frontend/scout.js:1100-1334`:

- `setupYouTubePlayer()` registers `onReady`, **`onStateChange`** and `onError`; it no longer sets
  `ytPlayerReady = true` optimistically after `loadVideoById` (a chapter chosen in the first
  moments of a new video used to seek in the *old* timeline).
- `_onYtStateChange()` toggles `#scoutLiveIndicator` (`live` / `idle` classes + tooltip) and
  starts/stops a 500 ms poll; `_updateVideoSyncUi()` drives `#scoutCurrentTimeDisplay`
  (`0:12 / 10:00`) and syncs the board.
- **Two-way sync**: `openStudio()` builds `moveTimestamps` (`uci → timestamp` from the study's
  variations, `scout.js:1013-1017`); playing a board move seeks the player
  (`_syncVideoToMove`, `:1187`), and the video time moves the board highlight
  (`_syncBoardToVideoTime`, `:1195`).
- **CODE-11 retry**: `_scheduleYouTubeApiRetry()` polls every 500 ms (bounded, 40 attempts) and
  upgrades the fallback `<iframe>` into a real `YT.Player` as soon as `window.YT` appears; when it
  never does, the chapter `<select>` is **disabled with an explanatory tooltip** instead of
  silently doing nothing, and `seekVideoTo()` returns `false` and retries.
- Also now driven: `#scoutResultsCount` (`renderResults`), `#scoutLoadingText`
  (`search` / `analyzeSource`), `#btnScoutFav` (`openStudio` / `toggleFavorite`).

**Verified by** `CODE-11.a…g`, `test_youtube_sync_elements_are_driven`.

### CODE-12 / CODE-13 / CODE-14 / CODE-21 (§8 layout stability)

All four are fixed **from JS** (inline styles), so no CSS is required for correctness — the CSS
rules in hand-off (a) make them belt-and-braces.

- `repertoire_trainer.js:210` `_updateNoteBox()` — the note callout reserves
  `min-height:44px; max-height:44px; overflow:hidden` and toggles `visibility`/`opacity`; the text
  is clamped (`nowrap` + `ellipsis` + `title`).
- `repertoire_trainer.js:231` `_updateFocusBanner()` — `display:flex` + fixed `min-height` +
  `visibility`, and `#btnClearTrainFocus.disabled` follows.
- `repertoire_trainer.js:195` `_setConditional()` and `:252` `_updateImportantBadge()` —
  `visibility` + `disabled` + fixed `min-width` (the badge keeps a 132 px box so the
  `flex-wrap` header cannot reflow); used for `#trainImportantBadge` and
  `#btnTrainToggleArrows`.
- `scout.js:581` `_primeQuickRepToolbar()` — reserves `min-width` for the three quick-rep buttons
  and switches the two conditional ones (`#btnScoutRemoveFromRep`, `#btnScoutTrainAfterImport`) to
  `visibility:hidden` from the first paint; `checkActiveLineOverlap()` (`:2131`) and
  `_submitLineImport()` (`:2370`) only flip `visibility`/`disabled` afterwards.
- `scout.js:294` `_clampInline(el, maxWidth)` + `title` for `#scoutCurrentOpeningTag`,
  `#scoutTurnBadge`, `#scoutNoteMoveLabel`; `#btnScoutMarkImportant` keeps a 212 px min-width
  across its two labels.
- **CODE-21**: `scheduleOverlapCheck()` is unchanged (120 ms debounce) but both checkers now abort
  their in-flight request and drop superseded results, and `_applyTreeBadges` refuses to patch a
  panel that was re-rendered from a newer tree.

**Verified by** `CODE-12.a…e`, `CODE-13.a…c`, `CODE-14.a`,
`test_conditional_trainer_elements_use_visibility`, `test_quick_rep_toolbar_keeps_a_fixed_box`,
`test_dynamic_text_elements_are_clamped_with_a_title`.

### CODE-15 (medium) — seven `getElementById` ids absent from `index.html`

**Change.** All seven dead lookups removed (`btnImportWhite`, `btnImportBlack`,
`scoutActiveLineOverlapBanner`, `scoutCoreIdea`, `scoutKeyPlans`, `scoutTypicalMistakes`,
`scoutImportAlert`) — the surviving equivalents are `#btnScoutAddMoveWhite` / `#btnScoutAddMoveBlack`,
which are already wired. `importToRepertoire()` now uses the floating toast only
(`scout.js:2409-2412`).

**Verified by** `test_no_ids_are_looked_up_that_do_not_exist_in_the_html` (now *all* 60+ ids
cross-checked against `index.html`) and `test_dead_methods_are_removed`.

### CODE-16 (medium) — all three non-`all` filters were no-ops; `loadRecent()` dead

**Change.** `scout.js:890-904` `_filterResults()` matches the literals the shelf buttons actually
carry (`all` / `youtube` / `eco_guide` / `recent`, plus `favorites` for the pending button — see
hand-off (b)); the filter click handler calls `loadRecent()` for `recent`
(`scout.js:652-658`), which finally gives `GET /api/scout/recent` a production caller
(`scout.js:851-888`).

**Verified by** `CODE-16[youtube|eco_guide|favorites|all]`, `CODE-16.recent`,
`test_filter_vocabulary_matches_the_shelf_buttons`.

### CODE-17 (medium) — FEN-format split, dead "transposition" clause, `children[0]`

**Change.** `scout.js:86-88` `normalizeTreeFen()` (first 4 fields = the backend's `normalize_fen`),
applied in `addMove()` (`:350-353`, the FEN clause now only fires when there is no UCI, so it is
honest instead of dead), `getAllUniqueMoves()` (`:479`), `_onUserBoardMove()` (`:1338-1342`,
so `lastBoardFen` stops alternating between 4 and 6 fields), and the metadata sync was widened to
the builder's richer set (`comment` / `is_important` / `arrows`, `:371-378`).
`scout.js:1436-1440` `_preferredChild()` — `Next ▶` / `End ⏭` follow the most recently created
branch (deterministic) instead of `children[0]`.
Bonus, same function: a line is flagged "Active" only when `currentNode` really is on its path
(`scout.js:2073-2085` `_isOnActivePath`) — the old UCI-prefix test flagged every descendant line.

**Verified by** `CODE-17.a…d`, `CODE-06.b`, `test_fens_are_canonicalised`,
`test_navigation_follows_a_deterministic_branch`, `test_line_card_is_marked_active_by_node_identity`.

### CODE-18 (medium) — `promptStudySelection` had no re-entrancy guard

**Change.** `frontend/app.js:192-210`: one in-flight promise is shared by all concurrent callers
(`if (studyPromptPending) return studyPromptPending;`), released in a `then` after the
constructor returns (so a synchronous settle cannot leave the slot set), `showModal()` is wrapped
in `try/catch`, `settleOnce()` guarantees a single resolution, and the study-creation POST is
validated (`if (!createRes.ok)`) with the error shown **in the modal** instead of resolving a name
for a study that does not exist. The studies `fetch` no longer mutates a closed dialog
(`isLive()` guard, `app.js:274-333`).

**Verified by** `CODE-18.a/b/c`, `test_study_prompt_is_reentrancy_safe`.

### CODE-19 (medium) — three divergent badge renderers

**Change.** One renderer (`scout.js:107-166` `overlapBadgeInfo` / `overlapBadgeMarkup` /
`overlapActionInfo`) produces the AGENTS §4E wording — `✓ In White`, `⚡ White: 4/8 in Rep (+4 new)`,
`★ New Line` (the new-line badge carries the repertoire in its `title` because §4E's literal form
does not) — and it is used by the line cards, the async refresh and the strip under the board
(`scoutOverlapStripMarkup`, `:282`). The divergent strings (`★ White New`, `✓ White (4/8)`,
`⚡ … (+4 new)` without "in Rep") no longer exist anywhere.

**Verified by** `CODE-19.a/b/c`, `test_single_badge_renderer_with_section_4e_wording`.

### CODE-20 (low) — audio: no master bus, no try/catch, ignored `resume()`, chime on deletes

**Change.** `frontend/audio.js` rewritten (141 lines): one master bus
`gain(0.8) → DynamicsCompressor(threshold −6, ratio 20, attack 3 ms, release 250 ms) → destination`;
every voice is scheduled through `_voice()` (`:72-106`) with a 12-voice cap and
`onended → disconnect`; `_init()` (`:26-70`) catches constructor failures, latches
`unavailable`, swallows the `resume()` rejection and **drops** the sound while the context is
still suspended (this is what used to queue every note at a frozen `currentTime` and fire them as
one burst). New `playRemove()` (`:136`) is the neutral, non-triumphal sound for destructive
actions, and `markLastMoveAsImportant` only plays `playSuccess()` when the flag really persisted.

**Verified by** `CODE-20.a…f`, `test_audio_uses_a_master_bus_and_never_throws`,
`test_destructive_actions_do_not_play_the_success_chime`.

### CODE-21 (medium) — 4 un-cancellable fetches per move; overlap applied to a changed tree

Covered with CODE-05/CODE-06 above: `_postOverlapPair()` sends one signal to both requests,
`AbortController` per check, sequence guards, and the snapshot guard in `_applyTreeBadges`.
Post-import refreshes now go through the debounced `scheduleOverlapCheck()` instead of firing a
third, immediate pair.

### CODE-22 (low) — keyboard handler hijacked `<select>`, swallowed Ctrl/Cmd+Z

**Change.** `frontend/scout.js:335-360`: the guard now tests `e.target` (not
`document.activeElement`) and covers `input` / `textarea` / `select` / `isContentEditable`;
`if (e.ctrlKey || e.metaKey || e.altKey) return;` leaves browser shortcuts alone, and key-repeat
no longer spams `undoUserMove()`.

**Verified by** `CODE-22.a…d`, `test_keyboard_handler_respects_form_controls`.

### DUP-01 (medium) — `SessionLineTree` fork

**Intra-file de-duplication done:** the two badge renderers collapsed into one (CODE-19), the two
import flows collapsed into `_submitLineImport()`/`_reportImportResult()`, the two overlap fetch
pairs collapsed into `_postOverlapPair()`, and `scout.js`'s `addMove` metadata sync was widened to
the builder's richer (canonical) field set. **Cross-file consolidation is a hand-off item** (b) —
`repertoire_builder.js` is FIXER-F's file and still carries its own copy of the two classes.

### DEAD-01 (medium) — dead methods/locals, duplicate `showToast`, CSS-less `feedback-box.mistake`

- `app.js`: the **first** (dead) `window.showToast` definition (25 lines, `borderLeft`,
  `toast-fadeout` class that has no CSS) removed — one definition remains (`app.js:497`).
- `scout.js`: `renderVariations(variations)` and the two alias methods
  `addCurrentMoveToRepertoire` / `addPlayedLineToRepertoire` removed; the two dead locals in
  `streamEvaluation()` removed; `loadRecent()` is now called (CODE-16); the unreachable
  `'favorites'` branch is now reachable from a filter button (hand-off (b)).
- `repertoire_trainer.js:945`: `'feedback-box mistake'` → `'feedback-box blunder'` (the only
  equivalent rule that exists in `style.css`; the alternative is hand-off (a)).
- `SessionTreeNode.id` is no longer dead — it is the overlap key (CODE-06).
- `saveSettings()`'s dead `const data = await res.json()` assignment is gone (CODE-08).

**Verified by** `DEAD-01.a…d`, `test_window_show_toast_is_defined_once`,
`test_feedback_mistake_class_has_a_css_rule`, `test_dead_methods_are_removed`.

---

## 2. Test evidence (before / after)

### 2.1 Behavioural harness under `node`

`audit/scripts/FIXER-G-01_contracts.js` loads the **actual shipped sources** into a sandbox with
a fake DOM + scriptable `fetch` and drives the production functions. It accepts
`--source/--trainer/--app/--audio`, so it can be pointed at a pre-fix tree.

Pre-fix sources = the state CODE-4 audited (2052/880/476/114 lines; snapshot preserved at
`/tmp/opencode/prefix_frontend/`, md5 of the scout copy `f3320e82bef1cbb9ddec46e641cd46b0` — see §5).

```
$ node audit/scripts/FIXER-G-01_contracts.js \
      --source /tmp/opencode/prefix_frontend/scout.js \
      --trainer /tmp/opencode/prefix_frontend/repertoire_trainer.js \
      --app /tmp/opencode/prefix_frontend/app.js \
      --audio /tmp/opencode/prefix_frontend/audio.js
checks: 85, passed: 17, failed: 68
failing ids: SEC-01.b, SEC-01.c, SEC-01.d, SEC-01.e, SEC-02.b,
  CODE-01.a[orientation=black], CODE-01.b[orientation=white], CODE-01.c,
  CODE-02.a, CODE-02.b, CODE-03.a, CODE-05.a, CODE-05.b, CODE-06.a, CODE-06.b, CODE-06.c,
  CODE-07.a, CODE-07.b, CODE-07.c, CODE-08.a[400], CODE-08.b[400], CODE-08.a[500-html],
  CODE-08.b[500-html], CODE-08.c, CODE-09.a … CODE-09.e, CODE-04.a … CODE-04.i,
  CODE-16[youtube], CODE-16[eco_guide], CODE-17.d, CODE-19.a, CODE-19.c, CODE-20.a … CODE-20.f,
  CODE-22.a, CODE-22.b, CODE-22.d, CODE-15.a, DEAD-01.a, DEAD-01.b, DEAD-01.c, CODE-12.a,
  CODE-13.a, CODE-12.e, CODE-14.a, CODE-11.a, CODE-11.c … CODE-11.g, CODE-18.a, CODE-18.b,
  DEAD-01.d
exit=1
```

Selected **before** failures (the money shots):

```
[FAIL] SEC-01.b  no <script>/event-handler/svg/javascript: sink survives in the card grid
  -> sinks=["/<\s*img[^>]*\bon(error|load|click|mouseover)\s*=/i", "/\son(error|...)\s*=\s*["']/i",
            "/javascript\s*:/i", "/<\s*svg/i"]
     markup= <img src="javascript:alert(3)" alt="<img src=x onerror=alert(1)>"><svg onload=alert(2)>"
                  class="scout-card-thumb" onerror="this.src='/assets/pieces/wN.svg'">
[FAIL] SEC-02.b  no ... sink survives in a line card -> sinks=["/<\s*img[^>]*\bon.../i", "/<\s*svg/i"]
[FAIL] CODE-01.a[orientation=black]  saveMoveNote() targets the White repertoire for a White move
  -> {"repertoire":"black","parent_fen":"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -",
      "move_uci":"e2e4","comment":"flip test note"}
[FAIL] CODE-01.c  12 executable reads of this.orientation:
   891/919/1100/1145/1460/1841: const rep = (this.orientation === 'black') ? 'black' : 'white';
[FAIL] CODE-02.a  the move played after a delete stays in the session tree -> []
[FAIL] CODE-03.a  black-first line emits valid PGN -> "e5 2. d4"
[FAIL] CODE-05.a  the newer search result is what stays on screen -> ""
[FAIL] CODE-07.a  the stale 550ms opponent reply does not play on the new drill
  -> [["playMove","e7","e5"],["clearArrows"]]
[FAIL] CODE-07.c  the stale 800ms mistake reset does not reload the new drill FEN -> ["AFTER-NEW-DRILL"]
[FAIL] CODE-08.a[400]  no success toast when the server answers 400
  -> [{"msg":"Settings saved successfully!","color":"#4ade80"}]
[FAIL] CODE-09.a  no "Removed" toast when deleted_count is 0
  -> [{"msg":"AUDIO_SUCCESS"},{"msg":"✓ Removed from Black Repertoire","color":"#ef4444"}]
[FAIL] CODE-04.g  a divergent import asks for confirmation BEFORE destroying the prepared move -> []
[FAIL] CODE-16[youtube]  filter "youtube" shows exactly ["VIDEO","FAV"] -> leaked=["GUIDE","STUDY"]
[FAIL] CODE-18.b  showModal() is called at most once -> calls=2
[FAIL] CODE-20.a  every voice is routed through a limiter, not straight to destination
[FAIL] CODE-11.f  chapter seeking works after the retry -> undefined null
```

After the patch:

```
$ node audit/scripts/FIXER-G-01_contracts.js
checks: 90, passed: 90, failed: 0
exit=0
```

### 2.2 Python contract suite

```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_scout_frontend_contracts
Ran 38 tests in 2.5s
OK

$ SCOUT_FRONTEND_DIR=/tmp/opencode/prefix_frontend \
      /home/robin/miniconda3/bin/python3 -m unittest tests.test_scout_frontend_contracts
Ran 38 tests in 2.5s
FAILED (failures=38)          # every static + every node-driven check
```

### 2.3 Full suite

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests
Ran 305 tests in 15.8s
FAILED (failures=8, errors=2)
```

All 38 FIXER-G tests pass inside that run. Every failure/error is in a **backend** test module
owned by other fixers and the failing set **changes between identical runs** while `backend/**`
and `tests/**` are being edited concurrently. Successive samples taken while I worked:
`Ran 305 … failures=8, errors=2` → `Ran 305 … failures=4, errors=1` → `Ran 193 … failures=2,
errors=9` (mid-edit import errors in `test_api`, `test_scout`, `test_repertoire`, …) →
`Ran 265 … failures=9, errors=12` (a new `tests/test_isolation_guard.py` appeared and its
repo-wide scans fail on *other* agents' files while it is being added).

Blame is never mine: the affected ids are `test_repertoire.*`, `test_scout.*`, `test_studies.*`,
`test_api_contract.test_00_contract_checks`, `test_openings`, `test_pedagogical_trainer.*`,
`test_user_workflows`, `test_isolation_guard.*`, `test_repertoire_write_safety.*` — none of them
executes frontend JS, and my patch touches no backend module and no existing test. Verified for
`test_isolation_guard`: it passes 12/12 when run alone, and my module is compliant with each of
its rules (first non-stdlib import is `tests.test_setup` at line 40; no `38281`, no
`chess_improvement.db` literal, no fail-open `CHESS_DB_PATH` default). Some of the flakiness is
order dependence on the shared temp DB: e.g. `test_scout.TestOpeningScout.test_single_played_move_import`
fails in a full run and passes alone.

---

## 3. Hand-off (a) — required `frontend/style.css` rules

None of these are required for correctness (the JS sets the same properties inline so the fix works
on today's HTML), but they make the layout stable from the first paint and cover the classes the JS
emits but that have no rule at all. Apply after FIXER-F lands.

```css
/* CODE-14 — fixed box for the quick-rep toolbar (mirrors _primeQuickRepToolbar) */
.btn-quick-rep { min-width: 150px; justify-content: center; }
#btnScoutMarkImportant { min-width: 212px; justify-content: center; }
#btnScoutRemoveFromRep,
#btnScoutTrainAfterImport { min-width: 150px; justify-content: center;
    visibility: hidden; opacity: 0; transition: opacity 0.18s ease; }
#btnScoutRemoveFromRep:not([disabled]),
#btnScoutTrainAfterImport:not([disabled]) { visibility: visible; opacity: 1; }

/* CODE-12 — the trainer note callout: constant height, clamped text (§8.1/§8.2) */
#trainNoteBox { min-height: 44px; max-height: 44px; overflow: hidden;
    transition: opacity 0.18s ease; }
#trainNoteContent { display: block; max-width: 100%; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }

/* CODE-13 — focus banner + priority badge: reserve the box, toggle visibility */
#trainFocusBanner { display: flex; min-height: 34px; box-sizing: border-box;
    transition: opacity 0.18s ease; }
#trainImportantBadge { display: inline-flex; min-width: 132px; justify-content: center;
    transition: opacity 0.18s ease; }
#btnTrainToggleArrows { min-width: 96px; justify-content: center; }

/* CODE-14 — the elements whose dynamic text changes length (§8.2) */
#scoutCurrentOpeningTag, #scoutNoteMoveLabel, #scoutTurnBadge {
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: inline-block;
}
#scoutCurrentOpeningTag { max-width: 240px; }
#scoutNoteMoveLabel { max-width: 160px; }
#scoutTurnBadge { max-width: 110px; }

/* CODE-19 — the badge strip must never re-wrap when the text changes */
.scout-card-overlap-badges { min-height: 20px; flex-wrap: nowrap; overflow: hidden; }

/* DEAD-01(j) — classes the JS emits that currently have NO rule at all */
.scout-badge-youtube, .scout-badge-guide {
    display: inline-block; font-size: 0.66rem; font-weight: 700; letter-spacing: 0.02em;
    padding: 0.1rem 0.4rem; border-radius: 999px; text-transform: uppercase;
}
.scout-badge-youtube { background: rgba(239, 68, 68, 0.18); color: #fca5a5; border: 1px solid rgba(239,68,68,0.35); }
.scout-badge-guide   { background: rgba(56, 189, 248, 0.15); color: #7dd3fc; border: 1px solid rgba(56,189,248,0.35); }
.badge-eco { font-size: 0.68rem; font-weight: 700; color: var(--accent-primary);
    background: rgba(56,189,248,0.12); border-radius: 4px; padding: 0.05rem 0.3rem; }

/* CODE-11 — the YouTube sync indicator (the base rule already exists at style.css:2260) */
.scout-live-indicator.idle { background: #475569; box-shadow: none; animation: none; }
.scout-live-indicator.live { background: #22c55e; box-shadow: 0 0 6px #22c55e; animation: livePulse 1.8s infinite; }

/* CODE-15 — dead CSS with no HTML consumer any more */
.scout-overlap-banner { display: none; }        /* or delete the rule block entirely */

/* DEAD-01(i) — OPTIONAL: if the reviewer prefers a distinct "mistake" look, add it and revert
   repertoire_trainer.js:945 to 'feedback-box mistake':
.feedback-box.mistake { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35);
    color: #fca5a5; padding: 0.6rem 0.8rem; border-radius: 6px; font-weight: 600; margin-top: 0.5rem; }
```

---

## 4. Hand-off (b) — required `index.html` / backend changes

### `frontend/index.html` (FIXER-F / HTML owner)

1. **§8 first-paint reservation** (the JS already overrides these inline, this removes the
   initial `display:none` flash and the one-shot width change):
   - `index.html:775` `#btnScoutRemoveFromRep` → replace `style="display:none; …"` with
     `style="visibility:hidden; min-width:150px; color:#ef4444; border-color:rgba(239,68,68,0.35);"`.
   - `index.html:781` `#btnScoutTrainAfterImport` → replace `style="display:none; font-size:0.75rem; padding:0.22rem 0.5rem;"`
     with `style="visibility:hidden; min-width:150px; font-size:0.75rem; padding:0.22rem 0.5rem;"`.
   - `index.html:582` `#trainNoteBox` → replace `style="display:none; …"` with
     `style="visibility:hidden; min-height:44px; max-height:44px; overflow:hidden; …"`.
   - `index.html:553` `#trainFocusBanner` → replace `style="display:none; …"` with
     `style="visibility:hidden; display:flex; min-height:34px; …"`.
   - `index.html:568` `#trainImportantBadge` → replace `style="display:none; …"` with
     `style="visibility:hidden; min-width:132px; justify-content:center; …"`.
   - `index.html:543` `#btnTrainToggleArrows` → `display:none` → `visibility:hidden`.
2. **Dead element**: delete `<div id="settingsAlert" …>` (`index.html:892`) — all settings
   messages now go through the floating toast system (§8.1).
3. **Dead element**: delete `<div id="scoutQuickAddNotice" style="display:none !important;">`
   (`index.html:786`) — never written by any JS; notices are toasts.
4. **Optional new filter button** (CODE-16 `favorites` branch is implemented and unit-tested but
   has no button): add next to `index.html:655-658`
   `<button type="button" class="scout-filter-btn" data-filter="favorites">★ Bookmarked</button>`.
   `#btnScoutFav` toggling now updates `cachedResults` and re-renders when this filter is active.
5. **Optional, restores the removed §4E panel** (CODE-15(b)): if the "strategic ideas" panel
   should come back, re-add a container with ids `scoutCoreIdea`, `scoutKeyPlans`,
   `scoutTypicalMistakes` — the backend already computes `study.strategic_ideas` on every analyse
   and the client will fill them in one line each (`textContent` only). Otherwise update
   `AGENTS.md` §4E, which still advertises core ideas / key plans / typical mistakes.
6. `AGENTS.md` §4E also still advertises "play back lines in sync with the interactive board" —
   now true (CODE-10/11); consider documenting the two-way sync and the `deleted_count` contract.

### `backend/api.py` (FIXER-B) — required for CODE-09

> Line numbers below are from the tree as I finished; FIXER-B is editing this file
> concurrently, so match by **function name**.

`delete_repertoire_move()` (`@app.delete("/api/repertoire/move")`, `backend/api.py:1865`
as of this patch) must report the affected rows so the client stops having to verify with a
follow-up `check_line`:

```python
@app.delete("/api/repertoire/move")
def delete_repertoire_move(payload: DeleteMovePayload):
    """Deletes a move and all children branches from the repertoire."""
    if not is_valid_fen(payload.parent_fen):
        raise HTTPException(status_code=400, detail="Invalid FEN string")
    rm = RepertoireManager.get_instance()
    deleted = rm.delete_repertoire_branch(payload.repertoire, payload.parent_fen, payload.move_uci)
    return {"status": "ok" if deleted else "not_found",
            "deleted_count": int(deleted or 0),
            "message": "Branch deleted successfully." if deleted else "No such move in this repertoire."}
```

and `RepertoireManager.delete_repertoire_branch()` (`backend/repertoire.py:632`) must return the
number of deleted `repertoire_moves` rows (it currently returns `None`; the `study_moves` cleanup
happens inside). The client already prefers `deleted_count` and falls back to the
`check_line` probe, so this is backwards compatible. (`api.py:2046` already returns a
`deleted_count` for `clear_repertoire_database` — same convention, worth mirroring.)

### Cross-file consolidation — DUP-01 (needs FIXER-F, who owns `repertoire_builder.js` / `index.html`)

1. Move the **builder's** (canonical) `SessionTreeNode` + `SessionLineTree` out of the top of
   `repertoire_builder.js` (the `class SessionTreeNode` at :4 through the end of the two guarded
   class blocks) into a new file (e.g. `frontend/session_line_tree.js`) that both
   consumers load **before** `repertoire_builder.js` and `scout.js`
   (`index.html:978-980` script block — match by content, FIXER-F keeps editing it),
   exporting `window.SessionTreeNode` /
   `window.SessionLineTree`.
2. Merge the scout-only additions into it: `getAllLines()` must keep returning
   `lineId`, `studyName` and `node.lineName`, and `addMove` must keep the builder's richer
   metadata sync (`comment` / `is_important` / `arrows`) — i.e. the union of both copies.
3. Delete both local declarations. The FIXER-G side is already aligned with the canonical
   behaviour: canonical 4-field FENs, `_buildPgn` with the `1...` prefix (CODE-03),
   `deleteLine` that repairs `currentNode` (CODE-02), `isAttached()` and `getAllLines().lineId`
   (CODE-06) — so the builder's copy needs the same three changes if it is *not* extracted
   immediately (`repertoire_builder.js` currently has its own buggy `_buildPgn`/`deleteLine`).
4. The two API aliases (`/api/scout/check_line` + `/api/scout/check_tree` vs
   `/api/repertoire/check_line` + `/api/repertoire/check_tree`) can then be collapsed to one pair;
   the Scout now keys its overlap map by node id, so sharing one `treeOverlapMap` between the two
   panels becomes possible (today each panel keeps its own copy and a Scout import does not
   refresh the Builder's badges).

---

## 5. Also noticed (not fixed)

1. **The full-suite failures above are not mine** — backend modules owned by other fixers, with a
   run-to-run varying failure set (evidence in §2.3). The suite also takes >15 s and then hangs
   for minutes after printing the result (a lingering thread/engine pool), so
   `timeout 240 … > file` is needed to get a result.
2. `tests/test_scout_frontend_contracts.py` filename collision with the db-migration fixer
   (see the header warning) — the safest long-term name for my module would have been
   `test_scout_frontend_xss_contracts.py`.
3. `#trainFeedback` is still `display`-toggled (`repertoire_trainer.js:507/714/783/…`) on every
   answer. It is the *primary* feedback channel, so `visibility` + a reserved `min-height` would
   be the §8-compliant treatment; it is not in the CODE-4 list and touching it changes the visible
   feedback rhythm, so I left it.
4. `backend/repertoire.py::check_line_overlap` re-reads the whole `repertoire_moves` table on
   every `/check_line` + `/check_tree` call. The client now debounces, aborts and de-duplicates
   (CODE-21), but a server-side cache (or a combined endpoint for both repertoires) is still
   warranted.
5. `#scoutQuickAddNotice`, `#scoutEvalBarWrapper`, `#trainMoveHistoryBreadcrumb`, `#trainPrompt`,
   `#repBottomSidebarCard`, `#repMoveToolsPanel`, `#repQuickAddNotice`, `#lblMyStatsHeader`,
   `#btnScoutSubmit`, `#syncDetailLabelSynced` are still ids in `index.html` that no JS writes —
   the reverse direction of CODE-15 (belong to the HTML owner).
6. `window.promptStudySelection` still falls back to a blocking `prompt()` when the dialog is
   absent; with the CSP work (FIXER-E) a native prompt may be blocked — the fallback is now
   reachable only in a broken DOM, so it is harmless, but worth knowing.
7. The `Scout`-side `treeOverlapMap` and the `Builder`-side one are still independent (item 4 of
   hand-off (b)) — a Scout import does not refresh the Builder's badges.
