# CODE3-20 — §8.2/§8.5 violations: the eval-bar label and the overlap strip both render dynamic-length text with no clamp, so they wrap and shift
- **Severity:** low
- **Type:** ui-regression
- **File:** frontend/repertoire_builder.js
- **Line(s):** 1699-1729 (`renderEvalBar`), 2596-2653 (`checkActiveLineOverlap`); CSS frontend/style.css:300-355, 2524-2533, 2553-2560
- **Component:** Repertoire Builder — eval bar / overlap strip
- **Repro script:** `audit/scripts/CODE-20_cls_text_clamp.js`
- **Detected by:** CODE-3

## Expected
Per `AGENTS.md` §8.2 every element whose text length varies with the position must be
`white-space: nowrap; overflow: hidden; text-overflow: ellipsis` with explicit min/max width, and per §8.5 status
strips must have a constant height.

## Actual

**1. `.eval-bar-label` has no clamp at all.**

```js
// repertoire_builder.js:1715
const pawns = scoreCp / 100;
text = (pawns > 0 ? '+' : '') + pawns.toFixed(1);      // "-9.9" .. "+99.0"
...
fillEl.style.height = `${pct}%`;
labelEl.textContent = text;
```

```css
/* style.css:327 */
.eval-bar-label { position: absolute; width: 100%; text-align: center;
                  font-family: <mono>; font-size: 0.72rem; font-weight: 800; ... }
/* style.css:300 */
.eval-bar-wrapper { width: 28px; ... }
```

No `white-space: nowrap`, no `overflow`, no `min/max-width`. At `0.72rem` (11.52 px) a monospace glyph is ≈6.9 px, so
the 28 px-wide bar fits 4 characters (27.6 px) and **not 5**. Any evaluation of ±10.0 pawns or worse (routine in the
blunder-review flow, where positions are frequently decisive) renders `"+10.0"`, which wraps to two lines inside an
absolutely positioned, un-clamped label — the second line spills over the board. Mate scores are safe (`M5` = 2 chars)
and sub-10.0 cp values are safe, so the breakage is exactly at the point where it matters most. `.eval-bar-label` is
also duplicated in `scout.js` for `#scoutEvalBarLabel`, so the defect exists twice.

**2. `.scout-line-overlap-strip` declares `min-height: 28px` but is allowed to grow.**

```js
// repertoire_builder.js:2643
strip.innerHTML = `<span ...>Repertoire:</span>${getBadge(dataW,'White')}${getBadge(dataB,'Black')}`;
strip.style.display = 'flex';
```

```css
/* style.css:2553 */
.scout-line-overlap-strip { display: flex; align-items: center; gap: 0.4rem;
                            flex-wrap: wrap; font-size: 0.73rem; min-height: 28px; }
/* style.css:2524 */
.scout-overlap-badge { font-size: 0.68rem; ... display: inline-flex; align-items: center; gap: 0.2rem; }
```

`flex-wrap: wrap` plus badges with **no** `white-space: nowrap` and no `max-width` means the strip's content can
occupy 2 (or 3) lines. At ply 0 the strip is a single short span (28 px); one move later it is a label plus two
badges such as `⚡ White: 4/8 in Rep (+4 new)` and `★ Black: New` — easily >200 px of text plus padding. Below the
`max-width: 960px` breakpoint (`--board-size: min(520px, calc(100vw - 2.5rem))`, style.css:1060) the left column
narrows to ~360 px on a small window, the badges wrap, and the strip grows to ~46-64 px — a 20-35 px vertical shift
of everything below it, on *every* transition between ply 0 and ply ≥ 1, which is precisely the jump §8.5 was written
to prevent. `min-height` is only a floor, not a constant height. (`#scoutActiveLineOverlap` in `index.html:729` is a
second, identical instance of the same strip in the Scout view.)

## Root cause
The `min-height` remedy prescribed by §8.5 was applied without the accompanying `white-space: nowrap` on the
badge children, and `flex-wrap: wrap` was left enabled on a container that must not reflow. The eval bar predates the
§8 rules and was never audited.

## Exploit / escalation
1. Scrape/hold the `→` key on a narrow window and watch the card below the strip jump on each move; the same happens
   in the Scout view for `#scoutActiveLineOverlap`.
2. Any position where Stockfish reports ≥ ±1000 cp makes the eval label wrap over the board, and the label is
   re-rendered on every SSE frame (up to depth 16), so the wrap flickers while the score improves.
3. Neither element sets a dynamic `title`, so the truncated/wrapped text cannot be recovered on hover as §8.2
   requires (`#repOpeningTitle`/`#repOpeningEco` do this correctly at lines 1441-1447 — the pattern already exists in
   this file and was simply not applied here).
