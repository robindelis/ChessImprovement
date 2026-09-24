# CODE3-10 — §8.1 violation: a temporary in-flow progress banner is prepended to the Recent Games list, pushing all 40 cards down and snapping back 2.5 s later
- **Severity:** medium
- **Type:** spec-violation
- **File:** frontend/repertoire_builder.js
- **Line(s):** 1082-1105 (widget creation), 1150-1155 (updates), 1240-1243 (removal)
- **Component:** Repertoire Builder — Recent Games sync progress
- **Repro script:** `audit/scripts/CODE-10_cls_games_sync.js`
- **Detected by:** CODE-3

## Expected
Per `AGENTS.md` §8.1, temporary/dynamic notification content must go through the fixed-position toast system
(`window.showToast`) and must never expand a container or push surrounding elements. All 40 game cards must keep
their vertical positions for the whole duration of a sync.

## Actual
`syncRecentGames()` builds a ~60 px-tall widget and inserts it at the **top of the scrollable games list**:

```js
// repertoire_builder.js:1083
const gamesList = document.getElementById('repGamesList');
let inlineWidget = document.getElementById('repGamesInlineProgress');
if (gamesList && !inlineWidget) {
    inlineWidget = document.createElement('div');
    inlineWidget.id = 'repGamesInlineProgress';
    inlineWidget.className = 'rep-games-sync-progress';
    inlineWidget.style.cssText = 'margin:0 0 0.6rem 0; padding:0.6rem 0.8rem; ...';
    inlineWidget.innerHTML = `... <div id="repGamesInlineBar" ...><div id="repGamesInlinePct">5%</div> ...`;
    gamesList.prepend(inlineWidget);            // <-- pushes every game card down ~60px
}
```

and removes it on a timer:

```js
// repertoire_builder.js:1240
setTimeout(() => {
    const el = document.getElementById('repGamesInlineProgress');
    if (el) el.remove();                        // <-- everything snaps back up 2.5 s later
}, 2500);
```

Two full-height layout shifts per sync (insert at t0, remove at t0+2.5 s), on top of a live bar whose width animates
(`transition:width 0.25s ease`). The `#repGamesList` container itself is `flex:1; overflow-y:auto` (`index.html:320`),
so the shift is a scroll-anchoring jump rather than a page reflow — but it is exactly the class of jitter §8.1
forbids, and the whole project's own progress card already exists in a fixed place (`#syncProgressContainer`,
`index.html:102`). The codebase even has a dead container for the purpose: `#repQuickAddNotice` (`index.html:194`,
`display:none !important`, never referenced — see CODE3-01).

Additionally the widget is created **only if absent** (`if (gamesList && !inlineWidget)`), so after a second sync
starts while the first widget is still within its 2.5 s removal window, the widget is not recreated and the new
stream updates a stale element — and if the removal timer of sync #1 fires while sync #2 is running, sync #2's
progress silently disappears from the UI.

## Root cause
Duplicated notification surface: the fixed `syncProgressContainer` card already shows the same information
(percent, message), but a second, in-flow copy was added inside the list. This is the "in-flow ephemeral notice"
pattern that `window.showToast` (`app.js:434`) was introduced to eliminate, and the vestigial `#repQuickAddNotice`
shows the migration was started but not finished.

## Exploit / escalation
1. Press "🔄 Sync Latest" repeatedly: each press can remove the *next* press's widget, leaving a running sync with no
   inline progress and the games list jumping under the user's cursor.
2. Because the widget is prepended to a `flex` column, an import triggered while the widget is present measures
   against a different list height.
3. `cancelSync()` (609-612) does clean this widget up (`inlineProgress.remove()`), but only when the handle is
   non-null (CODE3-04) — otherwise the widget stays until the timer fires.
