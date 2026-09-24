# CODE-08 — `saveSettings()` reports success unconditionally; the settings alert element is permanently dead
- **Severity:** high
- **Type:** wrong-result
- **File:** frontend/app.js
- **Line(s):** 105-122 (`loadSettings`), 124-157 (`saveSettings`); element at index.html:836
- **Component:** ChessApp settings form
- **Repro script:** `audit/scripts/CODE4_settings_repro.py`
- **Detected by:** CODE-4

## Expected
`saveSettings()` must check `res.ok` (and surface the server's `detail`) before
showing a success toast; a network error must produce a visible failure notice.
The `<div id="settingsAlert">` in the form is the intended in-form channel and
must be used (per `AGENTS.md` §8.1 it should be a floating toast, or the
element should be removed — it must not be dead markup).

## Actual
```js
// frontend/app.js:124-157
async saveSettings() {
    const token = document.getElementById('inputToken')?.value;
    const username = document.getElementById('inputUsername')?.value;
    const geminiKey = document.getElementById('inputGeminiKey')?.value;
    const gap = document.getElementById('inputDefaultGap')?.value;

    const payload = { lichess_username: username };
    if (token) payload.lichess_token = token;
    if (geminiKey) payload.gemini_api_key = geminiKey;

    try {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();          // <-- `data` is never used
        const alertEl = document.getElementById('settingsAlert');
        if (alertEl) {
            alertEl.style.display = 'none';     // <-- only ever hides
        }
        window.showToast('Settings saved successfully!', '#4ade80');
        if (gap && window.repertoireBuilder) {
            window.repertoireBuilder.gapThreshold = parseFloat(gap);
            const sel = document.getElementById('repGapThreshold');
            if (sel) sel.value = gap;
        }
        await this.loadAccount();
    } catch (e) {
        console.error('Error saving settings:', e);   // <-- silent to the user
    }
}
```

Three distinct defects in eleven lines:

* `res.ok` is never inspected — a `400`/`422`/`500` still yields the green
  "Settings saved successfully!" toast.
* `const data = await res.json();` is a dead assignment; the server's `detail`
  is discarded.
* `settingsAlert` is only ever set to `display: 'none'`. No code path in the
  whole codebase ever sets it to `block` or writes text into it, so the element
  at `index.html:836` is unreachable UI:
  ```html
  <div id="settingsAlert" style="font-size:0.9rem; font-weight:600; display:none;"></div>
  ```
* `await res.json()` itself throws for any non-JSON error body (e.g. a proxy
  502 HTML page), landing in the `catch` — which only `console.error`s.

Note the *token* is not leaked by `loadSettings` (it only sets a placeholder,
lines 111-114) — that part is correct. But `saveSettings` never clears
`#inputToken` after a successful save, so the raw `lip_…` secret stays in the
DOM (and is re-submitted on every later save) for the whole session.

## Root cause
The success path was written without an error path. `data` and `res.ok` are
both ignored, and the in-form alert element was wired up in the HTML but never
in the JS.

## Exploit / escalation
1. **Silent credential failure.** Paste a Lichess token, hit Save. If the
   backend rejects it (bad token format, DB error), the user sees
   "Settings saved successfully!" and then presses "Sync Games", which fails
   with a completely unrelated error — the root cause is invisible.
2. **Silent gap-threshold divergence.** `parseFloat(gap)` is applied to the
   in-memory builder even if the POST failed, so the UI select and the builder
   disagree with the server. If `gap` were ever non-numeric,
   `window.repertoireBuilder.gapThreshold` becomes `NaN` and every gap
   computation in `repertoire_builder.js` silently yields nothing.
3. **Stale `showToast` on the wrong screen.** `showTab('settings')`
   (`app.js:79-83`) calls `loadSettings()`; `loadSettings` swallows its own
   errors too (line 119-121), so with the backend down the user sees a
   pre-filled username and empty key fields and believes nothing is configured.
4. Adjacent weaker variant: `#inputDefaultGap` is read but **never sent** in
   `payload` — the "Default Opponent Gap Threshold" is a client-only setting,
   so it silently resets to 5% on every reload while looking saved.
