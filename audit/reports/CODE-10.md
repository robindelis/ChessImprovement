# CODE-10 — The YouTube↔board sync described in AGENTS §4E does not exist; two dedicated DOM elements are dead
- **Severity:** high
- **Type:** spec-violation
- **File:** frontend/scout.js (525-664); elements at index.html:671-672
- **Component:** OpeningScout video/board synchronisation
- **Repro script:** `audit/scripts/CODE4_dead_selector_scan.sh`
- **Detected by:** CODE-4

## Expected
`AGENTS.md` §4E: *"Embedded YouTube player with chapter timestamps …"* and
*"play back lines in sync with the interactive board."* Concretely that needs
`onStateChange` / `onCurrentTimeChanged` handlers (or a `message` listener
validating `event.origin === 'https://www.youtube.com'`) that drive
`#scoutCurrentTimeDisplay` and `#scoutLiveIndicator`, and that step the board
as the video plays.

## Actual
`setupYouTubePlayer` registers exactly one event — `onReady`, which only sets a
boolean:

```js
// frontend/scout.js:636-649
this.ytPlayer = new window.YT.Player('scoutYoutubePlayer', {
    videoId: videoId,
    playerVars: { autoplay: 0, enablejsapi: 1, rel: 0, modestbranding: 1 },
    events: {
        onReady: () => {
            this.ytPlayerReady = true;
        }
    }
});
```

There is **no** `onStateChange`, **no** `setInterval` polling
`getCurrentTime()`, **no** `window.addEventListener('message', ...)` anywhere
in the file, and the only time value the client ever handles is the chapter
dropdown's forward seek:

```js
// frontend/scout.js:302-310
chapSelect.addEventListener('change', (e) => {
    const sec = parseFloat(e.target.value);
    if (!isNaN(sec)) { this.seekVideoTo(sec); }
});

// frontend/scout.js:660-664
seekVideoTo(seconds) {
    if (this.ytPlayer && this.ytPlayerReady && typeof this.ytPlayer.seekTo === 'function') {
        this.ytPlayer.seekTo(seconds, true);
    }
}
```

The two HTML elements reserved for the sync UI are never written by **any**
JavaScript file:

```html
<!-- index.html:671-672 -->
<span class="scout-live-indicator" id="scoutLiveIndicator"></span>
<span id="scoutCurrentTimeDisplay" style="font-family:monospace; ...">00:00 / 00:00</span>
```

```
$ # ids present in index.html but never referenced by any frontend/*.js
btnScoutSubmit, lblMyStatsHeader, repBottomSidebarCard, repMoveToolsPanel,
repQuickAddNotice, scoutCurrentTimeDisplay, scoutEvalBarWrapper,
scoutLiveIndicator, scoutLoadingText, scoutMoveNoteBar, scoutQuickAddNotice,
scoutResultsCount, syncDetailLabelSynced, trainMoveHistoryBreadcrumb,
trainPrompt
```

`#scoutResultsCount` (`index.html:596`, the "Discovered Courses" count pill) is
likewise never updated — it is stuck at `0` even with 12 results on screen,
and `renderResults()` never writes to it.

The per-move `timestamp` / `timestamp_formatted` fields the backend carefully
computes for every extracted move (`backend/scout.py:368-369`, `:705-706`) are
sent to the client and **never read**:

```
$ grep -n "timestamp" frontend/scout.js      -> no matches
```

## Root cause
The video-sync feature was either never implemented or was removed without
cleaning up its DOM hooks. The player's own event surface is unused, so the
board and the video are two completely independent widgets that happen to sit
next to each other.

## Exploit / escalation
1. **User-visible dead UI.** The meta bar always reads `00:00 / 00:00` and the
   live indicator is a permanently inert dot, so the user cannot tell whether
   the video is playing, paused, or which chapter they are in.
2. **Result count lies.** `badge-pill` `#scoutResultsCount` shows `0` while the
   grid below it is full — a visible inconsistency in the main results header.
3. **No spoofing surface (positive).** Because there is no `message` listener,
   there is also no missing-`event.origin`-check vulnerability today. Adding
   one naively later would be a security regression, so the fix must validate
   `event.origin` and `event.source === ytPlayer.getIframe()` from the start.
4. Adjacent weaker variant: `#scoutLoadingText` (`index.html:608`) is also
   never written, so the spinner always says "Scouting online for chess guides
   and videos…" even for a PGN paste.
