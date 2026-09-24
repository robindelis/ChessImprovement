// FIXER-E-01 — CSP enforcement verification harness (throwaway).
// Mirrors applySessionHardening() from electron/main.js exactly and runs two
// pages through it:
//   1. the real frontend/index.html  (does the app still work under APP_CSP?)
//   2. a purpose-built fixture       (is APP_CSP actually enforced?)
// Read-only: hidden window, no files written, no backend spawned, and
// chess_improvement.db is never opened.
const { app, BrowserWindow, session } = require('electron');
const path = require('path');
const fs = require('fs');

const MAIN_JS = path.resolve(__dirname, '..', '..', 'electron', 'main.js');
const src = fs.readFileSync(MAIN_JS, 'utf8');
const m = src.match(/const APP_CSP = \[([\s\S]*?)\]\.join\('; '\);/);
if (!m) { console.error('FATAL: APP_CSP not found in electron/main.js'); process.exit(2); }
const APP_CSP = m[1].match(/"([^"]+)"/g).map(s => s.slice(1, -1)).join('; ');
const PORT = process.env.FIXER_E_PORT || '39411';
const ORIGIN = `http://127.0.0.1:${PORT}`;

const violations = [];
const otherErrors = [];
const say = (s) => process.stdout.write(s + '\n');

app.disableHardwareAcceleration();

function attach(wc) {
    wc.on('console-message', (event, ...rest) => {
        const d = (rest[0] && typeof rest[0] === 'object') ? rest[0]
            : { level: rest[0], message: rest[1], lineNumber: rest[2], sourceId: rest[3] };
        const msg = String(d.message || '');
        if (/Content Security Policy|Refused to/i.test(msg)) violations.push(msg);
        else if (d.level === 'error' || d.level === 3) otherErrors.push(msg);
    });
}

async function newWin() {
    const win = new BrowserWindow({
        show: false,
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
            sandbox: true,
            webSecurity: true,
            allowRunningInsecureContent: false
        }
    });
    attach(win.webContents);
    return win;
}

app.whenReady().then(async () => {
    say('ELECTRON_READY');
    const ses = session.defaultSession;
    ses.setPermissionRequestHandler((_c, _p, cb) => cb(false));
    ses.setPermissionCheckHandler(() => false);
    ses.webRequest.onHeadersReceived((details, callback) => {
        const headers = details.responseHeaders || {};
        if (details.resourceType === 'mainFrame' && details.url.startsWith(ORIGIN)) {
            for (const k of Object.keys(headers)) {
                if (k.toLowerCase() === 'content-security-policy') delete headers[k];
            }
            headers['Content-Security-Policy'] = [APP_CSP];
        }
        callback({ responseHeaders: headers });
    });

    // ---------- 1. the real app ----------
    const win = await newWin();
    say('LOADING real app: ' + ORIGIN + '/');
    await win.webContents.loadURL(`${ORIGIN}/`).catch(e => say('load-error: ' + e.message));
    let ytLoaded = false;
    for (let i = 0; i < 12; i++) {
        await new Promise(r => setTimeout(r, 1000));
        const yt = await win.webContents.executeJavaScript('typeof window.YT').catch(() => 'err');
        if (yt === 'object') { ytLoaded = true; say('YT_API_LOADED after ' + (i + 1) + 's'); break; }
        if (i === 11) say('YT_API_NOT_LOADED after 12s (typeof=' + yt + ')');
    }
    const appReport = await win.webContents.executeJavaScript(`(() => {
        const r = {};
        r.styleAttrApplied = getComputedStyle(document.getElementById('repOpeningTitle')).fontWeight;
        r.navTabsPresent = document.querySelectorAll('.nav-tab').length;
        r.svgBoardPresent = !!document.querySelector('svg');
        r.ytApiLoaded = typeof window.YT === 'object' && window.YT !== null;
        r.ytPlayerCtor = !!(window.YT && window.YT.Player);
        r.nodeGlobalsAbsent = typeof window.require === 'undefined' && typeof window.process === 'undefined'
            && typeof window.module === 'undefined' && typeof window.Buffer === 'undefined';
        r.desktopBridgeKeys = window.desktopApp ? Object.keys(window.desktopApp).sort() : null;
        r.hasInlineOnerror = !!document.querySelector('img[onerror]');
        return r;
    })()`).catch(e => ({ error: e.message }));
    say('\n=== REAL APP under APP_CSP ===\n' + JSON.stringify(appReport, null, 2));

    // ---------- 2. enforcement fixture (main world) ----------
    // Same window, fresh navigation: the fixture script runs in the page's MAIN
    // world, which is where an injected payload would actually run.
    say('\nLOADING enforcement fixture...');
    const fxLoad = await win.webContents.loadURL(`${ORIGIN}/__fixer_e_probe.html`)
        .then(() => 'ok').catch(e => 'load-error: ' + e.message);
    say('fixture load: ' + fxLoad);
    await new Promise(r => setTimeout(r, 4000));
    const fxReport = await win.webContents.executeJavaScript(
        'window.__FIXER_E_REPORT__ ? window.__FIXER_E_REPORT__() : {error:"fixture script blocked"}'
    ).catch(e => ({ error: e.message }));
    // The YouTube widget API needs a few seconds to swap the div for its embed
    // <iframe>, so the deferred checks are polled rather than raced.
    let fxDeferred = null;
    for (let i = 0; i < 8; i++) {
        await new Promise(r => setTimeout(r, 2000));
        fxDeferred = await win.webContents.executeJavaScript(
            'window.__FIXER_E_REPORT_DEFERRED__ ? window.__FIXER_E_REPORT_DEFERRED__() : {error:"no deferred fn"}'
        ).catch(e => ({ error: e.message }));
        if (fxDeferred && fxDeferred.ytEmbedIframeCreated) break;
    }
    say('\n=== ENFORCEMENT FIXTURE (main world) ===\n' + JSON.stringify({ ...fxReport, ...fxDeferred }, null, 2));

    // ---------- 3. does the real YouTube embed still work? ----------
    // The injection tests above pollute the page, so reload clean and run the
    // exact YT.Player call frontend/scout.js:636 makes.
    say('\nRELOADING fixture clean for the YouTube embed check...');
    await win.webContents.loadURL(`${ORIGIN}/__fixer_e_probe.html`).catch(() => {});
    let ytApi = false;
    for (let i = 0; i < 20; i++) {
        await new Promise(r => setTimeout(r, 1000));
        const ready = await win.webContents.executeJavaScript(
            'typeof window.YT === "object" && !!window.YT.Player'
        ).catch(() => false);
        if (ready) { ytApi = true; say('YT.Player available after ' + (i + 1) + 's'); break; }
    }
    let ytResult = { ytApiLoaded: ytApi };
    let ytDeferred = {};
    if (ytApi) {
        ytResult = await win.webContents.executeJavaScript('window.__FIXER_E_YT__()').catch(e => ({ error: e.message }));
        for (let i = 0; i < 10; i++) {
            await new Promise(r => setTimeout(r, 2000));
            ytDeferred = await win.webContents.executeJavaScript('window.__FIXER_E_YT_DEFERRED__()').catch(e => ({ error: e.message }));
            if (ytDeferred && ytDeferred.embedIframeCreated) { say('embed iframe appeared after +' + ((i + 1) * 2) + 's'); break; }
        }
    }
    say('\n=== YOUTUBE EMBED UNDER CSP (mirrors scout.js:636) ===\n' + JSON.stringify({ ...ytResult, ...ytDeferred }, null, 2));
    win.destroy();

    say('\n=== CSP violations observed: ' + violations.length + ' ===');
    violations.forEach(v => say('  - ' + v.replace(/\s+/g, ' ').slice(0, 200)));
    say('\n=== non-CSP console errors: ' + otherErrors.length + ' ===');
    otherErrors.slice(0, 12).forEach(v => say('  - ' + String(v).slice(0, 160)));
    say('\nPROBE_DONE');
    app.exit(0);
});

setTimeout(() => { say('PROBE_TIMEOUT'); process.exit(3); }, 170000);
