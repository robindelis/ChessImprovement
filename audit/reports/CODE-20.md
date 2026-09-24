# CODE-20 — `audio.js`: no master bus, no try/catch, ignored `resume()` promise, and a success chime for destructive actions
- **Severity:** low
- **Type:** crash
- **File:** frontend/audio.js (all 114 lines); callers at frontend/scout.js:1668, 1748, 1818, 1861, 1895
- **Component:** ChessAudio
- **Repro script:** `audit/scripts/CODE4_audio_scan.sh`
- **Detected by:** CODE-4

## Expected
A tiny synth helper should (a) never throw into its callers, (b) route all
voices through one master gain so overlapping notes cannot sum past full scale,
(c) handle a suspended/rejected `AudioContext`, and (d) not use a "success"
timbre for destructive operations.

## Actual
Each of the four voices connects **directly to `destination`** with its own
hard-coded peak gain, and there is no limiter, no master gain node and no voice
cap:

```js
// frontend/audio.js:17-35
playMove() {
    this._init();
    const now = this.ctx.currentTime;
    const osc = this.ctx.createOscillator();
    const gain = this.ctx.createGain();
    ...
    gain.gain.setValueAtTime(0.3, now);
    gain.gain.exponentialRampToValueAtTime(0.01, now + 0.08);
    osc.connect(gain);
    gain.connect(this.ctx.destination);       // <-- straight to output
    osc.start(now); osc.stop(now + 0.08);
}
```

`playCapture` uses `0.4`, `playCheck` fires two voices at `0.2`,
`playSuccess` fires **four** at `0.25` 70 ms apart, `playBlunder` uses `0.25`.
Nothing throttles or bounds concurrency, and `this.ctx` is never `close()`d and
the gain nodes are never `disconnect()`ed (harmless for one-shot sources, but
there is no teardown path at all).

`_init()` is unguarded:

```js
// frontend/audio.js:7-15
_init() {
    if (!this.ctx) {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        this.ctx = new AudioContext();
    }
    if (this.ctx.state === 'suspended') {
        this.ctx.resume();          // <-- promise ignored; can reject
    }
}
```

No `try/catch` around the constructor, and the `resume()` promise is discarded,
so a failed resume becomes an unhandled rejection. Note the context is created
**lazily on the first `play*` call**, and several call sites reach it *after* an
`await`:

```js
// frontend/scout.js:1810-1818  (markLastMoveAsImportant)
for (const rep of ['white', 'black']) {
    try {
        const res = await fetch('/api/repertoire/move_meta', { ... });
        ...
    } catch (_) {}
}

if (window.chessAudio) window.chessAudio.playSuccess();
```

```js
// frontend/scout.js:1666-1668  (importToRepertoire)
const data = await res.json();
if (data.status === 'success') {
    ...
    if (window.chessAudio) window.chessAudio.playSuccess();
```

`repertoire_trainer.js:209` (`onSessionComplete` → `playSuccess()`) and
`:680` / `:847` are likewise all downstream of an `await fetch(...)` in
`onUserAttempt`. If the very first sound of a session is one of these, the
`AudioContext` is constructed outside a user-activation task and starts
`suspended`; `_init()` retries `resume()` on every later call, but the
oscillators scheduled in the meantime are queued at a non-advancing
`ctx.currentTime` and all fire together the moment the context does resume.

Finally, the bright ascending "success" arpeggio is used for **destructive**
operations, in the same statement block as a red toast:

```js
// frontend/scout.js:1860-1862  (removeCurrentLineFromRepertoire)
if (data.status === 'ok') {
    if (window.chessAudio) window.chessAudio.playSuccess();
    this._showQuickAddNotice(`✓ Removed from ${repName} Repertoire`, '#ef4444');
```

and `scout.js:1895` (`removeTreeLineFromRepertoire`) does the same. Worse,
`markLastMoveAsImportant` plays `playSuccess()` **even when nothing was
persisted**:

```js
// frontend/scout.js:1816-1822
let persisted = false;
for (const rep of ['white', 'black']) { ... if (data.status === 'success') persisted = true; }

if (window.chessAudio) window.chessAudio.playSuccess();
const noticeMsg = newImportant
    ? (persisted ? `⭐ Marked ${moveName} as Important in repertoire!`
                 : `⭐ Marked ${moveName} as Important (saved on line import)`)
```

`persisted` is only consulted for the *text*, never for the sound — so the
"nothing was saved" case still gets the success chime.

## Root cause
No shared output bus/limiter, no error boundary, no activation-state handling,
and no distinction between "success" and "informational" feedback.

## Exploit / escalation
1. **Clipping.** Rapid board moves stack `playMove` (0.3) tails; a capture
   right after a check stacks `playCheck`'s two voices on top. With
   `playSuccess`'s four voices layered on an in-flight `playMove`, the summed
   gain exceeds 1.0 and the Web Audio graph clips audibly. `markLastMoveAsImportant`
   spam-reachable (each click = 4 voices, 250 ms each).
2. **Uncaught throw reaches the board.** If `new AudioContext()` throws (or
   `window.AudioContext` is undefined in a restricted preload context — note
   `electron/preload.js` is the bridge), the exception propagates from
   `board.js:475 _playMoveAudio` into `_executeMove`'s animation callback,
   which also owns `this.render()` and `this.options.onMove(...)` — so the board
   would stop rendering *and* stop recording the move. `_playMoveAudio` has an
   `if (!window.chessAudio) return;` guard but no `try/catch`.
3. Adjacent weaker variant: because `resume()` is retried on every call and the
   oscillators are scheduled at a frozen `currentTime`, the very first
   post-`await` sound of a session either plays as a burst of every queued note
   or is lost entirely, with no diagnostic.
