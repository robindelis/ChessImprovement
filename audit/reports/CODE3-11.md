# CODE3-11 — §8.4 violation: "📥 Add Opening" relabels itself twice with no fixed width inside a `space-between` card, shifting its neighbours — and is never re-enabled
- **Severity:** medium
- **Type:** ui-regression
- **File:** frontend/repertoire_builder.js
- **Line(s):** 1006-1035 (`loadRecentGames` per-card action group)
- **Component:** Repertoire Builder — Recent Games cards
- **Repro script:** `audit/scripts/CODE-11_cls_add_opening.js`
- **Detected by:** CODE-3

## Expected
Per `AGENTS.md` §8.4, conditionally-available actions in a fixed toolbar/card must have a fixed width
(`min-width` + centred content) so neighbouring buttons and headers never twitch.

## Actual
```js
// repertoire_builder.js:1006
<div style="display:flex; align-items:center; gap:0.35rem; flex-shrink:0;">
    <button class="btn-primary btn-review-game" ...>Review & Explore &rarr;</button>
    <button class="btn-secondary btn-quick-add-opening" ...>📥 Add Opening</button>
    <a href="${g.game_url}" ...>&rarr;</a>
</div>
```

```js
// repertoire_builder.js:1027
btnAdd.addEventListener('click', async (e) => {
    e.stopPropagation();
    btnAdd.disabled = true;
    btnAdd.textContent = 'Adding...';                       // width: "📥 Add Opening" -> "Adding..."
    await this.importGameOpening(g.id, g.user_color);
    btnAdd.textContent = '✓ Added!';                        // width: -> "✓ Added!"
    btnAdd.style.color = '#4ade80';
});
```

There is **no** `min-width` on `.btn-quick-add-opening` (or on `.btn-review-game`) anywhere in `style.css` — verified:

```
$ grep -n "btn-quick-add-opening\|btn-review-game" style.css      # no match
```

so three successive intrinsic widths ("📥 Add Opening" ≈ 118 px, "Adding..." ≈ 74 px, "✓ Added!" ≈ 66 px) propagate
into the parent action group, which sits in a `justify-content: space-between` card (`.game-card`, `style.css:1531`).
The right-hand group therefore shifts ~50 px horizontally twice per click, and the "↗" Lichess link moves with it.

Functional half of the same finding: `btnAdd.disabled = true` is never reset. After one import the button is
permanently inert for the lifetime of that card, and there is no `finally` block, no re-render and no
`btnAdd.disabled = false` anywhere in the file — so the user must switch tabs and re-fetch the list to be able to
import the same opening again.

## Root cause
Button state is communicated by swapping `textContent` on an auto-width element inside a `space-between` flex row,
instead of the `visibility`/`opacity` + fixed `min-width` pattern that `style.css:2778-2786` already applies to
`#btnRepToggleImportant` / `#btnRepDeleteCurrentMove`. The same pattern is repeated in the sync button
(`origText`/`'⏳ Syncing...'` at 1047-1051, 1237) and in the study modal (`app.js:216, 365, 377`).

## Exploit / escalation
1. Click "📥 Add Opening" on the first card and watch the second/third buttons and the ↗ link jump 50 px left, then
   8 px right — two distinct layout shifts per click, in a list of up to 40 cards.
2. Because the list is re-rendered by `loadRecentGames()` on any search/filter change, the "stuck disabled" state is
   invisible until the user happens to filter — a state that appears and disappears without explanation.
3. Same root cause, opposite direction: `renderGameReviewBanner()` conditionally *emits* the
   "📖 End of Theory (Move N)" button (line 1358-1362), so the banner's action row has a different number of buttons
   depending on `theoryPlies` — but that value is constant per game, so no shift there (verified, not a finding).
