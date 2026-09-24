# CODE-18 — `promptStudySelection` has no re-entrancy guard; a double-click throws and silently aborts the import
- **Severity:** medium
- **Type:** crash
- **File:** frontend/app.js
- **Line(s):** 190-431 (esp. 419-421), 414-416; callers at frontend/scout.js:1625-1631 and 1708-1714
- **Component:** window.promptStudySelection
- **Repro script:** `audit/scripts/CODE4_prompt_reentrancy_repro.js`
- **Detected by:** CODE-4

## Expected
Opening a `<dialog>` that is already open must be a no-op (or queued), and a
second concurrent request must reuse the in-flight promise. Every caller must
receive a resolved value so the awaited import flow always completes or aborts
visibly.

## Actual
```js
// frontend/app.js:414-421
if (btnCancel) btnCancel.onclick = handleCancel;
if (btnClose) btnClose.onclick = handleCancel;
if (btnConfirm) btnConfirm.onclick = handleConfirm;
modal.onclose = () => { cleanup(); resolve(null); };

const initialTab = (mode === 'new' || (mode === 'auto' && defaultName)) ? 'new' : 'existing';
setTab(initialTab);
modal.showModal();          // <-- throws InvalidStateError if already open
```

`modal.showModal()` throws `InvalidStateError` when the dialog already has an
open modal. The call sits inside the `new Promise(...)` executor, so the
promise **rejects**. There is:

* no `if (modal.open) { ... }` guard;
* no module-level "pending promise" cache, so a second caller gets a second
  promise and a second `showModal()`;
* no `.catch()` at the call sites.

Callers `await` the result and then early-`return` on `null`:

```js
// frontend/scout.js:1622-1638
let studyName = line?.studyName || this.currentStudy;
if (!studyName) {
    const defaultStudy = (...).replace(/[\(\)\[\]]/g, '').trim();
    studyName = (typeof window.promptStudySelection === 'function')
        ? await window.promptStudySelection({ repertoire, defaultName: defaultStudy, title: `...` })
        : defaultStudy;
    if (!studyName) return;      // user cancelled
```

so a rejected promise propagates out of `importToRepertoire`, past its
`try/catch` (which starts at line 1651, *after* this await), and becomes an
unhandled rejection with **no toast, no console message and no state change** —
the button appears completely inert.

Additionally the study-creation POST inside the modal ignores its response:

```js
// frontend/app.js:362-371
try {
    ...
    await fetch('/api/repertoire/studies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: chosenName, repertoire: repertoire })
    });
} catch (e) {
    console.error('Error creating study:', e);
}
```

`res.ok` is never checked, so a 4xx/duplicate-name rejection still resolves the
promise with the name and the line is imported against a study that does not
exist.

## Root cause
The modal helper is written as a fire-and-forget DOM mutator with no state
machine, no singleton-promise cache and no response validation.

## Exploit / escalation
1. **Double-click any import button.** `📥 Line → White`, `📥 Line → Black`,
   `📥 All → White`, `📥 All → Black`, `#btnScoutAddMoveWhite/Black` and
   `#btnScoutNewStudy` are all `async` handlers with no in-flight guard and are
   never disabled. With no `this.currentStudy` set, the first click opens the
   modal; the second click within the same tick calls `showModal()` on the open
   dialog and rejects.
2. **`Esc`-then-retry.** `handleCancel` → `cleanup()` → `modal.close()` →
   `modal.onclose` is already nulled by `cleanup()`, so the promise resolves
   `null` once via `handleCancel`. If the user then re-triggers the import, a
   *new* promise is created and `showModal()` succeeds — but the previous
   invocation's `cleanup()` has already nulled `btnConfirm.onclick`, so the
   freshly-installed handler is the only one and the flow works. The dangerous
   window is specifically "two clicks before the first modal settles".
3. Adjacent weaker variant: the async `fetch('/api/repertoire/studies')` at
   `app.js:259-316` has no `AbortController`; if it resolves after the modal was
   cancelled, it still calls `setTab(...)` and rewrites `#studySelectDropdown`
   (`innerHTML = '-- No Studies Found --'` on error, line 314) on a dialog that
   is closed, and can flip the tab back to "new" behind the user's back.
