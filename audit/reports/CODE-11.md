# CODE-11 — If the YT iframe API is not ready on first open, chapter seeking is dead for the whole session
- **Severity:** high
- **Type:** wrong-result
- **File:** frontend/scout.js
- **Line(s):** 630-658 (`setupYouTubePlayer`), 660-664 (`seekVideoTo`), 2050-2052 (init)
- **Component:** OpeningScout.setupYouTubePlayer
- **Repro script:** `audit/scripts/CODE4_yt_fallback_repro.js`
- **Detected by:** CODE-4

## Expected
`setupYouTubePlayer` must either wait for `window.onYouTubeIframeAPIReady` /
retry until `window.YT.Player` exists, or upgrade the fallback iframe into a
real `YT.Player` later. `seekVideoTo` must be a no-op **only** while genuinely
waiting, and the state must be reset when a new video is loaded.

## Actual
```js
// frontend/scout.js:630-658
setupYouTubePlayer(videoId) {
    if (window.YT && window.YT.Player) {
        if (this.ytPlayer && typeof this.ytPlayer.loadVideoById === 'function') {
            this.ytPlayer.loadVideoById(videoId);
            this.ytPlayerReady = true;
        } else {
            this.ytPlayer = new window.YT.Player('scoutYoutubePlayer', { ... });
        }
    } else {
        // Fallback iframe
        const playerEl = document.getElementById('scoutYoutubePlayer');
        if (playerEl) {
            playerEl.innerHTML = `<iframe id="scoutIframeFallback" src="https://www.youtube.com/embed/${videoId}?enablejsapi=1" allowfullscreen></iframe>`;
        }
    }
}
```

```js
// frontend/scout.js:660-664
seekVideoTo(seconds) {
    if (this.ytPlayer && this.ytPlayerReady && typeof this.ytPlayer.seekTo === 'function') {
        this.ytPlayer.seekTo(seconds, true);
    }
}
```

`<script src="https://www.youtube.com/iframe_api">` (`index.html:921`) is a
render-blocking classic tag, but it only *queues* the real
`youtube.com/player.js`; `window.YT` is therefore **not** guaranteed to exist
when `OpeningScout` is constructed on `DOMContentLoaded`
(`scout.js:2050-2052`) nor when the user opens the first study. When the guard
fails:

* the fallback branch runs and `this.ytPlayer` stays `null` **forever** —
  there is no retry, no `onYouTubeIframeAPIReady` hook, no polling;
* therefore `this.ytPlayerReady` stays `false` and **`seekVideoTo()` is a
  permanent no-op**, so the `#scoutChapterSelect` dropdown silently does
  nothing for the rest of the session (it is never disabled or greyed out, so
  the user gets zero feedback);
* the injected `<iframe id="scoutIframeFallback">` is never removed. On a
  later study, once `window.YT` *is* available, the code takes the
  `else` branch and calls
  `new window.YT.Player('scoutYoutubePlayer', …)` on a div that still contains
  the fallback iframe — and `loadVideoById` is never used for that first
  upgrade, so two players briefly coexist.

A second, independent defect: on the *fast* path,
`this.ytPlayer.loadVideoById(videoId)` is followed by
`this.ytPlayerReady = true` **unconditionally** (line 634), so `seekVideoTo`
will call `seekTo` on a player that is still loading the new video, and a
chapter selected within the first moments of a newly loaded video is applied
against the *old* timeline.

## Root cause
A one-shot capability check with no deferred retry, plus optimistic
`ytPlayerReady = true` on a `loadVideoById` that has not completed.

## Exploit / escalation
1. **Reproduced logic path** (see repro script): with `window.YT` undefined at
   `setupYouTubePlayer` time, after loading the API and clicking any chapter,
   `seekVideoTo` is a no-op for the whole session.
2. **Race on video switch.** Open study A, immediately open study B. `A`'s
   `onReady` callback (`scout.js:645-647`) is still pending; it fires and sets
   `ytPlayerReady = true` for the player that is now loading B, so the very
   first chapter click seeks within A's timeline.
3. Adjacent weaker variant: `toggleFavorite()` (`scout.js:2028-2045`) does not
   reset `#btnScoutFav`'s label when a new study is opened, so the button keeps
   saying "★ Favorited" for a video that was never favorited (and
   `openStudio` never initialises it from `study.is_favorite`).
