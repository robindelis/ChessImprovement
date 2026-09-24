# FIXER-E-01 — Electron shell hardening (CODE-05-09)

- **Fixer:** FIXER-E
- **Finding:** `audit/reports/CODE-05-09.md` (HIGH, security)
- **Files owned & changed:** `electron/main.js`, `electron/preload.js`, `package.json`
- **Files added:** `tests/test_electron_shell.py`, `audit/scripts/FIXER-E-01_*`
- **Full suite:** `Ran 270 tests ... FAILED (failures=3)` — all 52 new Electron tests pass;
  the 3 failures are in `test_repertoire.py` / `test_studies.py`, caused by another agent's
  in-flight `backend/` edits (proof in §9).

---

## 0. Claim verification — every element of CODE-05-09, checked before changing anything

| # | Claim | Verdict | Evidence |
|---|-------|---------|----------|
| 1 | No `setWindowOpenHandler` | **CONFIRMED** | `grep -c setWindowOpenHandler electron/main.js` → `0` |
| 2 | No `will-navigate` guard | **CONFIRMED** | `grep -c will-navigate electron/main.js` → `0` |
| 3 | No CSP anywhere | **CONFIRMED** | `grep -rn "Content-Security-Policy\|http-equiv" frontend/ backend/ electron/` → no output |
| 4 | Renderer loads a remote script from YouTube | **CONFIRMED** | `frontend/index.html:921` `<script src="https://www.youtube.com/iframe_api">` |
| 5 | `contextIsolation:true`, `nodeIntegration:false`, `sandbox:true` already correct | **CONFIRMED** | was `electron/main.js:226-231` |
| 6 | Four `target=_blank` links exist | **CONFIRMED** | `index.html:649`, `index.html:805`, `repertoire_builder.js:1013`, `repertoire_builder.js:1366` |
| 7 | Preload surface is small but non-empty | **CONFIRMED** | old `preload.js:4-13`: `minimize`/`maximize`/`close`/`onSyncGames` |
| 8 | `ipcMain.on` registered inside `createWindow()` (duplicate on macOS `activate`) | **CONFIRMED** | old `main.js:249-260` vs `activate` at `:305-309` |
| 9 | `resolveFreePort()` TOCTOU | **CONFIRMED** | probe socket closed at `main.js:31`, bind happens later in `startPythonBackend()` |
| 10 | `isServerRunning()` adopts any 200–399 on `/api/dashboard` | **CONFIRMED** | old `main.js:61-72` |
| 11 | Child not in its own process group → `SIGINT` not guaranteed to reach Stockfish | **CONFIRMED** | `spawn()` had no `detached`; Stockfish is `chess.engine.popen_uci`'s child (`backend/evaluator.py:70`) |
| 12 | No `session.setPermissionRequestHandler` | **CONFIRMED** | `grep -c "session.setPermission"` → `0` |
| 13 | `remote` module unused | **CONFIRMED (absent)** | no `@electron/remote`, no `enableRemoteModule` anywhere |

**One correction to the report:** CODE-05-09 claims the app "should not need real
navigation — confirm that." Confirmed. `grep` for `location.href =`, `location =`,
`window.open(`, `history.pushState` across all 5 frontend `.js` files: **zero hits**.
The SPA switches tabs by toggling CSS classes (`frontend/app.js:51-90`). So blocking
real navigation breaks nothing.

---

## 1. Navigation control

`electron/main.js:407-424` (in `createWindow`):

```javascript
    const contents = mainWindow.webContents;

    // The app is a single page that switches tabs with CSS classes - it never
    // needs a real navigation. Anything that is not our own backend origin
    // (including http://127.0.0.1:<other port>) is refused.
    const blockForeignNavigation = (event, url) => {
        if (!isTrustedAppUrl(url)) {
            event.preventDefault();
            console.warn(`[Desktop] Blocked navigation to ${url}`);
        }
    };
    contents.on('will-navigate', blockForeignNavigation);
    contents.on('will-redirect', blockForeignNavigation);
    contents.on('will-attach-webview', (event) => {
        event.preventDefault();
        console.warn('[Desktop] Blocked <webview> attachment');
    });
```

The trust anchor (`electron/main.js:82-88`) is **exact origin**, not "looks local":

```javascript
function isTrustedAppUrl(rawUrl) {
    try {
        return new URL(String(rawUrl)).origin === SERVER_URL;
    } catch (e) {
        return false;
    }
}
```

`SERVER_URL` is always `http://127.0.0.1:<port>` — a hardcoded loopback literal, so
`localhost`, a different port, `https://`, `file://` and `…@evil.example` userinfo
tricks are all rejected. `will-redirect` is covered too (a same-origin page could
otherwise be bounced elsewhere), and `will-attach-webview` closes the `<webview>`
tag even though `webviewTag: false` already disables it.

**Runtime proof** (`audit/scripts/FIXER-E-01_final_smoke.sh`, real app on port 39281):
the renderer was told `location.href = "https://evil.example/"` over CDP. Result:

```
--- navigation guard: attempt to navigate the window to an external origin
    navigating renderer to https://evil.example/
    renderer location after the attempt: http://127.0.0.1:39281/
--- [Desktop] blocked-navigation warnings:
    [Desktop] Blocked navigation to https://evil.example/
```

The main process logged the block and the window stayed on the app.

---

## 2. `setWindowOpenHandler` — deny by default, allow-list → system browser

`electron/main.js:427-432`:

```javascript
    // Deny every window.open()/target=_blank: a second BrowserWindow would
    // inherit this preload. Allow-listed https links go to the system browser.
    contents.setWindowOpenHandler(({ url }) => {
        openExternalUrl(url);
        return { action: 'deny' };
    });
```

`{ action: 'deny' }` is **unconditional** — no Electron window is ever created, so
the preload can never be attached to a foreign origin (this closes CODE-05-09
exploit variant #2). The four existing `target=_blank` links keep working because
`openExternalUrl` hands allow-listed ones to the OS browser.

### Every `openExternal` call site, audited

There is exactly **one** in the whole repo, and it is gated:

`electron/main.js:108-117`:
```javascript
function openExternalUrl(rawUrl) {
    const safe = resolveExternalUrl(rawUrl);
    if (!safe) {
        console.warn(`[Desktop] Blocked external open (not allow-listed): ${rawUrl}`);
        return false;
    }
    shell.openExternal(safe).catch((err) => {
        console.error('[Desktop] shell.openExternal failed:', err);
    });
    return true;
}
```

`shell.openExternal(safe)` — the **normalised** value, never the raw input. The
validator (`electron/main.js:93-106`):

```javascript
function resolveExternalUrl(rawUrl) {
    let parsed;
    try {
        parsed = new URL(String(rawUrl).trim());
    } catch (e) {
        return null;
    }
    if (parsed.protocol !== 'https:') return null;
    if (parsed.username || parsed.password) return null;
    const host = parsed.hostname.toLowerCase().replace(/\.$/, '');
    if (!EXTERNAL_HOST_ALLOWLIST.has(host)) return null;
    return parsed.toString();
}
```

Rejected: `file:`, `javascript:`, `data:`, `vbscript:`, `about:`, plain `http:`,
embedded credentials, protocol-relative `//host`, empty/malformed strings, and every
non-allow-listed host. The trailing-dot strip plus `Set.has` (not `endsWith`/`includes`)
defeats `lichess.org.evil.example` and `lichess.org.`.

Allow-list hosts, all of which the app genuinely links to
(`backend/api.py:394` + `backend/repertoire.py:1399` build `https://lichess.org/<id>`;
`frontend/scout.js:558` builds `https://www.youtube.com/watch?v=<id>`):

```
lichess.org, www.lichess.org, youtube.com, www.youtube.com, m.youtube.com,
youtu.be, www.youtu.be, youtube-nocookie.com, www.youtube-nocookie.com
```

**Runtime proof** — `audit/scripts/FIXER-E-01_mainjs_probe.py` loads the real
`electron/main.js` in Node with a stubbed `electron` module (no window, no process,
no DB) and calls the real `resolveExternalUrl` / `isTrustedAppUrl`:

```
  [PASS] allow -> allow  'https://lichess.org/gVf9kL3M'
  [PASS] allow -> allow  'https://lichess.org/account/oauth/token'
  [PASS] allow -> allow  'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
  [PASS] allow -> allow  'https://youtu.be/dQw4w9WgXcQ'
  [PASS] allow -> allow  'https://www.youtube-nocookie.com/embed/abc'
  [PASS] deny  -> deny   'http://lichess.org/gVf9kL3M'          # not https
  [PASS] deny  -> deny   'https://evil.example/steal'
  [PASS] deny  -> deny   'https://lichess.org.evil.example/'   # suffix confusion
  [PASS] deny  -> deny   'https://LICHESS.org.evil.example/'   # case + suffix
  [PASS] deny  -> deny   'file:///etc/passwd'
  [PASS] deny  -> deny   'file:///home/robin/.bashrc'
  [PASS] deny  -> deny   'javascript:alert(1)'
  [PASS] deny  -> deny   'JavaScript:alert(1)'
  [PASS] deny  -> deny   'data:text/html,<script>alert(1)</script>'
  [PASS] deny  -> deny   'vbscript:msgbox(1)'
  [PASS] deny  -> deny   'about:blank'
  [PASS] deny  -> deny   'https://user:pass@lichess.org/'      # embedded creds
  [PASS] deny  -> deny   ''
  [PASS] deny  -> deny   'not a url at all'
  [PASS] deny  -> deny   '//lichess.org/protocol-relative'
  [PASS] deny  -> deny   'https://localhost:38281/'            # own backend is not "external"
0 failing case(s)
```

---

## 3. CSP

### Where it is applied, and why I chose the response header

**Chosen: response header, injected by the Electron main process** —
`electron/main.js:293-313` (`applySessionHardening`):

```javascript
    ses.webRequest.onHeadersReceived((details, callback) => {
        const responseHeaders = details.responseHeaders || {};
        if (details.resourceType === 'mainFrame' && isTrustedAppUrl(details.url)) {
            for (const key of Object.keys(responseHeaders)) {
                if (key.toLowerCase() === 'content-security-policy') delete responseHeaders[key];
            }
            responseHeaders['Content-Security-Policy'] = [APP_CSP];
        }
        callback({ responseHeaders });
    });
```

Reasons, in order of weight:

1. **`frame-ancestors` is ignored in a `<meta>` tag.** The CSP spec (and Chromium)
   silently drop `frame-ancestors`, `report-uri`, `sandbox` and `upgrade-insecure-requests`
   when delivered via `<meta>`. A meta-only policy could not express the anti-clickjacking
   directive at all.
2. **It applies to exactly the document the shell is responsible for** and is scoped by
   `resourceType === 'mainFrame'` + the exact-origin check, so it cannot leak onto
   subresources or onto a foreign origin.
3. It is **owned by my file**. `frontend/index.html` and `backend/api.py` belong to other
   agents, and the task explicitly asks me to avoid editing them.
4. It is testable: the string lives in one JS constant that both the shell and the test
   suite read, so header and tests cannot drift apart.

### The exact string

```
default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; script-src 'self' https://www.youtube.com; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://i.ytimg.com; font-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-src https://www.youtube.com
```

### Why each directive is exactly this — the minimum-viable argument

Derived by grepping the real frontend, not guessed:

| Directive | Value | Justification (measured) |
|---|---|---|
| `script-src` | `'self' https://www.youtube.com` | The only remote script is `index.html:921` `youtube.com/iframe_api`. The IFrame API then bootstraps `https://www.youtube.com/s/player/<hash>/www-widgetapi.js` — **same host**, so one entry covers both. All other scripts are same-origin (`chess.min.js`, `audio.js`, `board.js`, `repertoire_builder.js`, `repertoire_trainer.js`, `scout.js`, `app.js`). **No `unsafe-inline`, no `unsafe-eval`, no `wasm-unsafe-eval`** — verified: the app uses no `eval(`, no `new Function`, no `document.write`. |
| `style-src` | `'self' 'unsafe-inline'` | **Required, not laziness.** `grep -o 'style="' frontend/index.html \| wc -l` → **257**; same grep over the 5 JS files → **103** more in template literals; plus 146 `.style.` assignments. Removing this breaks nearly all layout. It is scoped to *styles only* and does **not** weaken `script-src` — proven below. |
| `frame-src` | `https://www.youtube.com` | The YouTube embed is the only frame. Confirmed live: the API produced `https://www.youtube.com/embed/dQw4w9WgXcQ?autoplay=0&enablejsapi=1&rel=0&modestbranding=1`. |
| `connect-src` | `'self'` | Every `fetch()`/stream in the app is a **relative** path (`/api/...`). Grep for absolute `http(s)://` across all frontend JS returns only `youtube.com` and the SVG namespace `www.w3.org`. No third-party XHR/WebSocket/EventSource. |
| `img-src` | `'self' data: blob: https://i.ytimg.com` | Pieces are local `/assets/pieces/*.svg`; thumbnails are `https://i.ytimg.com/vi/<id>/hqdefault.jpg` (`backend/scout.py:134,210`). `data:`/`blob:` for generated assets. **Host-pinned** to the one CDN actually used, not `https:`. |
| `default-src` | `'self'` | Safety net for anything unenumerated (workers, manifests). Never a wildcard. |
| `base-uri`, `object-src`, `form-action` | `'none'` | No `<base>`, no `<object>`/`<embed>`, and both forms `preventDefault()` (`app.js:25`, `scout.js:210-211`) so no legitimate navigation ever needs `form-action`. |
| `frame-ancestors` | `'none'` | The app window is never embedded. |
| `font-src` / `media-src` | `'self' data:` / `'self' blob:` | No `@font-face`/`url()` in `style.css`; no `<audio>`/`<video>`/WebAudio src elements (Web Audio is synthesised in `audio.js`, not loaded). |

### Proof the policy is enforced AND that the app still works

`audit/scripts/FIXER-E-01_csp_probe.js` runs the **real `frontend/index.html`** and a
purpose-built injection fixture through the **real `applySessionHardening` logic** in a
real Electron renderer.

Control run first, to prove the harness can detect a bad policy: with a deliberately
over-strict `default-src 'self'` the harness reported 40+ violations and
`YT=undefined`. Then with the shipped `APP_CSP`:

```
=== REAL APP under APP_CSP ===
{ "styleAttrApplied": "700",        <- style="" still applies
  "navTabsPresent": 4,
  "svgBoardPresent": true,          <- board.js ran
  "ytApiLoaded": true,              <- remote YouTube script still allowed
  "ytPlayerCtor": true,
  "nodeGlobalsAbsent": true,
  "desktopBridgeKeys": null }       <- no preload in this harness window

=== ENFORCEMENT FIXTURE (main world) ===
{ "evalBlocked": true,
  "functionCtorBlocked": true,
  "dynamicInlineScriptBlocked": true,
  "inlineHandlerBlocked": true,
  "jsUrlBlocked": true,
  "crossOriginFrameBlocked": true }

=== YOUTUBE EMBED UNDER CSP (mirrors scout.js:636) ===
{ "ytApiLoaded": true, "playerInstantiated": true,
  "embedIframeCreated": true,
  "embedIframeSrc": "https://www.youtube.com/embed/dQw4w9WgXcQ?autoplay=0&enablejsapi=1&rel=0&modestbranding=1&",
  "playerHasSeekApi": true }

=== CSP violations observed: 6 ===   (all 6 are the fixture's own attack attempts)
  - Executing inline script violates ... 'script-src 'self' https://www.youtube.com'
  - Running the JavaScript URL violates ...
  - Loading the stylesheet 'https://example.com/x.css' violates ... "style-src 'self' 'unsafe-inline'"
  - Loading the image 'https://example.com/pixel.png' violates ... "img-src ... https://i.ytimg.com"
  - Framing 'https://example.com/' violates ... "frame-src https://www.youtube.com"
  - Executing inline event handler violates ... 'script-src ...'
```

Every violation is an attack attempt being stopped. The Scout video feature is
**unaffected**: the embed iframe is created and `seekTo` is available, which is what
`frontend/scout.js:660-664` needs.

The same checks against the **real running app** (`FIXER-E-01_final_smoke.sh`, port 39281):

```
{ "currentUrl": "http://127.0.0.1:39281/",
  "title": "Chess Repertoire Studio",
  "metaCspPresent": false,           <- header-delivered, not meta
  "desktopAppKeys": ["isDesktop","onSyncGames"],
  "nodeGlobalsAbsent": true,
  "evalBlocked": true,
  "dynamicInlineScriptBlocked": true,
  "styleAttrWorks": "700",
  "navTabs": 4, "boardRendered": true,
  "fetchWorks": true }               <- /api/settings still reachable
```

---

## 4. Renderer isolation

`electron/main.js:396-404`:

```javascript
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            nodeIntegration: false,
            contextIsolation: true,
            sandbox: true,
            webSecurity: true,
            allowRunningInsecureContent: false,
            webviewTag: false
        }
```

Verified **at runtime, not just in source** — the renderer reports
`require`/`process`/`module` all `undefined`, in both the harness and the real app
(`"nodeGlobalsAbsent": true`). No `@electron/remote` anywhere in the repo.

### Preload surface: minimised from 7 keys to 2

Old (`preload.js:4-13`, pre-patch) exposed `isDesktop, platform, version, minimize, maximize,
close, onSyncGames`. New (`electron/preload.js:14-27`):

```javascript
contextBridge.exposeInMainWorld('desktopApp', {
    isDesktop: true,
    onSyncGames: (callback) => {
        if (typeof callback !== 'function') return () => {};
        const listener = () => {
            try {
                callback();
            } catch (err) {
                console.error('[desktopApp] onSyncGames handler failed:', err);
            }
        };
        ipcRenderer.on('menu-sync-games', listener);
        return () => ipcRenderer.removeListener('menu-sync-games', listener);
    }
});
```

**Removed and why:**
- `minimize` / `maximize` / `close` — **dead code**. Grep across the whole repo:
  `desktopApp.minimize|maximize|close` → **0 call sites**. They granted any script in
  the page control of the app window. The matching `ipcMain.on('window-minimize'…)`
  handlers are deleted from `main.js` too, so the channels no longer exist at all.
- `platform` / `version` — **dead code** (0 references). `process.platform` inside a
  sandboxed preload is also one step closer to leaking Node surface for no benefit.

**Kept and why:** `isDesktop` (a boolean; used for feature detection) and `onSyncGames`
— the only bridge function with a real caller, `frontend/app.js:42-48`, wired to the
native menu item "Refresh / Sync Games" (`main.js` File menu).

`onSyncGames` was also made safe to call repeatedly: it now returns an unsubscribe
function (the old version leaked one `ipcRenderer` listener per call and could not be
torn down), and it ignores a non-function argument instead of throwing.

The renderer can reach **no** Node primitive: no `require`, no `ipcRenderer` object, no
`process`, no filesystem, no `child_process`. The bridge passes only a boolean and a
function.

---

## 5. Remote script (YouTube IFrame API)

**Is it avoidable? Not for the Scout feature as built — but it is now bounded.**

- `frontend/index.html:921` loads `https://www.youtube.com/iframe_api`; `scout.js:636`
  uses `YT.Player` for chapter-synced seeking (`seekVideoTo`, `scout.js:660`). Replacing
  it means rewriting the player integration. It is **not** carrying any secret: no token,
  key, or credential is passed to it (grep: the only `?v=`/`videoId` values are YouTube
  video IDs), and the CSP confines it to `script-src`/`frame-src` on that one host.
- **The `allow-*` and `sandbox` items are NOT fixable from my side** — the iframe is
  created by Google's own API inside the page; I cannot attach attributes to it from the
  main process. See hand-off §8.
- **`youtube-nocookie.com` is a viable and worthwhile alternative.** It is already in
  my `openExternal` allow-list, and it is a drop-in change
  (`https://www.youtube-nocookie.com/embed/<id>?enablejsapi=1` supports the same
  `postMessage` API). Benefits: no persistent YouTube cookie in the app's Electron
  profile, and it blocks the ad/personalisation script that the `youtube.com` embed
  injects. Caveat worth stating plainly: it is a **privacy** improvement, not a
  **trust** improvement — the remote script still runs with the app's privileges, so it
  does not mitigate the CODE-05-09 exploit chain. A **self-hosted** copy of the API is
  not viable: the script bootstraps `www-widgetapi.js` from `www.youtube.com` at
  runtime, so it cannot be vendored without patching Google's loader.

---

## 6. Python child process — no orphans on any shutdown path

### 6a. Why orphans were possible, and the fix

Three root causes, all fixed:

1. **Not in its own process group.** `spawn()` had no `detached`, so `SIGINT` went only
   to the direct child. Stockfish is spawned by `python-chess`
   (`backend/evaluator.py:70 popen_uci`), so a grandchild could survive.
   → `main.js:221-225` now spawns `detached: true`.
2. **Signalling only the pid.** → `main.js:255-273` `signalProcessTree()` signals
   `-pid` (the whole group), falling back to the single pid.
3. **`SIGINT`/`SIGTERM` handlers called `process.exit(0)` synchronously**, so the
   1200 ms `SIGKILL` escalation timer never got a chance to run.
   → `main.js:523-533` defers the exit by `BACKEND_STOP_GRACE_MS + 200`.

Plus a **correctness fix I found in my own first draft**: `signalProcessTree` initially
signalled `-pid` unconditionally. After a lost bind race the child is reaped, its pid can
be **recycled** by an unrelated process, and a group signal would then hit that stranger.
`main.js:257` now refuses to signal a reaped child:

```javascript
    if (proc.exitCode !== null || proc.signalCode !== null) return;
```

`stopPythonBackend()` is also now idempotent (`backendStopRequested`, `main.js:273-291`),
so the three exit hooks that all fire on quit don't triple-signal.

### 6b. `ps` evidence — all four paths

Harness: `audit/scripts/FIXER-E-01_process_lifecycle.sh`. Runs the **real app** on port
**39281** (the user's own server on 38281 is never touched), never clicks anything in the
UI, so no sync/seed/clear occurs. `ps` filtered to this app's electron main + its python
child + **that child's own stockfish**.

```
########## snapshot: 00-BEFORE-LAUNCH
--- our python backend (port 39281) + its stockfish children:
--- verdict: python=0 electron=0 stockfish=0

########## PATH 1: SIGINT to the Electron main process (Ctrl+C equivalent)
electron main=557141  python=[557197]
########## snapshot: 01-RUNNING-path1
 557141  557109  557109  557109  /…/electron/dist/electron --remote-debugging-por
 557197  557141  557197  557197  /…/python3 /…/start.py --port 39281     <- own PGID
 557349  557197              /…/stockfish-linux-x86-64-universal          <- grandchild
--- verdict: python=1 electron=1 stockfish=1
########## snapshot: 02-AFTER-SIGINT
--- verdict: python=0 electron=0 stockfish=0                            <- CLEAN

########## PATH 2: SIGTERM to the Electron main process
electron main=558412  python=[558503]
########## snapshot: 03-RUNNING-path2
 558412 …  558503  558412  558503  …start.py --port 39281
 558649  558503            …stockfish-linux-x86-64-universal
--- verdict: python=1 electron=1 stockfish=1
########## snapshot: 04-AFTER-SIGTERM
--- verdict: python=0 electron=0 stockfish=0                            <- CLEAN

########## PATH 3: real window close (close -> closed -> window-all-closed)
electron main=559458  python=[559515]
########## snapshot: 05-RUNNING-path3
 559458 …  559515  559458  559515  …start.py --port 39281
 559656  559515            …stockfish-linux-x86-64-universal
--- verdict: python=1 electron=1 stockfish=1
  [CDP] target: http://127.0.0.1:39281/   window.close() dispatched
########## snapshot: 06-AFTER-WINDOW-CLOSE
--- electron still alive? []
--- orphan python: []
                                                    <- CLEAN
```

Path 3 needed a fix to my own harness first: my first CDP client used the `ws` package
API (`.on`), but Node's built-in global `WebSocket` is EventTarget-based
(`addEventListener`) — the close was silently never dispatched, and the "PASS" was
meaningless. Rewritten (`audit/scripts/FIXER-E-01_close_window.js`); the app log then
showed the genuine `close → window-all-closed` path firing
(`[Desktop] Stopping Python backend process group (pid 570369)…`).

### 6c. Single-instance lock

```
########## PATH 4: second launch while the first is running
first instance: electron=561322 python=[561378 ]
########## snapshot: 07-FIRST-INSTANCE-RUNNING
 561322 …  561378  561322  561378  …start.py --port 39281
 561527  561378            …stockfish-linux-x86-64-universal
--- launching a second instance
[Desktop] Another instance is already running. Quitting.
second-instance-exit=0
python after 2nd launch: [561378 ]  (must equal [561378 ])
########## snapshot: 08-AFTER-SECOND-LAUNCH
--- verdict: python=1 electron=1 stockfish=1
########## snapshot: 09-AFTER-FIRST-QUIT
--- verdict: python=0 electron=0 stockfish=0
########## FINAL ORPHAN CHECK (port 39281)
RESULT: PASS - no orphaned 'start.py --port 39281' process remains.
```

The second launch exits immediately, does **not** spawn a second backend, and the first
window keeps its single backend. `requestSingleInstanceLock()` is called at module top
level (before `whenReady`) and the `second-instance` handler focuses/restores the
existing window. Correct as-is; unchanged.

### 6d. Port detection — TOCTOU and identity

**Identity (worse than CODE-05-09 stated).** The old `isServerRunning()` accepted *any*
200–399 on `/api/dashboard` and adopted it as the app's backend. Replaced by
`probeBackend()` (`main.js:158-192`), which requires the FastAPI OpenAPI identity title:

```javascript
                    const info = JSON.parse(body).info || {};
                    done(info.title === BACKEND_IDENTITY_TITLE);
```

Verified: `TestClient(app).get('/openapi.json')` → `{'title': 'Chess Opening Improvement
Platform', 'version': '0.1.0'}`. An unrelated process cannot accidentally claim the app
unless it deliberately serves that exact title.

**TOCTOU.** `resolveFreePort()` closes its probe socket before uvicorn binds, so two
launches can pick the same port. The window is now *closed* rather than *removed*:
`main.js:464-489` retries up to `BACKEND_START_ATTEMPTS` (5) on the next port, and
`waitForServer` fails fast when the child dies instead of burning the full 25 s timeout.

Proof (`audit/scripts/FIXER-E-01_backend_retry.sh`) — `PYTHON_PATH` points at a wrapper
that fails its first invocation, simulating "the port was taken between probe and bind":

```
[Desktop] Starting Python backend with: /tmp/…/flaky-python.sh /…/start.py --port 39321
[Desktop] Python process exited with code 1, signal null
[Desktop] Backend attempt 1/5 on port 39321 failed, retrying next port.
[Desktop] Preferred port 39321 was occupied, using port 39322
[Desktop] Starting Python backend with: /tmp/…/flaky-python.sh /…/start.py --port 39322
[Desktop] Backend is online. Opening application window.
RESULT: PASS - app recovered by retrying on the next port.
```

Before the fix this produced the `Server Startup Timeout` dialog and a dead app.

A 4-way simultaneous race (`audit/scripts/FIXER-E-01_port_race.sh`, lock bypassed with
per-launch `--user-data-dir`): `online=4 failed=0`, one shared backend, and after
cleanup `count=0` — no orphans from the losers.

---

## 7. `package.json`

```
  "main": "electron/main.js",     <- correct entry
  "private": true,                <- ADDED
```

- **`main`** already correct — unchanged.
- **No dangerous scripts.** `start`/`desktop` = `electron .`; `server`/`test` run the
  conda python; `install-shortcut` runs `./install-desktop.sh`. No `preinstall`,
  `install`, `postinstall`, `prepare` or `prepublish` hook (any of these is arbitrary
  code execution on `npm install`). No `&&`/`;`/`|`/`` ` ``/`$(` chaining. No script
  references `chess_improvement.db`, `/seed` or `/clear`.
- **`"private": true` added** — the package was publishable, which would upload
  `chess_improvement.db` (the user's real games and repertoire) to a registry. Cheap,
  zero runtime impact.
- **`start` matches `AGENTS.md` §2**: `npm start` → `electron .` → desktop app, same as
  `./run-desktop.sh`. The hardcoded `/home/robin/miniconda3/bin/python3` in `server`/`test`
  matches the paths §2 documents, so left alone.

---

## 8. Hand-off — changes other owners must apply

### `backend/api.py` owner — **nothing required**

The CSP is delivered by the Electron shell, so `backend/api.py` needs no change. If you
*want* the policy to also apply when the app is opened in a plain browser
(`python3 start.py` + browser, which `AGENTS.md` §2 documents as a supported mode), add a
header in the existing `no-cache` middleware at `backend/api.py:60-71`:

```python
response.headers["Content-Security-Policy"] = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; "
    "frame-ancestors 'none'; form-action 'none'; "
    "script-src 'self' https://www.youtube.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https://i.ytimg.com; "
    "font-src 'self' data:; media-src 'self' blob:; "
    "connect-src 'self'; frame-src https://www.youtube.com"
)
```

Keep it byte-identical to `APP_CSP` in `electron/main.js:66-79` — the Electron shell
deletes any incoming `Content-Security-Policy` before injecting its own, so a divergent
backend copy cannot weaken the desktop build, but divergence is still a maintenance trap.

### `frontend/index.html` owner — **1 required, 2 recommended**

Do **not** add a `<meta http-equiv="Content-Security-Policy">`. A meta tag cannot express
`frame-ancestors` (silently ignored), would be a second copy to keep in sync, and would
break the moment the backend header is added. If you nonetheless want defence-in-depth
for the browser mode, the exact tag is:

```html
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; script-src 'self' https://www.youtube.com; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://i.ytimg.com; font-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-src https://www.youtube.com">
```

**Required — one inline event handler is now blocked.**
`frontend/scout.js:470`:
```javascript
<img src="${thumb}" alt="${item.title}" class="scout-card-thumb" onerror="this.src='/assets/pieces/wN.svg'">
```
The CSP blocks inline event-handler attributes (verified live: *“Executing inline event
handler violates … 'script-src'*”). Degradation is cosmetic — a broken-image icon instead
of the knight fallback — **not** a crash, which is why I did not treat it as a blocker.
This is the **only** inline handler in the entire frontend
(`grep -rnoE '\bon(click|error|load|…)=' frontend/` → 1 hit). Fix by attaching the handler
in JS after `innerHTML`:

```javascript
grid.querySelectorAll('.scout-card img').forEach((img) => {
    img.addEventListener('error', () => { img.src = '/assets/pieces/wN.svg'; }, { once: true });
});
```
Do **not** “fix” this by adding `'unsafe-hashes'` or `'unsafe-inline'` to `script-src` —
that would give the whole page back inline script execution and undo the fix.

**Recommended — `youtube-nocookie`.** Switch `index.html:921` to
`https://www.youtube-nocookie.com/iframe_api` and the embed URL built by
`scout.js:492,558,655` to `https://www.youtube-nocookie.com/embed/<id>` (already in my
`openExternal` allow-list, and `frame-src`/`script-src` would need
`https://www.youtube-nocookie.com` added). Removes YouTube's persistent cookie from the
Electron profile. Privacy win, not a trust win — see §5.

**Recommended — sandbox the embed.** `YT.Player` creates the iframe itself, so this can
only be done by replacing it with a hand-built iframe
(`<iframe src="https://www.youtube-nocookie.com/embed/<id>?enablejsapi=1"
sandbox="allow-scripts allow-same-origin allow-presentation"
allow="accelerometer; autoplay; encrypted-media; picture-in-picture"
referrerpolicy="strict-origin-when-cross-origin">`) plus direct `postMessage` for
`playVideo`/`seekTo`. Today the embed gets no `sandbox` and no restrictive `allow`.

**Also noted, not fixed** (yours, `index.html:805`): the Lichess token link lacks
`rel="noopener noreferrer"`. Moot under the new `setWindowOpenHandler` (nothing opens in
Electron any more), but worth adding for the browser mode.

---

## 9. Tests

**Added `tests/test_electron_shell.py` — 52 tests, all passing.** No existing test was
weakened or deleted.

| Group | Tests | Kind |
|---|---|---|
| `TestRendererIsolation` | 9 | static source assertions |
| `TestNavigationControl` | 4 | static |
| `TestWindowOpenHandler` | 6 | static |
| `TestContentSecurityPolicy` | 10 | static (parses the real `APP_CSP`) |
| `TestBackendLifecycle` | 7 | static |
| `TestPermissionHardening` | 1 | static |
| **`TestExternalUrlAllowListRuntime`** | **4** | **real execution** in Node |
| `TestFrontendContract` | 4 | assertions on read-only frontend files |
| `TestPackageJson` | 5 | JSON assertions |

> **Honesty note, as requested:** the window-options / navigation-guard / CSP-wiring tests
> are **static source checks**, not runtime ones. They parse the shipped source and
> assert its shape. They cannot prove Electron *honours* the flags — only that the flags
> are correct in the file that creates the window. The file's docstring says this too.
> The runtime proof is in the `audit/scripts/FIXER-E-01_*` probes, which are not part of
> `unittest discover` (they need a display and ~2 min each).
>
> The one genuinely *runtime* test is `TestExternalUrlAllowListRuntime`: it loads the
> real `electron/main.js` in Node with a stubbed `electron` module — zero windows, zero
> processes, zero DB — and executes the real `resolveExternalUrl` / `isTrustedAppUrl`
> against 20 hostile URLs and 9 origin cases. `node` is required, so it self-skips.

**Suite status and proof the 3 failures are not mine.** With my `electron/` changes
**stashed**, the suite showed **23 failures** — my 17 new lifecycle/security tests failed
exactly as they should (proving they are meaningful, not tautological) alongside the same
backend failures. With my changes restored:

```
Ran 270 tests in 14.206s
FAILED (failures=3)
FAIL: test_all_games_scan_and_count (test_repertoire.TestRepertoire…)
FAIL: test_multi_study_trainer_mixing (test_studies.TestRepertoireStudies…)
FAIL: test_trainer_drill_from_move_one  (test_studies.TestRepertoireStudies…)
```

All three are in files owned by other agents, fail identically with my Electron changes
reverted, and concern Lichess explorer candidate ordering
(`AssertionError: 'Nf6' not found in ['Bc5']`) — nothing to do with the desktop shell.
`git diff --stat -- electron/ package.json` confirms I touched only my three files.
**Baseline for the orchestrator: `Ran 104 … OK` + 52 new Electron tests, all green.**

---

## 10. Residual risks

1. **The YouTube script still executes with app privileges.** The CSP limits *where* it
   can come from and what it can reach, but `script-src https://www.youtube.com` means a
   compromise of that origin yields script execution in the app's origin. Mitigated by
   the preload being 1 boolean + 1 event subscription with no Node access — the worst an
   attacker gets is triggering `/api/sync`. Not eliminable without dropping the feature.
2. **The backend has no authentication and binds `127.0.0.1`.** Any local process, and
   any web page the user visits, can drive `/api/repertoire/clear`, `/seed?force=true`,
   `/api/settings`. My `connect-src 'self'` and navigation guard protect the *shell*, not
   the *backend*. A real fix is a per-launch bearer token in a header — out of scope here
   (backend owned elsewhere), flagged.
3. **The YouTube embed iframe has no `sandbox` attribute** (see §8). Google creates it;
   only the frontend owner can change that.
4. **`SIGKILL` of Electron, a session logout, or a machine crash still orphans the
   backend.** No user-space fix exists for `SIGKILL`; `detached: true` makes the child a
   session leader, so it survives. A `prctl(PR_SET_PDEATHSIG)` wrapper or a systemd scope
   would be needed. All *graceful* paths are covered and proven.
5. **The stderr of a failed backend is still only piped to the Electron console**, which
   is invisible in a `.desktop` launch. The `Server Startup Timeout` dialog therefore
   still shows generic text. Now mitigated in practice: the retry loop recovers the
   transient cases, and `dialog.showErrorBox` still fires for a genuine failure.
6. **`View → toggleDevTools` remains enabled** in the production menu
   (`electron/main.js:346`). Intentional — it is the app's only debugging affordance —
   but it is a local-only risk and is listed in CODE-05-09.
7. **`run-desktop.sh` hardcodes `/home/robin/miniconda3/bin` onto `PATH`** and does not
   pass `--no-sandbox`/`--disable-gpu`. Machine-specific (noted in `AGENTS.md:48` as the
   general entry point). I did not change it: the file is not in my scope, and altering
   the launcher affects every user's startup.

## Also noticed (not fixed)

- `electron/main.js` still hardcodes `/home/robin/miniconda3/bin/python3` in
  `getPythonPath()` (`main.js:148-159`) — machine-specific, but overridable via
  `PYTHON_PATH`, and `AGENTS.md` §2 documents that exact path as canonical.
- `frontend/scout.js` has 62 `innerHTML` uses; several interpolate backend data
  (`item.title`, `item.channel`, `study.title`) into HTML. The CSP now blocks inline
  script execution there, which converts the worst case from RCE to markup/DOM injection,
  but attribute-breakout in `alt="${item.title}"` is still worth an audit pass by the
  frontend owner. **Not fixed — frontend is not mine.**
- `package.json` `version: "1.0.0"` vs FastAPI's `0.1.0` and the About dialog's
  hardcoded `Version 1.0.0` — cosmetic drift, left alone.

## Files changed

| File | Change |
|---|---|
| `electron/main.js` | +288/−56: CSP, nav guard, window-open handler, allow-list, session hardening, process-group kill, identity probe, port retry, duplicate-IPC removal |
| `electron/preload.js` | +27/−13: bridge reduced 7 keys → 2, unsubscribe support, rationale |
| `package.json` | +1: `"private": true` |
| `tests/test_electron_shell.py` | new, 52 tests |
| `audit/scripts/FIXER-E-01_*` | new, 9 probe/harness scripts (re-runnable, idempotent, no production DB writes) |
