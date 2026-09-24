# FIXER-H-01 — `frontend/style.css` + `frontend/index.html` (§8 zero-CLS layout-stability wave)

- **Fixer:** FIXER-H
- **Scope (owned exclusively):** `frontend/style.css`, `frontend/index.html`
- **Implements:** `AGENTS.md` §8 sub-rules **8.1, 8.2, 8.3, 8.4, 8.5**, from the hand-off lists
  of `audit/patches/FIXER-G-01.md` §3 (a) and §4 (b) items 1–4
- **New test module:** `tests/test_layout_contracts.py` (29 tests)
- **Files NOT touched:** every `frontend/*.js`, `backend/**`, `electron/**`, and every
  pre-existing test in `tests/**` (FIXER-D owns those)

> ⚠️ **`index.html` was re-read immediately before the final edit** and again after the
> full suite run. Its mtime is still `19:45:29` (mine); FIXER-J's last touch was `18:59`.
> All eight HTML edits are single-line / single-element replacements, so a concurrent
> edit to any *other* region of the file cannot be clobbered by mine.

---

## 0. Method, stated plainly (read this before believing any claim below)

There is **no layout engine in a Python process**, so nothing in this patch is verified by
rendering anything. The verification is two kinds of evidence, and they are different in kind:

| kind | what it reads | what it does **not** prove |
|---|---|---|
| **DOM-attribute assertions** | `frontend/index.html` is **parsed** with `html.parser.HTMLParser` into a real element tree (`_DomBuilder` / `_Node` in the test module) and the assertions read `element.attrs["style"]`, `["id"]`, `["class"]`, `["data-filter"]`, `element.tag` | that a browser painted a box, or that the box has the width the declaration asks for |
| **static source scans** | `frontend/style.css` is run through a small selector→declaration parser (`_css_rules`, which merges comma-joined selector groups and concatenates repeated selectors so a lookup sees the whole cascade) and `frontend/*.js` is scanned for the ids it reveals/hides | the same |

**I did not run the app, the Electron shell, or a browser.** Nothing below is a screenshot
or a `getBoundingClientRect`. The reason static checking is nevertheless the right tool here
is that the entire bug class in this wave is *declarative*: an inline `display:none` that a
JS `style.visibility = 'visible'` assignment **cannot** beat, because `visibility` only
affects an already-boxed element (finding CODE3-01: "🗑️ Remove Line" was permanently
invisible for exactly that reason). That is fully determinable without a browser and is
**not** determinable by eyeballing a screenshot. A *rendering* regression that leaves every
declaration in this file intact would not be caught by these tests.

---

## 1. `frontend/style.css` — one line per change

### 1.1 `.btn-quick-rep` — §8.4 — fixed box for the quick-rep toolbar
`style.css:2610-2624` (rule at `2615`, was `2566-2573`). Mirrors `scout.js::_primeQuickRepToolbar()`, which
writes the same numbers inline.
```diff
 .btn-quick-rep {
     font-size: 0.75rem;
     padding: 0.22rem 0.5rem;
     background: #1e293b;
     border-color: #334155;
     transition: all 0.15s ease;
+    min-width: 150px;
+    justify-content: center;
+    white-space: nowrap;
 }
```

### 1.2 `#btnScoutMarkImportant` — §8.4 — 212px box across both of its labels
`style.css:2632-2640` (new block; rule at `2637`). Its label swaps between *"⭐ Mark Last Move Important"*
and *"⭐ Important Move"*; without a bound the quick-rep row reflows on every toggle.
```css
#btnScoutMarkImportant {
    min-width: 212px;
    justify-content: center;
}
```

### 1.3 `#btnScoutRemoveFromRep`, `#btnScoutTrainAfterImport` — §8.4 — **the exact hand-off snippet**
`style.css:2642-2653` (new block). Hidden by default, revealed by `:not([disabled])` —
never by `display`.
```css
#btnScoutRemoveFromRep,
#btnScoutTrainAfterImport {
    min-width: 150px;
    justify-content: center;
    visibility: hidden;
    opacity: 0;
    transition: opacity 0.18s ease;
}

#btnScoutRemoveFromRep:not([disabled]),
#btnScoutTrainAfterImport:not([disabled]) {
    visibility: visible;
    opacity: 1;
}
```

### 1.4 `#scoutCurrentOpeningTag` — §8.2 — clamp aligned with the JS
`style.css:2319-2332` (rule at `2324`). The rule existed but was pinned at `max-width: 210px` while
`scout.js::_clampInline(tagEl, '240px')` writes `240px` inline — so the first paint had a
210px box and the first JS frame snapped it to 240px (a 30px shift the first frame hides).
`min-width: 0` is added because it is what lets the ellipsis actually engage: without it a
flex item's automatic minimum size is its content width, so the tag overflowed the
fixed-height `.scout-board-info-strip` instead of clipping.
```diff
 #scoutCurrentOpeningTag {
     display: inline-block;
-    max-width: 210px;
+    min-width: 0;
+    max-width: 240px;
     white-space: nowrap;
     overflow: hidden;
     text-overflow: ellipsis;
     vertical-align: middle;
 }
```

### 1.5 `#scoutNoteMoveLabel` (160px), `#scoutTurnBadge` (110px) — §8.2 — new rules
`style.css:2333-2352` (new). Neither selector existed anywhere in the stylesheet, so both
elements — one whose text is rebuilt on every played move (`"⚪ Move 12 (Nf3) Note"`) and
one that flips between "White to move" / "Black to move" — were **completely unclamped**.
```css
#scoutNoteMoveLabel {
    display: inline-block;
    min-width: 0;
    max-width: 160px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    vertical-align: bottom;
}

#scoutTurnBadge {
    display: inline-block;
    min-width: 0;
    max-width: 110px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    vertical-align: bottom;
}
```

### 1.6 `.scout-live-indicator.live` / `.scout-live-indicator.idle` — new rules
`style.css:2277-2288` (new, next to the existing base rule at `2263-2271`).
`scout.js::_onYtStateChange()` puts `#scoutLiveIndicator` into `.live` / `.idle` from the
player state; **neither state class had a rule**, so the dot stayed green and pulsing
forever, including while nothing was playing. Base rule left byte-identical.

### 1.7 `#trainFocusBanner` — §8.5 + §8.4 — permanent flex strip, 34px
`style.css:2672-2687` (new).
```css
#trainFocusBanner {
    display: flex;
    min-height: 34px;
    box-sizing: border-box;
    visibility: hidden;
    opacity: 0;
    transition: opacity 0.18s ease;
}
```

### 1.8 `#trainImportantBadge` — §8.4 — 132px box so the wrapping header cannot reflow
`style.css:2688-2697` (new).

### 1.9 `#btnTrainToggleArrows` — §8.4 — 96px box
`style.css:2698-2705` (new). It sits between "Show Hint" and "Flip" in a `space-between`
row, so a width change pushes "Flip" sideways.

### 1.10 `#trainNoteBox` — §8.5 — constant 44px
`style.css:2704-2725` (new; the 13-line KNOWN-GAP comment is at `2704-2715`). See §6.1 for a *known* shortfall in the 44px figure, which is
FIXER-G's runtime constant and therefore not fixable from this file.

### 1.11 `#trainNoteContent` — §8.2 — clamped text
`style.css:2726-2733` (new). `repertoire_trainer.js::_updateNoteBox()` sets these four
properties inline; the stylesheet now matches, so the first paint already has them.

### 1.12 `#trainFocusBanner` / `#trainImportantBadge` / `#btnTrainToggleArrows` / `#trainNoteBox` — §8.4 — `visibility:hidden` + `opacity:0` defaults
Added to 1.7–1.10. Without this the stylesheet alone still described the *revealed* state,
so the contract lived only in the inline styles. The JS sets `visibility:'visible'` inline
and therefore still wins; this only makes the stylesheet self-consistent and makes the
first paint match the post-JS paint.

### 1.13 `.scout-card-overlap-badges` — §8.5 — 20px floor, never wraps
`style.css:3107-3117` (was `2990-2992`, `min-height: 22px` with no wrap control).
```diff
 .scout-card-overlap-badges {
-    min-height: 22px;
+    min-height: 20px;
+    flex-wrap: nowrap;
+    overflow: hidden;
 }
```

### 1.14 `.scout-badge-youtube` / `.scout-badge-guide` / `.badge-eco` — new rules
`style.css:3119-3158` (new; shared rule at `3123`, `.scout-badge-youtube` at `3136`, `.scout-badge-guide` at `3141`, `.badge-eco` at `3148`). **These three classes had no rule anywhere in the stylesheet**
while `scout.js::scoutCardMarkup()` emits all three and `index.html:692` already carries
`class="badge-eco"` on `#scoutStudioEco` — i.e. the badges rendered as bare unstyled text,
at a different height than the 20px badge row reserved by §8.5.
```css
.scout-badge-youtube,
.scout-badge-guide { display:inline-block; font-size:0.66rem; font-weight:700;
    letter-spacing:0.02em; padding:0.1rem 0.4rem; border-radius:999px;
    text-transform:uppercase; white-space:nowrap; vertical-align:middle; }
.scout-badge-youtube { background:rgba(239,68,68,0.18);  color:#fca5a5;
                       border:1px solid rgba(239,68,68,0.35); }
.scout-badge-guide   { background:rgba(56,189,248,0.15); color:#7dd3fc;
                       border:1px solid rgba(56,189,248,0.35); }
.badge-eco           { display:inline-block; font-size:0.68rem; font-weight:700;
                       color:var(--accent-primary); background:rgba(56,189,248,0.12);
                       border-radius:4px; padding:0.05rem 0.3rem;
                       white-space:nowrap; vertical-align:middle; }
```
(Full multi-line form in the file; collapsed here only to keep this table readable.)

### 1.15 `.explorer-table td` — §8.3 — **new finding, found while checking verify item 4**
`style.css:1262-1273`. The table already had `table-layout: fixed`, `width: 100%` and six
explicit `nth-child` column widths, so it passed §8.3's letter — but the cells declared
`overflow: hidden; text-overflow: ellipsis` **without `white-space: nowrap`**, and
`text-overflow` is inert without it. A long opening name in the *Master (2000+)* or *My
Stats* column therefore wrapped to a second line and changed the row height — precisely the
"columns must never twitch, jitter, or resize" clause. One line added:
```diff
 .explorer-table td {
     padding: 0.6rem 0.6rem;
     border-bottom: 1px solid var(--border-color);
     vertical-align: middle;
+    white-space: nowrap;
     overflow: hidden;
     text-overflow: ellipsis;
 }
```

### 1.16 `.scout-overlap-banner` + `.banner-present` / `.banner-partial` / `.banner-new` — **deleted**
`style.css:2859-2875` (28 lines removed, replaced by a 9-line comment). CODE-15 removed
`#scoutActiveLineOverlapBanner` from `index.html`; the class is now referenced by **zero**
elements and **zero** JS statements:
```
$ grep -rn "scout-overlap-banner" frontend/*.js frontend/index.html
$ (exit 1 — no hits)
```
I chose **deletion** over the hand-off's alternative `display:none` tombstone: a
`display:none` rule on a class nothing applies is a no-op that only misleads the next
reader, whereas a deleted rule plus the comment points at the three classes that *do* render
the overlap UI (`.scout-line-overlap-strip`, `.scout-overlap-badge`,
`.scout-card-overlap-badges`), all of which are §8.5-compliant. `test_dead_overlap_banner_css_is_gone`
asserts both halves of that (no reference, no rule), so if the class is ever re-introduced
the failure message tells the author to restore the CSS instead.

### 1.17 `.feedback-box.mistake` — **deliberately not added**
Hand-off (a) offered it as optional. FIXER-G resolved the original mismatch by changing
`repertoire_trainer.js:945` to `feedback-box blunder` (the class that *does* have a rule) and
has a passing test (`test_feedback_mistake_class_has_a_css_rule`) pinning that. Adding a
`.mistake` rule now would create the dead CSS DEAD-01 was cleaning up.

---

## 2. `frontend/index.html` — one line per change

Every one of the six is the **§8.4 / CODE3-01 class of bug**: an inline `display:none` that
the JS `visibility` toggle cannot override. The JS already overrode all of them at runtime,
so these edits remove the first-paint flash and make the stylesheet authoritative — no
behavioural change, only a first-frame one.

### 2.1 `#btnTrainToggleArrows` — §8.4
`index.html:543`
```diff
-<button id="btnTrainToggleArrows" class="btn-secondary" style="display:none;" title="Toggle plan arrows on board">🎯 Arrows</button>
+<button id="btnTrainToggleArrows" class="btn-secondary" style="visibility:hidden; min-width:96px;" title="Toggle plan arrows on board">🎯 Arrows</button>
```

### 2.2 `#trainFocusBanner` — §8.5 + §8.4
`index.html:553-554` comment + `index.html:555` element
```diff
-<div id="trainFocusBanner" style="display:none; margin-bottom:0.75rem; …">
+<!-- AGENTS.md 8.5: constant-height status strip, permanently in flow.
+     `visibility` (not `display`) so the sidebar below never reflows. -->
+<div id="trainFocusBanner" style="visibility:hidden; display:flex; min-height:34px; box-sizing:border-box; margin-bottom:0.75rem; …">
```

### 2.3 `#trainImportantBadge` — §8.4
`index.html:570-571` comment + `index.html:572` element — `display:none` → `visibility:hidden` +
`display:inline-flex; min-width:132px; justify-content:center`.

### 2.4 `#trainNoteBox` — §8.5 — and `#trainNoteContent` — §8.2
`index.html:584-585` comment, `index.html:588` `#trainNoteBox`, `index.html:590` `#trainNoteContent`
```diff
-<div id="trainNoteBox" style="display:none; background:rgba(30, 41, 59, 0.85); …">
+<div id="trainNoteBox" style="visibility:hidden; min-height:44px; max-height:44px; overflow:hidden; background:rgba(30, 41, 59, 0.85); …">
     …
-    <div id="trainNoteContent" style="line-height:1.45;"></div>
+    <div id="trainNoteContent" style="display:block; max-width:100%; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; line-height:1.45;"></div>
```

### 2.5 `#btnScoutRemoveFromRep` — §8.4
`index.html:782-787` (3-line comment at `782-784`, element at `786`)
```diff
-<button id="btnScoutRemoveFromRep" class="btn-secondary btn-quick-rep" style="display:none; color:#ef4444; border-color:rgba(239,68,68,0.35);" title="…">
+<!-- AGENTS.md 8.4: fixed 150px box + `visibility` (NOT `display`).
+     An inline `display:none` cannot be overridden by
+     `style.visibility = 'visible'` — that is exactly what kept
+     this button permanently invisible (CODE3-01 class of bug). -->
+<button id="btnScoutRemoveFromRep" class="btn-secondary btn-quick-rep" style="visibility:hidden; min-width:150px; color:#ef4444; border-color:rgba(239,68,68,0.35);" title="…">
```

### 2.6 `#btnScoutTrainAfterImport` — §8.4
`index.html:791-792` comment + `index.html:793` element — `display:none` → `visibility:hidden; min-width:150px`.

### 2.7 `#settingsAlert` — §8.1 — **deleted**
`index.html:892` (was). No JS reference remains (CODE-08 removed every write; FIXER-G's
`tests/test_scout_frontend_contracts.py:453` already asserts the JS never mentions it), and
`scout.js::_showQuickAddNotice()` is now a thin `window.showToast()` wrapper. An
`display:none` notice slot sitting in the settings form is exactly what §8.1 bans.

### 2.8 `#scoutQuickAddNotice` — §8.1 — **deleted**
`index.html:786` (was). `style="display:none !important;"` and **never written by any JS** —
it is now the only thing §8.1's "ephemeral notice in the document flow" would look like,
and leaving it invites the next author to write to it.

### 2.9 `★ Bookmarked` filter button — optional hand-off (b) item 4 — **added**
`index.html:665`, inserted after the `recent` button so it is the last chip in
`.scout-shelf-filters` and the row does not grow a second line at the shelf's current width.
```html
<button type="button" class="scout-filter-btn" data-filter="favorites" title="Only the guides and videos you bookmarked with the ★ button">★ Bookmarked</button>
```
This is the *only* missing piece of a fully-wired feature: `scout.js::_filterResults()` already
implements the `favorites` case, `toggleFavorite()` keeps `cachedResults` in sync and
re-renders `if (this.activeFilter === 'favorites')`, and FIXER-G has a passing test for the
filter behaviour. `type="button"` is explicit because the search shelf is inside a form
context in the DOM and a bare `<button>` defaults to `type=submit`.

---

## 3. Test module: `tests/test_layout_contracts.py` (29 tests, new file — FIXER-D untouched)

No existing test was modified, weakened or deleted. The module **never imports `backend.*`,
never opens a database, never makes a network call and never writes to
`chess_improvement.db`**, so it needs no `tests.test_setup` import and cannot perturb the
shared temp-DB fixture. `tests/test_isolation_guard.py` rule 3 only polices modules that
import a `backend*` module, so this module is compliant by construction — and
`tests/test_isolation_guard` itself is green (12/12, verified in §4.3).

`index.html` is parsed with `html.parser`, and the CSS is read through a declaration parser,
so the DOM-attribute assertions are attribute-level facts about the shipped markup, not
regex matches on a string.

| class | tests | §8 rule | what it pins |
|---|---|---|---|
| `TestConditionalControlsUseVisibility` | 6 | 8.4 | (a) the seven named ids are not `display:none`; (b) the six that boot hidden are `visibility:hidden` **and** reserve a box; (c) **STANDING GUARD, fully generic** — every id the JS reveals/hides via `style.visibility`/`style.opacity` is scanned out of `frontend/*.js` and none may boot `display:none`; (d) the stylesheet's `min-width` set matches the JS's; (e) `:not([disabled])` is the reveal; the base rule contains no `display` at all; (f) the focus banner stays a flex strip |
| `TestConstantHeightStatusStrips` | 4 | 8.5 | `#trainNoteBox` 44/44, `#trainFocusBanner` 34; both strips' `min-height` + `flex-wrap:nowrap` + `overflow:hidden`; the CODE3-03 blunder banner keeps its constant height and a placeholder |
| `TestDynamicTextIsClamped` | 4 | 8.2 | all four clamp-list selectors carry `nowrap`+`hidden`+`ellipsis`+a width bound; the bounds **agree with the values `_clampInline()` writes in the JS**; the dismissed 210px value is gone; `.eval-bar-label` is clamped |
| `TestDynamicTablesAreFixed` | 3 | 8.3 | every `<table>` in the parsed DOM has a class whose rule sets `table-layout: fixed` + `width: 100%`; all six `.explorer-table` columns are explicitly sized and cells clip; FIXER-F's two gap lists are divs, not tables |
| `TestNoticesAreToastsOnly` | 3 | 8.1 | the two dead in-flow notice nodes are gone **and** unreferenced in any `frontend/*.js`; the replacement `.app-toast-container` is `position: fixed` / `pointer-events: none`; **no remaining `display:none` container in index.html is named like a notice** (`notice|alert|message|toast|banner-msg`) — the generic version of the same rule |
| `TestEmittedClassesAreStyled` | 5 | 8.2/8.5 | the three badge classes are styled and visually distinct; `.badge-eco` is present *and* has a rule; `.scout-live-indicator.idle`/`.live` exist; `.scout-overlap-banner` is unreferenced *and* unstyled; the three survivor classes are still styled (so the deletion in §1.16 could not have removed the only styling) |
| `TestScoutFilterButtons` | 3 | — | every literal `_filterResults()` implements has a `data-filter` button; the `favorites` button is `type=button` with a `title`; all filter buttons share `.scout-filter-btn` so the `querySelectorAll` click handler binds them all |

### 3.1 Before / after

The pre-fix tree is `/tmp/opencode/fixerh_prefix/frontend`: the **live** `frontend/*.js`
(FIXER-G's and FIXER-F's output, byte-identical to the working tree) with `style.css` and
`index.html` restored from the 18:59 snapshot at `/tmp/opencode/fixeri_prefix_repo/frontend/`.
That isolates *my* change exactly — the JS is the same on both sides, so every red is
attributable to the CSS/HTML.

```
$ LAYOUT_FRONTEND_DIR=/tmp/opencode/fixerh_prefix/frontend \
      /home/robin/miniconda3/bin/python3 -m unittest tests.test_layout_contracts
Ran 29 tests in 0.602s
FAILED (failures=53)

$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_layout_contracts
Ran 29 tests in 0.585s
OK
```

The standing guard's own pre-fix output names all six offenders and quotes the shipped tag:

```
FAIL: test_no_visibility_toggled_element_is_bootstrapped_display_none
AssertionError: Lists differ: [...6 elements...] != []
: these elements are toggled with visibility/opacity by the JS but boot
with inline display:none, so they can never be revealed (CODE3-01):
  btnScoutRemoveFromRep   -> <button id="btnScoutRemoveFromRep" class="btn-secondary btn-quick-rep" style="display:none; color:#ef4444; …">
  btnScoutTrainAfterImport-> <button id="btnScoutTrainAfterImport" class="btn-primary btn-quick-rep" style="display:none; …">
  btnTrainToggleArrows    -> <button id="btnTrainToggleArrows" class="btn-secondary" style="display:none;" …>
  trainFocusBanner        -> <div id="trainFocusBanner" style="display:none; …">
  trainImportantBadge     -> <span id="trainImportantBadge" style="display:none; …">
  trainNoteBox            -> <div id="trainNoteBox" style="display:none; …">
```

Other representative pre-fix reds:

```
FAIL: test_every_clamped_element_has_the_full_treatment  (sel='#scoutNoteMoveLabel')
  AssertionError: '#scoutNoteMoveLabel must declare white-space:nowrap (§8.2); rule is: '
FAIL: test_every_clamped_element_has_the_full_treatment  (sel='#scoutTurnBadge')
  AssertionError: '#scoutTurnBadge must declare white-space:nowrap (§8.2); rule is: '
FAIL: test_every_clamped_element_has_the_full_treatment  (sel='#trainNoteContent')
  AssertionError: 'no CSS rule for #trainNoteContent — its dynamic text is unclamped (§8.2)'
FAIL: test_clamp_widths_agree_with_the_values_the_js_sets (max_width='240px')
  AssertionError: scout.js clamps an element to 240px but no rule in style.css agrees
FAIL: test_dismissed_clamp_widths_are_still_respected
  AssertionError: #scoutCurrentOpeningTag still uses the stale 210px bound
FAIL: test_overlap_strips_have_declared_min_height_and_never_wrap (sel='.scout-card-overlap-badges')
  AssertionError: 'flex-wrap:nowrap' not found in 'min-height:22px;'
FAIL: test_source_badge_classes_are_styled (cls='scout-badge-youtube')
  AssertionError: .scout-badge-youtube is emitted by scout.js but has no CSS rule at all
FAIL: test_live_indicator_has_an_idle_state
  AssertionError: 'animation:none' not found in ''
FAIL: test_dead_overlap_banner_css_is_removed
  AssertionError: .scout-overlap-banner still has styling but no element and no JS reference
FAIL: test_dead_in_flow_notice_nodes_are_removed (id='settingsAlert')
  AssertionError: #settingsAlert is still in index.html; a display:none notice container
                  in the flow is an §8.1 violation waiting to be resurrected
FAIL: test_every_filter_literal_the_js_implements_has_a_button (filter='favorites')
  AssertionError: 'favorites' not found in {'all', 'youtube', 'eco_guide', 'recent'}
FAIL: test_explorer_table_columns_are_explicitly_sized
  AssertionError: 'white-space:nowrap' not found in 'padding:0.6rem 0.6rem;… overflow:hidden;
                  text-overflow:ellipsis;'
```

### 3.2 Which of the 29 were green before the fix (honest accounting)

**22 red pre-fix, 7 green pre-fix.** The 7 are deliberate **zero-regression standing guards**
— they pin a property FIXER-F/G had already established correctly, and the task explicitly
asked for standing tests ("add a standing test so it cannot return", verify item 4):

| test | why green pre-fix |
|---|---|
| `test_every_table_in_the_app_is_fixed_layout` | `.explorer-table` already had `table-layout: fixed` + `width: 100%` |
| `test_the_gap_lists_are_not_unconstrained_tables` | FIXER-F's two gap lists are divs |
| `test_eval_bar_label_is_clamped` | CODE3-20 already clamped `.eval-bar-label` |
| `test_blunder_banner_placeholder_keeps_the_strip_in_flow` | CODE3-03 already gave the banner a 46px box + placeholder |
| `test_toast_container_is_fixed_and_out_of_the_flow` | `.app-toast-container` was already `position: fixed` |
| `test_survivor_badges_are_still_styled` | the three overlap-badge classes are FIXER-F's |
| `test_eco_badge_is_used_in_the_html_and_has_a_rule` | was green only because it then checked *presence*; I tightened it to also require a rule, so it is red pre-fix now |

### 3.3 Mutation check — do the guards actually bite?

A green-guard set is worthless if it cannot fail, so I built a **sabotaged** copy
(`/tmp/opencode/fixerh_mut`: pre-fix tree with 8 targeted mutations — `table-layout: fixed`
deleted, `.app-toast-container` set to `position: static`, `.eval-bar-label`'s selector
renamed, the blunder banner's `height`/`min-height` removed, the three overlap-badge
selectors renamed, the `.badge-eco` rule deleted, `display:none` put back on
`#btnScoutRemoveFromRep`, and the `favorites` button removed):

```
$ LAYOUT_FRONTEND_DIR=/tmp/opencode/fixerh_mut \
      /home/robin/miniconda3/bin/python3 -m unittest tests.test_layout_contracts
Ran 29 tests in 0.455s
FAILED (failures=60)
```

Every guard fires; the only test that stayed green is
`test_the_gap_lists_are_not_unconstrained_tables`, which is correct — that mutation was in
the CSS, not in the gap-list markup.

---

## 4. Verification results

### 4.1 Isolated module
```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_layout_contracts
Ran 29 tests in 0.585s
OK                       (stable across repeated runs; no DB, no network, no ordering state)
```

### 4.2 Full suite
```
$ timeout 400 /home/robin/miniconda3/bin/python3 -m unittest discover tests
Ran 375 tests in 20.829s
FAILED (failures=2)

FAIL: test_evaluator_illegal_fen_safety (test_repertoire.TestRepertoire…)
FAIL: test_get_game_gaps_performs_no_writes_and_persists_no_sentinel (test_repertoire_write_safety…)
```

**Neither failure is mine, and neither can be.** Both live in `tests/test_repertoire.py` and
`tests/test_repertoire_write_safety.py` (files FIXER-D/other fixers own — I did not touch
them), both exercise the Stockfish engine and the live explorer, and **neither reads
`frontend/` at all**. Grepping the full-suite log for my module:

```
$ grep -E "^(FAIL|ERROR): .*layout_contracts" /tmp/opencode/fixerh_full2.txt
(empty — my module is clean in the discover run)
```

Isolated, they behave the same and show the same two engine/network-dependent assertions:

```
$ /home/robin/miniconda3/bin/python3 -m unittest \
      tests.test_repertoire.TestRepertoire.test_evaluator_illegal_fen_safety \
      tests.test_repertoire_write_safety
Ran 9 tests in 2.393s
FAILED (failures=1)

  test_evaluator_illegal_fen_safety:  self.assertEqual(good["type"], "eval")  →  'error' != 'eval'
      : the engine must still work after a refused FEN
  test_get_game_gaps_…:                self.assertTrue(lookups, "the blunder rows were never
      resolved at all")  →  [] is not true
```

Baseline for this wave was `Ran 104 … OK` and FIXER-F's last sample was `Ran 266 … failures=10`;
the suite is at 375 tests with 2 failures, both in other agents' backend/engine territory and
with a run-to-run varying set while `backend/**` is being edited.

### 4.3 Isolation / production-data invariants
```
$ /home/robin/miniconda3/bin/python3 -m unittest tests.test_isolation_guard
Ran 12 tests in 2.490s
OK
```
`tests/test_layout_contracts.py` contains no `backend` import, no `chess_improvement.db`
literal, no `38281`, no `CHESS_DB_PATH` and no `fail-open` default — grep for all of them
returns only the two lines of the module docstring that say so. I never ran
`POST /api/repertoire/seed`, `/clear`, `/api/sync`, the desktop app, or a request to port 38281.

### 4.4 Structural sanity of the files I edited
```
$ python3 -c "css=open('frontend/style.css').read(); print(css.count('{')==css.count('}'))"
True        # 438 / 438 braces
$ python3 -c "h=open('frontend/index.html').read(); print(h.count('<div'), h.count('</div>'))"
245 245     # div open/close still balanced after deleting 2 divs and adding 5 comment blocks
```

---

## 5. Hand-off

### 5.1 DUP-01 — cross-file `SessionLineTree` consolidation — **still needs FIXER-J**
Unresolved; unchanged by this patch and **not** actionable by me (`repertoire_builder.js` and
`scout.js` are FIXER-J's / FIXER-G's). Restating FIXER-G §4 so it is not lost:
1. Extract the canonical `SessionTreeNode` + `SessionLineTree` from
   `frontend/repertoire_builder.js:3-172` into e.g. `frontend/session_line_tree.js`, loaded
   before both `repertoire_builder.js` and `scout.js`, exporting `window.SessionTreeNode` /
   `window.SessionLineTree`; delete **both** local declarations.
2. The merged class must keep the union of both copies: `getAllLines()` returning `lineId` /
   `studyName` / `node.lineName`, and `addMove`'s richer metadata sync
   (`comment` / `is_important` / `arrows`).
3. Until that lands, the builder's copy still needs the same three fixes the Scout's already
   has — `_buildPgn` with the `1...` prefix for black-first lines (CODE-03), `deleteLine` that
   repairs `currentNode` via an `isAttached()` walk (CODE-02), and `getAllLines().lineId` as
   the overlap key (CODE-06).
4. Afterwards the two API alias pairs can collapse to one, and the Builder's and Scout's
   `treeOverlapMap` can become one (today a Scout import does not refresh the Builder's
   badges).
**My new file `tests/test_layout_contracts.py` is JS-agnostic, so it will not break when that
refactor lands** (it only resolves ids and class names, never JS functions).

### 5.2 FIXER-G / `repertoire_trainer.js` — the 44px note box is ~13px short
`#trainNoteBox` is pinned to `min-height: 44px; max-height: 44px; overflow: hidden` (that is
the hand-off figure and it is also `_updateNoteBox()`'s runtime constant), but the box needs
about 56.7px: `0.6rem` padding ×2 = 19.2px, the "📝 Move Note / Idea:" label ≈ 17.8px
(0.72rem × 1.2 + 0.25rem margin), and one 1.45-line note ≈ 19.7px. With the universal
`box-sizing: border-box` (`style.css:19-20`) the last ~12.7px of the note line is **clipped**.
This is *pre-existing runtime behaviour*, not something this patch introduces — and it is
**not fixable from `style.css`**, because the JS writes `minHeight`/`maxHeight` inline and an
inline declaration beats the stylesheet. Two options, both in a file I do not own:
- raise the constant to ~58px in `repertoire_trainer.js::_updateNoteBox()` **and** the
  matching `#trainNoteBox` rule in `style.css` (or the two drift again), or
- move the label onto the same line as the note.
I documented the arithmetic in a comment at `style.css:2704-2715` and pinned the 44px
contract in `test_constant_height_elements_declare_a_height`, so if the constant is raised the
test tells the author to raise the CSS number in the same change.

### 5.3 FIXER-G / `scout.js` — `_clampInline` vs the stylesheet must stay in lockstep
`#scoutCurrentOpeningTag` / `#scoutNoteMoveLabel` / `#scoutTurnBadge` are now clamped in
**both** places, and `test_clamp_widths_agree_with_the_values_the_js_sets` reads
`_clampInline(<el>, '<N>px')` out of `scout.js` and fails if no stylesheet rule matches. If
anyone changes a clamp width in `scout.js`, that test fires and names the mismatch.

### 5.4 FIXER-J — nothing needed, stated explicitly
My `index.html` edits are eight single-element replacements at lines 543, 555, 572, 586–590,
665, 782–787 and 793–794, all matched by unique `id=`/`data-filter=` literals. They are
additive in style (inline `visibility` + a `min-width`/`min-height` reservation) and remove
exactly two dead `display:none` divs. Nothing FIXER-J is doing in `repertoire_builder.js` or
`board.js` can conflict, and `index.html`'s mtime is unchanged since my last edit.

### 5.5 `AGENTS.md` §4E — still advertises a panel that does not exist
Hand-off (b) item 5: CODE-15 removed `scoutCoreIdea` / `scoutKeyPlans` / `scoutTypicalMistakes`
from `index.html`, but §4E still promises "opening themes, core ideas, key plans, and common
tactical mistakes". The backend already computes `study.strategic_ideas` on every analyse, so
restoring it is one container with three ids. I did **not** do it: it is a feature decision
(HTML-only change, but the content to show has to be agreed), and it is outside the §8 work
list. Either restore it or amend §4E.

---

## 6. Also noticed (not fixed)

1. **`#trainNoteBox`'s 44px box clips its own content** — see §5.2. The CSS side is pinned and
   documented; the fix is in `repertoire_trainer.js`.
2. **Nine classes `scout.js` emits still have no CSS rule**, and they are the same *kind* of
   gap as the three I fixed — they render as bare unstyled text and have a different height
   than the 20px badge row reserved by §8.5:
   `btn-del-session-line`, `btn-load-tree-line`, `btn-import-tree-white`, `btn-import-tree-black`,
   `btn-del-tree-white`, `btn-del-tree-black`, `scout-line-name`, `scout-line-study-row`,
   `scout-card-meta`. Most are marker classes on elements that are styled inline
   (`style="font-weight:700; color:#fff; …"`), so the practical impact is small, but
   `scout-card-meta` (the row that *contains* the three badges I just styled) and
   `scout-line-study-row` are unstyled containers. I left them: they are a styling decision,
   not a §8 contract, and the brief says keep the diff surgical.
   *Repro:* `python3 -c "import re;css=open('frontend/style.css').read();js=open('frontend/scout.js').read();print([c for m in re.finditer(r'class=\"([^\"\$]*)\"',js) for c in m.group(1).split() if '.'+c not in css])"`
3. **`.badge-pill` has no rule either.** `#scoutResultsCount` (which FIXER-G now writes) and
   the other count pills carry `class="badge-pill"`, which is not in `style.css` at all — so
   the result count is unstyled text. Same class of gap, not on my list.
4. **`#trainFeedback` is still `display`-toggled** on every answer
   (`repertoire_trainer.js:508/784/845/880/906/945`). It is the *primary* feedback channel and
   a 1rem-padded box, so it is the largest remaining in-flow reflow in the app. §8.4's
   `visibility` + a reserved `min-height` would fix it, but it changes the visible feedback
   rhythm and FIXER-G explicitly left it (their §5.3). Needs a product decision, not a
   mechanical fix.
5. **`.scout-overlap-banner` deletion is a judgement call.** If FIXER-J or a later fixer
   re-introduces an element with that class, `test_dead_overlap_banner_css_is_gone` fails with
   a message telling them to restore the CSS — so the failure is informative, not a trap.
6. **The `#scoutCurrentOpeningTag` clamp is 240px in CSS now, and the JS also sets
   `display: inline-block` + `vertical-align: bottom` inline while the CSS says
   `vertical-align: middle`.** The inline value wins, so the stylesheet's `middle` is dead for
   that element. Harmless (both are single-line tags) but it is a genuine duplication between
   the two layers that only the inline style resolves.
7. **My tests cannot catch a rendering regression** — see §0. If a real CLS is suspected, the
   next step is a headless-browser check (Playwright/Puppeteer) or a manual Ctrl+R pass, not
   another static scan. The static layer is now complete for the five §8 sub-rules; adding a
   sixth static test for a property that is already pinned would only add maintenance cost.
