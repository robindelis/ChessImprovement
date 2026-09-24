# FIXER-J-01 — XSS retrofit of the Repertoire Builder frontend (SEC-J) + TRAINER-14 verdict

**Files owned and edited (exclusively mine):**
`frontend/repertoire_builder.js` (+350/−236), `frontend/board.js` (+23/−3)

**Files added:**
`tests/test_builder_xss_contracts.py` (12 tests),
`audit/scripts/FIXER-J_xss_scenario.js` (39 runtime checks),
`audit/scripts/FIXER-J_trainer14_scenario.js` (67 runtime checks)

**Files touched outside my scope, deliberately and minimally:**
`audit/scripts/FIXER-F_logic_probe.js` — **+2 lines** to the extraction list and **+11 lines** of
new checks. FIXER-F's probe extracts `board.js` methods by name and evaluates them standalone;
`setArrows` now calls `_sanitizeArrowColor`, so the probe threw `TypeError: this._sanitizeArrowColor
is not a function` and one FIXER-F test failed. The fix is to extract the new helper too. **No
existing check was weakened, removed or reworded.**

**Not touched:** `frontend/scout.js`, `repertoire_trainer.js`, `app.js`, `audio.js`,
`backend/**`, `tests/frontend_harness.js`, `tests/test_builder_frontend_contracts.py`,
`chess_improvement.db`, port 38281, the desktop app.

**`frontend/index.html` and `frontend/style.css`: NOT edited by me.** They are being edited
*right now* by FIXER-G (trainer/scout §8.4/§8.5 fixes landed at 19:45 and 19:48 while I worked).
I verified this by diffing against my own 19:25 pre-edit snapshot — every hunk in those two files
belongs to FIXER-G's trainer/scout work, none to me. I did not need a change in either.

---

## 1. The `innerHTML` / interpolation audit

Every `innerHTML` / `outerHTML` / `insertAdjacentHTML` assignment and every `${…}` hole that sits
inside a markup-bearing template literal, with the origin of each interpolated value.

### `frontend/repertoire_builder.js`

| # | Location (post-fix) | Value(s) interpolated | Origin | Trust | Action |
|---|---|---|---|---|---|
| 1 | `updateSortHeaderUI` `:546-562` | `h.label` (embeds `treeData.game_gaps.total_games_analyzed` / `currentCandidateData.user_games_total`) | backend JSON | **untrusted** | **FIXED** — was `el.innerHTML = \`${h.label} <span…>\``; now a `text` node + a real `<span class="sort-arrow">` built with `createElement`/`createTextNode` |
| 2 | `loadRecentGames` empty state `:1134-1143` | `this.gamesColorFilter` | fixed button set (`all`/`white`/`black`) | trusted-constrained | **escaped anyway** (one call, zero behaviour change) |
| 3 | **`_gameCardHtml` `:1200-1242`** | `g.result` → `class=` | `/api/games/recent` → Lichess API + `games` table | **untrusted** | **FIXED** — `_outcomeClass()` allowlist (`win\|loss\|draw`, else `draw`) |
| 4 | " | `g.result` (text) | idem | **untrusted** | **FIXED** — `_escapeHtml(String(...).toUpperCase())`; also `null`-safe (the old `g.result.toUpperCase()` threw on a null `result`) |
| 5 | " | `g.score_text` | backend-computed | **untrusted** | **FIXED** — `_escapeHtml` |
| 6 | " | `g.opponent_name` | **Lichess username** | **remote** | **FIXED** — `_escapeHtml` ← *this is SYNC-13* |
| 7 | " | `g.opponent_rating` | Lichess | **remote** | **FIXED** — `_escapeHtml(String(...))` |
| 8 | " | `g.speed`, `g.played_at_formatted` | Lichess / formatted date | **remote** | **FIXED** — `_escapeHtml` |
| 9 | " | `g.eco`, `g.opening_name` | **DB `opening_name` / `eco`** | **untrusted** | **FIXED** — `_escapeHtml` ← *SYNC-13* |
| 10 | " | `g.user_color` → `title=` | DB | **untrusted** | **FIXED** — normalised to `white`/`black`, then `_escapeAttr` |
| 11 | " | `g.game_url` → `href=` | backend-built `https://lichess.org/<id>` | **untrusted URL** | **FIXED** — `_safeHttpUrl()` scheme allowlist (`^https?://`, else `#`) |
| 12 | `cancelSync` `:678-701`, `resyncGames` `:691-719,855` | `'🔄 Refresh All Games'`, `'⏳ Scanning Games…'`, and the `origHtml` **round-trip** | literals this module itself wrote, or `index.html` | **trusted-static** | **no change** — the value read back at `:701` is never attacker-influenced |
| 13 | `loadStudies` `:894` | `'<option value="">(All Studies)</option>'` | literal | trusted-static | no change (the study names are set with `opt.textContent` at `:900`) |
| 14 | `loadRepertoire` `:973`, `renderBlunderBanner` `:2114` | the banner placeholder literal | literal | trusted-static | no change |
| 15 | **`_gameReviewBannerHtml` `:1547-1583`** | `game.result` → `class=` | `/api/games/recent` | **untrusted** | **FIXED** — `_outcomeClass()` |
| 16 | " | `game.result`, `game.score_text` | idem | **untrusted** | **FIXED** — `_escapeHtml` |
| 17 | " | **`game.opponent_name`** | Lichess | **remote** | **FIXED** — `_escapeHtml` ← *SYNC-13 :1499* |
| 18 | " | `game.opponent_rating` | Lichess | **remote** | **FIXED** — `_escapeHtml` |
| 19 | " | `game.eco`, `game.opening_name` | DB | **untrusted** | **FIXED** — `_escapeHtml` ← *SYNC-13 :1502* |
| 20 | " | `theoryPlies`, `theoryMoveNum` | `game.opening_ply` (backend int) | untrusted-as-string | **FIXED** — `_num()` |
| 21 | " | `targetColorName` | derived from `user_color` | constrained | **FIXED** — `_escapeHtml`/`_escapeAttr` defensively |
| 22 | " | `game.game_url` → `href=` | backend | **untrusted URL** | **FIXED** — `_safeHttpUrl()` |
| 23 | `updatePosition` turn indicator `:1655-1658` | two literals | literal | trusted-static | no change |
| 24 | `renderMoveToolsPanel` `:1809` | `'⭐ Priority Move'` / `'☆ Mark Important'` | literals | trusted-static | no change |
| 25 | `renderBreadcrumbs` `:2146` | `container.innerHTML = ''` (a clear) | — | — | no change; the chips are `createElement` + `textContent` already |
| 26 | `renderBlunderBanner` `:2131-2244` | the whole body | `treeData.game_gaps` | **untrusted** | **already safe** — FIXER-F built it with DOM APIs; verified, and the scenario re-checks it |
| 27 | `renderGapLists` / `_blunderGapCardHtml` `:2360-2437` | `g.opened`… `root.move_number`, `g.popularity_pct`, `g.games`, `g.opening_games` | backend | **untrusted** | **FIXED** — `Number(x\|\|0)` → `_num(x, 0)` (a JSON *string* reaching `.toFixed()`/`.toLocaleString()` returns the payload verbatim), and `root.move_number` → `_num()` |
| 28 | `onBoardMove` solved text `:2690` | `moveRes.san` | chess.js | low | already `_escapeHtml` (FIXER-F) — verified |
| 29 | `loadCandidateMoves` `:2705,2739,2751` | three literal status rows | literal | trusted-static | no change |
| 30 | **`_candidateRowHtml` `:2767-2879`** | **`m.san`** | Lichess Masters explorer JSON cached verbatim in `explorer_cache.data_json` | **remote** | **FIXED** — `_escapeHtml` |
| 31 | " | `m.popularity_pct` → `style="width:…%"` **and** as text | idem | **remote** | **FIXED** — `_num()` (was `Math.min(100, m.popularity_pct)`, which yields `width:NaN%` for a string but a *verbatim* `width:<payload>%` for a hostile one) |
| 32 | " | `m.games` → `.toLocaleString()` | idem | **remote** | **FIXED** — `_num()`; **this was a live primitive**: `("<img src=x …>").toLocaleString()` returns the payload unchanged |
| 33 | " | `m.white_win_pct` / `draw_pct` / `black_win_pct` → `title=` + three `style="width:…%"` + text | idem | **remote** | **FIXED** — `_num()` |
| 34 | " | `m.eval` | Stockfish cp (int) | low | **hardened** — `_num` + `Number.isFinite`; `null`/`undefined` still render the "no evaluation" badge exactly as before (an explicit `hasEval` guard, because `Number(null) === 0` would otherwise have invented a `+0.00` badge) |
| 35 | " | `my_stats.{games,wins,draws,losses,win_rate,threat_score}` → `title=` + text | backend | **untrusted** | **FIXED** — `_num()`; `win_rate` keeps its `null`/`undefined` → `-` behaviour via an explicit guard |
| 36 | " | `badgeClass` (`neutral`/`threat`/`win`) | chosen by comparing a coerced number | trusted-literal | no change (never data-derived) |
| 37 | " | `actionHtml` (5 variants) | literals | trusted-static | no change |
| 38 | `checkActiveLineOverlap` `:3290-3308` | placeholder literal; `getBadge()` | `_overlapBadge*` helpers | **untrusted** (`data.message`) | already `_escapeHtml`/`_escapeAttr` (FIXER-F) — verified, and the scenario re-checks it |
| 39 | `checkTreeOverlap` / `_applyTreeBadges` `:3460` | `_overlapBadgesHtml()` | idem | **untrusted** | already escaped once inside the helper — verified (see §4) |
| 40 | `renderTreeLines` empty state `:3571-3580` | literal | literal | trusted-static | no change |
| 41 | **`_treeLineCardHtml` `:3472-3541`** | **`line.name`** (= `lastMove.opening_name`) | game row **or** repertoire DB | **untrusted** | **FIXED** — `_escapeHtml` |
| 42 | " | **`line.eco`** | DB | **untrusted** | **FIXED** — `_escapeHtml` |
| 43 | " | **`line.pgn`** (assembled from those moves' SAN/UCI) | idem | **untrusted** | **FIXED** — `_escapeHtml` for the text, `_escapeAttr` for the `title` |
| 44 | " | `line.plyCount` | `path.length` (a JS number) | trusted | **hardened** — `_num()` |
| 45 | " | `idx`, `idx + 1`, `data-idx="${idx}"` | array index | trusted | no change |
| 46 | " | `badgesHtml` + the two `_importButtonState()` labels | backend `data.message` | **untrusted** | **already escaped exactly once inside the helpers** — deliberately *not* re-escaped here (see §4) |
| 47 | `loadTreeLine` / `removeTreeLineFromRepertoire` / `clearSessionTree` `:3620,3780,3829` | `line.name`, `lastMove.san`, `count` | — | — | **safe by sink**: they go to `showQuickNotice()` → `window.showToast()` → `text.textContent` (`app.js:520`), or to `confirm()` |
| 48 | every `notify()` / `showQuickNotice()` call with a backend string (`data.message`, `studyName`, `err.message`, …) | — | backend | **untrusted** | **safe by sink** — `app.js`'s `showToast` uses `textContent`. This is by far the largest untrusted-string surface in the file and it was never a hole |
| 49 | every `el.title = …` / `chip.title = …` / `el.textContent = …` assignment | — | — | — | **safe by sink** — DOM property, never parsed as markup |

### `frontend/board.js`

| # | Location | Value | Origin | Trust | Action |
|---|---|---|---|---|---|
| 50 | `_initDOM:53`, `render:223`, `_renderArrows:671` | `innerHTML = ''` (three element clears) | — | — | **no change** — a clear, not a write |
| 51 | `_renderArrows:711,723` | `setAttribute('fill'/'stroke', arr.color)` | `repertoire_moves.arrows` JSON | **untrusted** | **FIXED** — `_sanitizeArrowColor()` allowlist (`#rgb/#rgba/#rrggbb/#rrggbbaa` / `rgb()` / `rgba()`); a hostile value is replaced with `#22c55e` *before* it reaches the DOM **and** before `onArrowsChange` re-serialises it back to the DB |
| 52 | `_renderArrows` | `createElementNS('…svg','line'|'path'|'marker')` + `setAttribute` for every geometry value | computed from a validated square | trusted | no change — `setAttribute` cannot be used to break out of the attribute it is given |
| 53 | the whole file | — | — | — | **no HTML string interpolation anywhere.** `board.js` builds every node with `createElement`/`createElementNS`; the only string→markup path in the file is the three `innerHTML = ''` clears |

### Considered and deliberately left alone

* `btnResync`'s `origHtml = btnResync.innerHTML` round-trip (row 12). Converting it to
  `textContent` would change the whitespace `index.html` puts inside the button for a **zero**
  security gain — the value is only ever what this module wrote two lines earlier. Surgical
  beats tidy here.
* `<button>` is not in the scenario's "executable node" list: the renderers legitimately emit
  action buttons, and their wiring is asserted separately (class names + `querySelector`
  selectors + a real dispatched click on the candidate `<tr>`).
* **CODE3-18 residual** (`resyncGames` / `syncRecentGames` / `startEvalStream` share ~85 lines of
  SSE scaffolding, and `importToRepertoire` / `importAllLinesToRepertoire` are a near-clone):
  **still only a duplication concern, not a correctness bug.** I re-read all three SSE loops and
  the import clone; they differ where FIXER-F made them differ (two independent abort handles
  with identity-checked `finally`; `importToRepertoire` takes an optional `specificLineMoves`
  and the per-card callers use it). No line of one copy can produce a different result from its
  twin. Per the brief I did **not** attempt the refactor.

---

## 2. Changes, with `file:line`

### `frontend/repertoire_builder.js`

**(a) Three new sanitising helpers — `:2216-2255`** (next to FIXER-F's `_escapeHtml`/`_escapeAttr`)

```diff
+    /**
+     * Coerces a value that is about to be interpolated into a `style="width:N%"` or a numeric
+     * column. Explorer/DB payloads are JSON, so `popularity_pct` / `games` can legitimately arrive
+     * as strings; `"<img src=x>".toLocaleString()` returns the payload verbatim, so an un-coerced
+     * value is an injection primitive. `Number()` + `|| 0` makes every one of them inert.
+     */
+    _num(v, fallback = 0) {
+        const n = Number(v);
+        return Number.isFinite(n) ? n : fallback;
+    }
+
+    /** Allowlist for a value that lands in a `class="..."` attribute. … */
+    _outcomeClass(v) {
+        return (v === 'win' || v === 'loss' || v === 'draw') ? v : 'draw';
+    }
+
+    /** Allowlist for an `href` built from backend data. `javascript:` … collapses to '#'. */
+    _safeHttpUrl(u) {
+        const s = String(u == null ? '' : u).trim();
+        if (!s) return '#';
+        if (/^https?:\/\//i.test(s)) return s;
+        return '#';
+    }
```

**(b) `updateSortHeaderUI` — `:546-562`** — a count from a JSON payload is a text node

```diff
             if (this.candidateSort.col === h.col) {
                 el.classList.add('active-sort');
                 const arrow = this.candidateSort.dir === 'desc' ? '▾' : '▴';
-                el.innerHTML = `${h.label} <span class="sort-arrow">${arrow}</span>`;
+                // SEC-J: `h.label` embeds `total_games_analyzed` / `user_games_total` from a JSON
+                // payload, so it is a text node, not markup (the previous innerHTML interpolated it).
+                // `replaceChildren` (not `appendChild`) so a re-sort cannot accumulate labels.
+                const arrowEl = document.createElement('span');
+                arrowEl.className = 'sort-arrow';
+                arrowEl.textContent = arrow;
+                el.replaceChildren(document.createTextNode(`${h.label} `), arrowEl);
             } else {
                 el.classList.remove('active-sort');
-                el.innerHTML = h.label;
+                el.textContent = h.label;
             }
```

**(c) `loadRecentGames` — `:1145-1148`** — the game card is now a named builder

```diff
             games.forEach(g => {
                 const item = document.createElement('div');
                 const isCurrentReview = (this.activeReviewGame && this.activeReviewGame.id === g.id);
                 item.className = 'game-card' + (isCurrentReview ? ' active-review' : '');
-                … 40 lines of template literal …
+                item.innerHTML = this._gameCardHtml(g);
```

**(d) new `_gameCardHtml(g)` — `:1200-1242`** — the escaped replacement for those 40 lines

```diff
+    _gameCardHtml(g) {
+        const game = g || {};
+        const outcomePill = `<span class="game-outcome-pill ${this._outcomeClass(game.result)}">${this._escapeHtml(String(game.result == null ? '' : game.result).toUpperCase())} (${this._escapeHtml(game.score_text)})</span>`;
+        …
+                    ${colorDot} vs ${this._escapeHtml(game.opponent_name)}
+                …
+                    <span …>(${this._escapeHtml(String(game.opponent_rating || '?'))})</span>
+                    <span …>${this._escapeHtml(game.speed)} • ${this._escapeHtml(game.played_at_formatted)}</span>
+                …
+                    ${game.eco ? `<strong style="color:#38bdf8;">${this._escapeHtml(game.eco)}</strong> • ` : ''}${this._escapeHtml(game.opening_name || 'Standard Opening')}
+                …
+                <button class="btn-secondary btn-quick-add-opening" … title="Import recognized opening into your ${userColorAttr} repertoire">
+                <a href="${this._escapeAttr(this._safeHttpUrl(game.game_url))}" target="_blank" rel="noopener noreferrer" …>↗</a>
```

**(e) `renderGameReviewBanner` — `:1515`** — delegates to the new builder

```diff
-        banner.innerHTML = `… 35 lines …`;
+        banner.innerHTML = this._gameReviewBannerHtml(game, targetColorName, theoryPlies, theoryMoveNum);
```

**(f) new `_gameReviewBannerHtml(...)` — `:1547-1583`** — the escaped replacement
(`game.opponent_name` at old `:1499`, `game.eco`/`game.opening_name` at old `:1502`,
`game.game_url` at old `:1515`).

**(g) `renderGapLists` / `_blunderGapCardHtml` — `:2388,2389,2428,2430`** — coerce the
numeric fields FIXER-F left as `Number(x || 0)` and the raw `root.move_number`

```diff
-                            <span class="badge-critical">${Number(g.popularity_pct || 0).toFixed(1)}%</span>
+                            <span class="badge-critical">${this._num(g.popularity_pct, 0).toFixed(1)}%</span>
-                            … ${Number(g.games || 0).toLocaleString()} games …
+                            … ${this._num(g.games, 0).toLocaleString()} games …
-                    … · ${Number(g.opening_games || 0).toLocaleString()} master games
+                    … · ${this._num(g.opening_games, 0).toLocaleString()} master games
-… ↳ root cause: move ${root.move_number} (${this._escapeHtml(root.played_san || '?')}) …
+… ↳ root cause: move ${this._num(root.move_number, 0)} (${this._escapeHtml(root.played_san || '?')}) …
```

**(h) `renderCandidateTable` — `:2716-2718`** — the row is now a named builder

```diff
         moves.forEach(m => {
             const tr = document.createElement('tr');
             tr.className = 'explorer-row';
-            … 105 lines …
+            tr.innerHTML = this._candidateRowHtml(m, isUserTurn, m.uci === userPreparedUci, oppPreparedUcis.has(m.uci));
```

**(i) new `_candidateRowHtml(m, isUserTurn, isUserPrepared, isOppCovered)` — `:2767-2879`** —
the escaped replacement. The two subtleties:

```diff
+        // `Number(null) === 0`, which would have invented a "+0.00" badge for a move that has no
+        // evaluation. The explicit guard preserves the pre-fix "No engine evaluation" row.
+        const hasEval = (move.eval !== undefined && move.eval !== null);
+        const evalCp = hasEval ? this._num(move.eval, NaN) : NaN;
+        if (Number.isFinite(evalCp)) { … }
…
+        const msWinRate = (ms.win_rate === null || ms.win_rate === undefined)
+            ? NaN : this._num(ms.win_rate, NaN);      // same reason as above
…
+                <span class="explorer-move-san">${this._escapeHtml(move.san)}</span>
…
+                    <div class="pop-bar-fill" style="width:${Math.min(100, popPct)}%;"></div>
+                    <span …>${popPct}%</span>
+                <div …>${this._num(move.games, 0).toLocaleString()} games</div>
```

**(j) `renderTreeLines` — `:3572-3574`** — the card is now a named builder

```diff
-        list.innerHTML = lines.map((line, idx) => { … 75 lines … }).join('');
+        list.innerHTML = lines
+            .map((line, idx) => this._treeLineCardHtml(line, idx, activeUcis))
+            .join('');
```

**(k) new `_treeLineCardHtml(line, idx, activeUcis)` — `:3472-3541`**

```diff
+                                ${this._escapeHtml(line.name || 'Explored Line')}
+                            ${line.eco ? `…${this._escapeHtml(line.eco)}</span>` : ''}
+                        <span class="scout-var-plies">${this._num(line.plyCount, 0)} plies</span>
+                <div class="scout-var-pgn" title="${this._escapeAttr(line.pgn)}">${this._escapeHtml(line.pgn)}</div>
```

### `frontend/board.js`

**(l) new `_sanitizeArrowColor` — `:185-199`, applied at `:206` and `:225`**

```diff
+    _sanitizeArrowColor(color, fallback = '#22c55e') {
+        if (typeof color !== 'string') return fallback;
+        const c = color.trim();
+        if (/^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(c)) return c;
+        if (/^rgba?\(\s*[\d.\s,%+-]+\)$/i.test(c)) return c;
+        return fallback;
+    }
+
     addArrow(from, to, color = '#22c55e') {
         if (!this._isValidArrow({ from, to })) { … return false; }
-        this.arrows.push({ from, to, color });
+        this.arrows.push({ from, to, color: this._sanitizeArrowColor(color) });
…
     setArrows(arrows) {
         this.arrows = Array.isArray(arrows)
-            ? arrows.filter(a => this._isValidArrow(a)).map(a => ({ from: a.from, to: a.to, color: a.color || '#22c55e' }))
+            ? arrows.filter(a => this._isValidArrow(a)).map(a => ({
+                from: a.from, to: a.to, color: this._sanitizeArrowColor(a.color)
+            }))
             : [];
         this._renderArrows();
         return this.arrows.length;
     }
```

### `audit/scripts/FIXER-F_logic_probe.js` (FIXER-F's file, +2/+11, nothing weakened)

```diff
-const pieces = ['_isValidArrow', '_isValidSquare', 'addArrow', …
+const pieces = ['_isValidArrow', '_isValidSquare', '_sanitizeArrowColor', 'addArrow', …
…
+    // SEC-J: the persisted `color` is written into SVG presentation attributes and re-serialised
+    // back to the DB, so only real paint values survive.
+    check('setArrows keeps every legitimate paint value verbatim', [ … ].join(',') === '#22c55e,#ABC,#22c55eff,rgb(1, 2, 3),rgba(1,2,3,0.5)');
+    boardObj.setArrows([{ from: 'e2', to: 'e4', color: 'url(javascript:alert(1))' }]);
+    check('a non-paint arrow colour is replaced by the default', …);
+    boardObj.setArrows([{ from: 'e2', to: 'e4', color: 'red" onload="alert(1)' }]);
+    check('a colour carrying an attribute break-out is replaced by the default', …);
```

---

## 3. Double-escaping audit (the thing that is easiest to get wrong)

No value in this file arrives at a renderer **already** escaped, so escaping exactly once is
achievable — but three values *are* pre-escaped **by the time they reach the renderers**, because
FIXER-F's helpers escape internally:

| Value | Escaped in | At the call site |
|---|---|---|
| `_overlapBadgesHtml(w, b)` → the badge `<span>`s and their `title` | `_overlapBadgesHtml` (`:3409-3418`) | `renderTreeLines`/`_treeLineCardHtml` interpolates it **raw** (`${badgesHtml}`), and `_applyTreeBadges` assigns it to `innerHTML` **raw** (`:3446`). **Correct — a second escape here would render `&amp;lt;img` to the user.** |
| `_importButtonState(w,'White')` → `.text` / `.title` (which embed the backend `data.message`) | the two call sites in `_treeLineCardHtml` (`:3538,3545`) | already exactly once |
| `_overlapBadgeTitle` / `_overlapBadgeLabel` | `_overlapBadgesHtml` and `checkActiveLineOverlap`'s local `getBadge` (`:3218`) | — |

So `_treeLineCardHtml` escapes `line.name` / `line.eco` / `line.pgn` (raw data) and does **not**
escape `badgesHtml` / the button labels (pre-escaped). `test_no_double_escaping_of_the_pre_escaped_fragments`
asserts exactly this, and the scenario proves the *observable* consequence
(`SEC-J/overlap badges: the backend message is escaped exactly once (no double escaping)`:
`&lt;img` present, `&amp;lt;img` absent).

---

## 4. FIXER-F hand-off items I verified in my own files

| Item | Verdict |
|---|---|
| **Adopt `setArrows()`'s returned count** | **Already done** by FIXER-F at `repertoire_builder.js:1769` (`const kept = this.board.setArrows(parsed);` feeding `#repArrowsCount`). It is the **only** `setArrows()` call in my two files. `addArrow`/`clearArrows` return nothing and no label depends on them. |
| **`loadFEN(fen,{keepArrows:true})` where arrows should survive** | **Correctly not needed in my files.** All three of my `loadFEN` call sites (`:1642` in `updatePosition`, `:2890` in `playMoveFromUci`, `:3092` in `addressGap`) are immediately followed by `renderMoveToolsPanel(curr)`, which is the single authority for arrows: it either calls `setArrows(parsed)` or `clearArrows()`. Passing `keepArrows:true` would leave a stale arrow set on screen for one frame. The flag is still live for `scout.js`/`repertoire_trainer.js` (FIXER-G's hand-off 3). |
| **Route the builder's board flip through the `applyEvalBarOrientation` helper** | **Already done, all three sites.** `btnRepFlip` (`:319-328` → `applyEvalBarOrientation(newOr)` then replays `this.lastEval` through `renderEvalBar`), `loadRepertoire` (`:978-979`), `reviewGame` (`:1447-1448`). There is no fourth orientation change in the file. FIXER-F's CODE3-21 is complete on the builder side; hand-off item 6 remains open for `scout.js`. |
| **CODE3-18 residual** | **Still duplication only, not a correctness bug** — see §1, last bullet. Not attempted, as instructed. |

---

## 5. TRAINER-14 verdict (attacker ATK-2) — **FIXED. The trainer path is now safe, and there is no remaining route to the permanently-unanswerable state.**

### The exact code path that prevents it

`frontend/repertoire_trainer.js` (FIXER-G's file, read-only to me):

```
:591-598   this.savedPlanArrows = JSON.parse(data.arrows)      ← already try/caught (:594-597)
:601-603   if (len) this.board.setArrows(this.savedPlanArrows)  ← NOT in a try/catch of its own
:605       this.isWaitingForUser = true;                        ← the line ATK-2 never reached
:607-609   } catch (e) { console.error('Error starting repertoire drill:', e); }
```

Because `:602` is inside the `try` that ends at `:609`, any `TypeError` out of `setArrows()` was
swallowed there and left `currentDrill` set (`:560`) with the board loaded (`:568`) but
`isWaitingForUser === false`. `onUserAttempt` (`:731  if (!this.isWaitingForUser || !this.currentFen) return;`)
and `showHint` (`:663  if (!this.isWaitingForUser || …)`) both gate on that flag, so the drill was
permanently unanswerable and the only escape was "New Drill".

**The prevention, step by step, with the current `board.js`:**

1. `setArrows(arrows)` → `arrows.filter(a => this._isValidArrow(a))` (`board.js:221`).
   `_isValidArrow` (`:171-174`) requires a non-null object with two **exactly** two-character
   `a1..h8` strings (`_isValidSquare`, `:176-183`). `[null]`, `[{from:"e2",to:"j9"}]`,
   `[{from:"i4",to:"e7"}]`, `[{to:"e5"}]`, `[["e2","e4"]]`, `[{from:1,to:4}]` → all rejected.
   A non-array (a bare object, `null`) → `[]` (`:219`).
2. `this.arrows` therefore only ever holds validated entries. `setArrows`' return value is the
   post-filter length (`:228`), which is what `renderMoveToolsPanel` shows.
3. `_renderArrows()` (`:690`) opens with `if (!this.arrows.length) return;` (`:692`), so the
   `[null]` payload draws **nothing** and touches no DOM.
4. The only two things `_renderArrows` does with an entry are `setAttribute('fill'/'stroke', color)`
   — which coerces any value to a string and **cannot** throw — and
   `_getSquareCenterPercent(arr.from|arr.to)`, whose own guard (`:714-717`, FIXER-F) returns the
   board centre for anything invalid. With the `setArrows` filter in front of it, it is **total**.
5. Therefore `setArrows()` cannot throw, `startNewDrill` reaches `:605`, and the drill is
   answerable. FIXER-J closes the last remaining soft spot: `color`, which was the one field that
   still passed through unvalidated into two SVG presentation attributes and back into the DB
   (`_sanitizeArrowColor`).

The other three writers of `this.arrows` are equally safe: `addArrow` validates
(`board.js:201-209`), the right-click handler only ever produces `dataset.square` values
(constructed as `a1..h8` at `:78`), and `loadFEN`/`destroy` assign `[]`.

### End-to-end proof (not an assumption)

`tests/test_builder_xss_contracts.py::TestTrainer14Regression` concatenates the **real**
`frontend/repertoire_trainer.js` in front of `audit/scripts/FIXER-J_trainer14_scenario.js` and runs
the pair through the existing `tests/frontend_harness.js`, so the real trainer drives the real
`startNewDrill()` against the real `InteractiveChessboard` with only the network stubbed. It runs
**14 hostile `arrows` payloads** (`[null]` = the ATK-2 payload, an array hole, out-of-bounds
squares, a missing `from`, a bare object, a nested array, non-JSON garbage, an empty string, a
500-entry array, a hostile colour, numbers instead of squares, `null`, plus a valid one) and for
each asserts (1) no throw, (2) **`isWaitingForUser === true`** — the actual regression —
(3) the drill + board are positioned, (4) every rendered arrow is a well-formed `a1..h8` pair,
(5) every rendered arrow colour is a real paint value. It then proves the answer path is open by
calling `onUserAttempt('e7','e5')` and asserting the `POST /api/repertoire/train/attempt` was
actually issued (1 request) rather than short-circuited at `:731`.

**Against the current sources: 67/67 green** (`allPass True`, 14 payloads × 4 assertions
+ the 3 setup/teardown checks + the 14 colour checks + the final answer-path check).
**Against the true pre-FIXER-F `board.js`** (FIXER-F's own preserved snapshot in
`/tmp/opencode/fixerf/`, which has no `_isValidArrow` at all) **it is red, and it reproduces
ATK-2 verbatim**:

```
TRAINER-14 [[null]  (ATK-2 payload)]: the drill becomes answerable (isWaitingForUser === true)
    -> isWaitingForUser=false currentDrill=set          ← the permanently-unanswerable state
TRAINER-14 [out-of-bounds squares]: startNewDrill() does not throw
    -> TypeError: Cannot read properties of null (reading 'color')
…
Ran 2 tests … FAILED (failures=2)
```

---

## 6. Test evidence

### 6.1 Isolated module — POST-fix (mine + FIXER-F's, nothing else)

```
$ /home/robin/miniconda3/bin/python3 -m unittest \
      tests.test_builder_xss_contracts tests.test_builder_frontend_contracts
..............................................
----------------------------------------------------------------------
Ran 46 tests in 1.489s

OK
```

12 of those 46 are mine; FIXER-F's 34 are untouched and still green.

### 6.2 Isolated module — PRE-fix (`/tmp/opencode/fixerj/prefix/frontend` = the tree as I found it, post-FIXER-F / pre-FIXER-J)

```
$ XSS_FRONTEND_DIR=/tmp/opencode/fixerj/prefix/frontend \
  /home/robin/miniconda3/bin/python3 -m unittest tests.test_builder_xss_contracts
FAIL: test_board_arrow_colour_is_allowlisted (…TestInterpolationAudit)
FAIL: test_class_attributes_are_allowlisted_not_interpolated (…TestInterpolationAudit)
FAIL: test_escape_helpers_exist_and_cover_the_five_mark_characters (…TestInterpolationAudit)
FAIL: test_href_attributes_go_through_the_scheme_allowlist (…TestInterpolationAudit)
FAIL: test_no_untrusted_interpolation_reaches_markup (…TestInterpolationAudit)
FAIL: test_numeric_fields_are_number_coerced_not_string_formatted (…TestInterpolationAudit)
FAIL: test_the_renderers_delegate_to_their_extracted_builders (…TestInterpolationAudit)
FAIL: test_hostile_values_produce_no_executable_node (…TestRuntimeXssScenario)
FAIL: test_hostile_arrows_never_block_the_drill (…TestTrainer14Regression)
----------------------------------------------------------------------
Ran 12 tests in 1.056s

FAILED (failures=9)
```

9 of 12 red. The 3 that are green in both states are honest regressions guards, not security
claims, and I am not pretending otherwise:
* `test_no_double_escaping_of_the_pre_escaped_fragments` — asserts a mistake that did not exist
  before my change and that only my change could have introduced.
* `test_the_live_app_still_renders` — a smoke test that `index.html` / `style.css` are present.
* `test_board_render_arrows_is_total_for_any_setarrows_input` is green pre-fix **for
  `/tmp/opencode/fixerj/prefix`** (FIXER-F had already fixed it) and red against
  `/tmp/opencode/fixerf` — see §5.

The mechanism is env-var driven, so the *same* test file is used for both runs:
`XSS_FRONTEND_DIR` → `FRONTEND_DIR` → the real `frontend/`. For the runtime test the module
builds a throwaway shadow project root (`tempfile.mkdtemp`, `atexit`-cleaned) containing a copy
of `tests/frontend_harness.js`, a copy of `frontend/vendor/chess.min.js` and the two JS files from
the selected directory — so **nothing in the repository is swapped or modified** to produce the
"before" numbers.

### 6.3 The runtime scenarios, before and after

Reusing FIXER-F's harness pattern (`tests/frontend_harness.js` + a scenario file):

```
$ node tests/frontend_harness.js audit/scripts/FIXER-J_xss_scenario.js
POST-FIX  →  allPass True   total 39   failures 0

$ (same scenario, same harness, pre-FIXER-J board.js + repertoire_builder.js in a shadow root)
PRE-FIX   →  allPass False  total 39   failures 21
   SEC-J/games card (hostile): produces no executable node
   SEC-J/games card: the payload is escaped exactly once
   SEC-J/games card: a javascript: game_url is neutralised
   SEC-J/games card: a hostile result cannot escape the class attribute
   SEC-J/game review banner (hostile): produces no executable node
   SEC-J/game review banner: the payload is escaped exactly once
   SEC-J/game review banner: a javascript: game_url is neutralised
   SEC-J/candidate row (hostile): produces no executable node
   SEC-J/candidate row: the payload is escaped exactly once
   SEC-J/tree line (hostile): produces no executable node
   SEC-J/tree line: the payload is escaped exactly once
   SEC-J/tree line: a quote in a SAN cannot open a new attribute
   SEC-J/sort header: the active label is still "My Stats (N Games) + arrow span"
   SEC-J/sort header: a hostile count never becomes markup
   SEC-J/sort header: an inactive column is a plain text label
   SEC-J/sort header: re-sorting does not accumulate labels
   SEC-J/board: a hostile arrow colour is replaced, every valid arrow survives
   SEC-J/board: no SVG paint attribute carries a quote or a url()
   SEC-J/blunder gap list: produces no executable node
   SEC-J/overlap badges: produces no executable node
   SEC-J/overlap badges: the backend message is escaped exactly once (no double escaping)
```

A representative pre-fix finding, verbatim from the run:

```
SEC-J/games card (hostile): produces no executable node
  -> ["tag:img","handler:onerror","handler:onmouseover","url:href"] in …
     <span class="game-outcome-pill draw">WIN&quot; ONMOUSEOVER=&quot;ALERT(9) …
```

### 6.4 How "no executable node" is asserted without a DOM

`FakeElement.innerHTML` in `tests/frontend_harness.js` stores the markup string verbatim and drops
the children — which is exactly the artefact a payload needs, so the scenario tokenises the string
itself: every `<tag …>` is checked against a deny-list of scripting/embedding elements
(`script img svg math iframe object embed applet form input style link meta base video audio
source template noscript frame frameset portal marquee details dialog select option body html xmp
plaintext listing`) and every attribute against `on*` handlers, `javascript:` URLs and CSS
`expression()`. `<button>`/`<a>` are excluded on purpose (legitimate click targets) and covered by
dedicated click-target assertions instead, including a **real dispatched click** on the candidate
`<tr>` (whose listener is bound on the element itself, so it survives the shim) that must still call
`playMoveFromUci('g8f6','Nf6')`.

Regression coverage for "the existing click targets and rendering still work": every benign render
is compared against the exact expected markup — the `btn-review-game` / `btn-quick-add-opening`
classes *and* the `querySelector` selectors the wiring uses, the three review-banner button ids, the
`★ Your Move` / `✓ In Tree` / `✕` / `+ Add Branch` / `+ Set as My Move` class pairs, the four
`btn-var-action` classes with their `data-idx`, the eval badge, the win-rate bar widths, the
`1,200 games` locale formatting and the threat-score `title`.

The only **byte-level** difference in benign output is `King's Pawn` → `King&#39;s Pawn` (and the
same for any apostrophe): an entity and a literal apostrophe parse to the same glyph, so nothing
moves on screen. The assertions accept either form and say so in a comment.

### 6.5 FIXER-F's own evidence, re-run

```
$ node audit/scripts/FIXER-F_logic_probe.js
… ALL CHECKS PASSED (frontend dir: frontend)            exit 0   (59 checks)

$ node tests/frontend_harness.js audit/scripts/FIXER-F_integration_scenario.js
FIXER-F integration allPass True total 39 failures 0
```

Both green — i.e. the 34 FIXER-F tests encode real regressions and none of them broke.

### 6.6 Full suite

The full-suite result moves as other fixers land, exactly as the brief warned. Four observations
during this session, all with my modules green in every one:

```
run A (19:20)  Ran 374 …  FAILED (failures=11, errors=1)
                 FAIL test_repertoire.TestRepertoire.test_evaluator_illegal_fen_safety
                 FAIL test_repertoire_write_safety…test_get_game_gaps_performs_no_writes…
                 FAIL/ERROR × 6 in test_layout_contracts   ← FIXER-G's in-flight index.html edits
run B (19:47)  Ran 375 …  FAILED (failures=2)   ×3 consecutive runs, identical set
                 FAIL test_repertoire.TestRepertoire.test_evaluator_illegal_fen_safety
                 FAIL test_repertoire_write_safety…test_get_game_gaps_performs_no_writes…
run C (20:05)  Ran 404 …  FAILED (failures=6, errors=15)   ← a NEW module appeared mid-session
                 ERROR × 15 + FAIL × 5 in test_analyzer_correctness  (brand-new file, 0 refs to frontend/)
                 FAIL test_api.TestAPI.test_sync_malformed_games
```

**No failure in any run is mine, and none of the failing modules reference `frontend/`** (checked
mechanically: `grep -c 'frontend/' tests/test_analyzer_correctness.py` → `0`,
`tests/test_api.py` → `0`). All are in `backend/**`/`tests/**` owned by other agents, and the
failing set demonstrably shifts between runs as their work lands. The count grew from 375 to 404
because `tests/test_analyzer_correctness.py` was added by another agent *while I was finishing* —
it did not exist in run B.

Isolating my own work:

```
$ /home/robin/miniconda3/bin/python3 -m unittest discover tests 2>&1 \
    | grep -cE 'test_builder_(xss|frontend)_contracts'
0        # no FAIL:/ERROR: line has ever named either module
```

`tests/test_builder_xss_contracts.py` never touches a database and does **not** import
`tests.test_setup`, so it cannot perturb the shared fixture for anyone else. It writes only to a
`tempfile.mkdtemp()` shadow root, and never to `chess_improvement.db`. Port 38281 was never
contacted; the desktop app was never started; no Stockfish analysis was run.

### 6.7 Final consolidated green run

```
node --check frontend/repertoire_builder.js && node --check frontend/board.js   → syntax OK
node audit/scripts/FIXER-F_logic_probe.js              → exit 0  (59 PASS / 0 FAIL)
node tests/frontend_harness.js … FIXER-F_integration…  → allPass True  total 39  failures 0
node tests/frontend_harness.js … FIXER-J_xss_scenario  → allPass True  total 39  failures 0
python3 -m unittest tests.test_builder_xss_contracts \
                    tests.test_builder_frontend_contracts
                                                       → Ran 46 tests … OK
```

---

## 7. Residual risks

1. **`explorer_cache.data_json` and the `games` table still hold the raw payload.** The frontend
   now renders them inertly, but a *new* consumer (another tab, an export, a future
   `innerHTML`) would have to repeat this reasoning. The durable fix is a shared
   `domSafeText()/domSafeAttr()` helper in one place — which is exactly what
   `test_no_untrusted_interpolation_reaches_markup` now enforces statically for
   `repertoire_builder.js`. **`scout.js` has not had this audit** (FIXER-G's file): it interpolates
   `opening_name`, `eco` and video-derived strings into `#scoutActiveLineOverlap` and the
   `scout-line-overlap-strip`. I have not verified it either way.
2. **`_outcomeClass` degrades an unknown `result` to the `draw` pill.** The backend already
   normalises `result` to `win|loss|draw` (`backend/api.py:83-94`), so this never triggers today;
   if that contract changes, the pill colour silently falls back rather than rendering an
   arbitrary class. The *text* still shows the raw value, escaped, so nothing is hidden.
3. **`_safeHttpUrl` returns `'#'` for a non-http(s) URL**, so a legitimate `lichess://` deep link
   would stop working. No such link exists in the payload today (`api.py:627` builds
   `https://lichess.org/<id>`).
4. **The DOM shim does not parse `innerHTML`**, so `renderTreeLines`'s per-card listeners
   (`list.querySelectorAll('.btn-load-rep-tree-line')` etc.) cannot be exercised at runtime here.
   I assert the class names and `data-idx` values those selectors match on instead. This is a
   harness limitation inherited from FIXER-F, not a gap in the fix.
5. **`_sanitizeArrowColor` accepts `rgb()`/`rgba()` with a permissive character class**
   (`[\d.\s,%+-]+`). It is a *paint-value* allowlist, not a CSS parser; a malformed `rgb(` can at
   worst make the browser ignore the `fill`/`stroke` attribute. It cannot inject markup or an
   event handler, and that is the property the audit needs.
6. **`_num(v, 0)` turns a missing/garbage number into `0`.** For `m.popularity_pct` and the
   win-rate widths that means an empty bar instead of a broken one — an improvement — but it does
   mean a corrupt payload renders as a plausible-looking `0.0%` rather than `NaN%`. The
   `eval`/`win_rate` cases deliberately do **not** do this (explicit `null`/`undefined` guards), so
   "no evaluation" and "no win rate" still render as such.
7. **CODE3-18's residual duplication** is untouched by design (§1). If someone later extracts the
   shared SSE loop, the *newly shared* code must keep the two identity-checked `finally` blocks
   apart — that is the one place where "deduplicating" would reintroduce CODE3-04.
8. **Hand-off for FIXER-G (files I do not own):** `scout.js` has had no interpolation audit and
   almost certainly has the same class of hole (item 1). `repertoire_trainer.js` sets
   `feedback.textContent` and `nameEl.textContent` throughout, so it is clean by sink, but its
   `board.setArrows(this.savedPlanArrows)` calls at `:602`, `:865`, `:101` now benefit from the
   colour allowlist and should switch to the **returned count** if they ever render a label.
