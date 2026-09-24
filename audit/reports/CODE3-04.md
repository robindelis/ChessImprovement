# CODE3-04 — Two different sync streams share one `syncAbortController` slot; the `finally` of one nulls the other's handle, silently disarming the Cancel button
- **Severity:** high
- **Type:** race
- **File:** frontend/repertoire_builder.js
- **Line(s):** 590-613 (`cancelSync`), 660-663, 776-782 (`resyncGames`), 1107-1110, 1234-1249 (`syncRecentGames`)
- **Component:** Repertoire Builder — Lichess sync (SSE)
- **Repro script:** `audit/scripts/CODE3-04_sync_abort_race.js`
- **Detected by:** CODE-3

## Expected
`cancelSync()` (bound to the persistent `✕` button, `index.html:115`, which lives **outside** the progress card and is
always clickable) aborts whatever sync is running and stops all progress writes.

## Actual
`resyncGames()` (full re-scan, reachable from the Electron app menu via
`app.js:42-48 → desktopApp.onSyncGames`) and `syncRecentGames()` (Recent-Games "🔄 Sync Latest") both assign to and
clear the **same** slot, and both clear it unconditionally in `finally`:

```js
// resyncGames, line 660
if (this.syncAbortController) { this.syncAbortController.abort(); }
this.syncAbortController = new AbortController();
...
} finally {                                     // line 776
    if (btnResync) { ... }
    this.syncAbortController = null;             // <-- unconditional
}
```

```js
// syncRecentGames, line 1107 — identical pattern
...
} finally {                                     // line 1234
    if (btnSync) { ... }
    this.syncAbortController = null;             // <-- unconditional, clobbers the other stream
    setTimeout(() => { const el = document.getElementById('repGamesInlineProgress'); if (el) el.remove(); }, 2500);
    setTimeout(() => { const c = document.getElementById('syncProgressContainer');
                        if (c && !this.syncAbortController) { c.style.display = 'none'; } }, 3500);
}
```

Interleaving that breaks cancel:

1. `t0` user clicks "🔄 Sync Latest" → `syncAbortController = A`, stream A reading.
2. `t1` user triggers a full resync (Electron menu "Sync Games") → `resyncGames()` aborts A, assigns `B`.
3. `t2` A's `reader.read()` rejects → `catch` sees `err.name === 'AbortError'` and `return`s — **but the `finally`
   still runs** → `this.syncAbortController = null`, destroying B's handle.
4. `t3` user clicks the `✕` button → `cancelSync()` runs
   `if (this.syncAbortController) { this.syncAbortController.abort(); }` → it is `null`, so **nothing is aborted**;
   `cancelSync` still hides `syncProgressContainer`, so the UI looks cancelled while the full
   `/api/repertoire/sync_games_stream` keeps streaming, keeps writing `statusTitle`/`progressBar`/`syncCountProcessed`
   into the hidden card, and keeps writing games/blunders into the database.
5. The 3500 ms `setTimeout` guard `if (c && !this.syncAbortController)` is likewise defeated: because the field is
   `null`, the card is hidden even though a stream is live.

## Root cause
A single mutable field is used as the cancel handle for two independent, concurrently-startable streams, and it is
cleared in `finally` without an identity check. Compare with the correctly-written sibling `startEvalStream()` (1731),
which does guard its teardown: `if (this.currentEvalAbort === abortCtrl) { this.currentEvalAbort = null; }` (1793).

## Exploit / escalation
1. Rapid double-click "🔄 Sync Latest": the second call aborts the first, but the first's `finally` then nulls the
   second's handle — the second stream is un-cancellable and its `setTimeout`s race the first's.
2. The 8 s auto-dismiss in `resyncGames` (761-766) uses the same `!this.syncAbortController` guard, so it can hide the
   card of a *different* still-running stream.
3. `cancelSync()` also references two dead elements (`btnResyncGames`, `repGamesInlineProgress` — the latter exists
   only after `syncRecentGames` inserted it), so the "reset the button" half of the cancel path is partly no-op.
