# CODE-4 — Unproven suspicions

Things I could not confirm statically within scope. Each would need a browser
(Electron renderer) or a live backend to settle. **None of these is reported as
a finding** — they are leads for the attacker agents or for a follow-up pass.

## US-1 — `_updateEvalBar` inverts the eval bar on board flip
`frontend/scout.js:1985-1987`:
```js
if (this.orientation === 'black') { pct = 100 - pct; }
```
The bar is a *vertical* fill whose height represents White's advantage, so
flipping the board should arguably not change the sign convention. It may be
intentional ("advantage for the side to move") or a colour/turn confusion.
`AGENTS.md` §4C defines the logistic mapping without an orientation term, which
weakly suggests the flip is a bug — but `repertoire_builder.js` needs the same
check and I could not confirm the intended UX. **Needs a browser.**

## US-2 — `AudioContext` autoplay: is the very first sound ever inaudible?
`frontend/audio.js:7-15` creates the context lazily on the first `play*` call,
and several call sites reach it after an `await` (`scout.js:1668`, `1748`,
`1818`; `repertoire_trainer.js:209`, `680`, `847`). Chromium's *sticky* user
activation normally survives an `await`, so I expect this to work in practice.
I could not verify the Electron 44 preload/preload-policy combination, nor
whether `electron/main.js` sets any autoplay flag. **Needs a running app.**

## US-3 — Does the orphaned-`currentNode` path (CODE-02) also drop a DB write?
`deleteSessionLine` (`scout.js:1907`) does not touch the board. After the
orphan, `_onUserBoardMove` still records `fen_before = this.lastBoardFen`
(a real position) and POSTs it. Whether the resulting
`(parent_fen, move_uci)` pair is accepted or silently rejected by
`add_repertoire_move` depends on whether the orphan chain happens to be a legal
continuation — I did not construct a case where it is not, so I cannot claim a
DB write is lost, only that the *tree* loses the move (which is proven).

## US-4 — Could a crafted `san` reach `title="${wData.message}"`?
`backend/repertoire.py:1113-1120` embeds client-supplied `san` in the overlap
`message`, which `scout.js:1204-1218` and `1545-1559` interpolate into a
`title="..."` attribute. All SAN values in the normal flow come from
`board.san()` / `python-chess` and are safe. I could not find a path that puts
a `"` into `san` (the client's own `formatMoves` only forwards what the tree
holds, and the tree only holds chess.js/python-chess SAN). **Likely safe, but
the sink is real and should be escaped defensively.**

## US-5 — `scout.js:409` error-message interpolation
```js
grid.innerHTML = `...Failed to load scout results: ${e.message}</div>`;
```
`e.message` comes from `fetch`/`res.json()` failures, so it is browser-generated
in practice. A proxy or a non-JSON error body could in principle carry
attacker text. I did not confirm a delivery path.

## US-6 — `promptStudySelection` cross-invocation handler clobbering
`app.js:252-253, 414-417` install handlers with `onclick =` on **shared,
module-level DOM nodes**. Two interleaved invocations could leave the *first*
invocation's `handleConfirm` bound (or nulled by the second's `cleanup()`),
resolving the wrong promise. I modelled the `showModal()` throw (CODE-18) but
not this specific interleaving, because it needs a real `<dialog>` plus a
pending `fetch` to order the events. **Needs a browser.**

## US-7 — Scope boundary: are `board.js` XSS-free?
`board.js` builds arrows with `createElementNS` + `setAttribute` (lines 545-586)
rather than `innerHTML`, so arrow `color`/`from`/`to` from the DB are not an
injection sink — I checked this specifically because the brief flagged
"arrow JSON". I consider it clean. The one remaining `innerHTML` in `board.js`
is `this.svgEl.innerHTML = ''` (a literal empty string). **No finding.**

## US-8 — `repertoire_trainer.js` `alert()` at lines 424/428
Blocking `window.alert` on the 404 path. Not reported as a CLS finding because
`alert` is out-of-flow, but it is a UX defect (a modal dialog in a desktop app)
and may be considered by the orchestrator as part of CODE-08's "no surfaced
failure" theme.

## Negative results (checked, found clean)
* No `TODO` / `FIXME` / `HACK` / `XXX` in any of my four files.
* No bare `console.log`; all logging is `console.error` / `console.warn`.
* No `Date.now()` / `new Date()` anywhere in my four files, so the classic
  seconds-vs-milliseconds mixup **cannot** occur. The only timestamp-like
  values are `ch.start_time` (seconds, from `backend/scout.py:format_seconds`)
  fed to `ytPlayer.seekTo(seconds, true)`, which expects seconds — correct.
* No `setInterval` in my files; every `setTimeout` delay is in ms and no
  `setTimeout` delay is unit-confused.
* No loose `==` / `!=`; every `parseInt` supplies radix 10; every `parseFloat`
  result is `isNaN`-guarded.
* No `var` in loops; all `let`/`const`.
* No `encodeURIComponent` omissions on user-controlled URL parameters
  (`scout.js:402`, `1930`; `repertoire_trainer.js:216`, `401-417`;
  `app.js:259`). `scout.js:558` and `492` interpolate `study.video_id` into an
  `href` without encoding, but that value is regex-constrained to
  `[a-zA-Z0-9_-]{11}` by `backend/scout.py:extract_youtube_id`, and the `href`
  is a property assignment (not `innerHTML`), so it is not injectable.
* `scout.js:49-52` — the FEN clause of `addMove` is unreachable (two distinct
  legal moves from one position never reach the same FEN); reported as part of
  CODE-17 rather than as an XSS/dead-code finding of its own.
* `repertoire_trainer.js` sends `/api/repertoire/train/progress` **twice** per
  `startNewDrill` (`loadOpeningsList` + `loadMasteryProgress` in the same
  `Promise.all`) — a real waste, folded into CODE-05 rather than a separate
  PERF finding.
