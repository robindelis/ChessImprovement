const { app, BrowserWindow, Menu, dialog, shell, session } = require('electron');
const path = require('path');
const http = require('http');
const net = require('net');
const { spawn } = require('child_process');
const fs = require('fs');

const PROJECT_ROOT = path.resolve(__dirname, '..');
const DEFAULT_PORT = 38281;
let PORT = parseInt(process.env.CHESS_PORT || process.env.PORT || `${DEFAULT_PORT}`, 10);
let SERVER_URL = `http://127.0.0.1:${PORT}`;

// How long the backend is given to shut down gracefully before SIGKILL.
const BACKEND_STOP_GRACE_MS = 1200;
// How many ports we are willing to try before giving up on the backend.
const BACKEND_START_ATTEMPTS = 5;

// On Linux, enable automatic Wayland / X11 display server detection
if (process.platform === 'linux') {
    app.commandLine.appendSwitch('ozone-platform-hint', 'auto');
    app.commandLine.appendSwitch('enable-features', 'WaylandWindowDecorations');
    // Prevent uninitialized YUV zero-buffer green flicker during video decode initialization on Linux/hybrid GPUs
    app.commandLine.appendSwitch('disable-accelerated-video-decode');
}

let mainWindow = null;
let pythonProcess = null;
let serverStartedByUs = false;

// --- Security hardening (audit CODE-05-09) ----------------------------------
// The renderer may only ever display our own local backend. Anything else is
// either blocked outright or handed to the user's system browser through a
// strict allow-list (https + known hosts only).

// Identity of our own backend, used before adopting an already running server.
const BACKEND_IDENTITY_TITLE = 'Chess Opening Improvement Platform';

// Hosts we are willing to hand over to the system browser via shell.openExternal.
// Everything else (file:, javascript:, data:, vbscript:, http:, unknown hosts) is
// rejected by resolveExternalUrl().
const EXTERNAL_HOST_ALLOWLIST = new Set([
    'lichess.org',
    'www.lichess.org',
    'chessbook.com',
    'www.chessbook.com',
    'youtube.com',
    'www.youtube.com',
    'm.youtube.com',
    'youtu.be',
    'www.youtu.be',
    'youtube-nocookie.com',
    'www.youtube-nocookie.com'
]);

// Content-Security-Policy injected into every main-frame response of our own
// backend (see applySessionHardening()). Minimum viable for this app:
//   * script-src 'self' + https://www.youtube.com - the YouTube IFrame Player API
//     (frontend/index.html) also bootstraps
//     https://www.youtube.com/s/player/<hash>/www-widgetapi.js, i.e. the same
//     host. No 'unsafe-inline', no 'unsafe-eval'.
//   * style-src 'unsafe-inline' is REQUIRED: index.html and the JS templates
//     carry ~360 style="..." attributes. It does not weaken script-src.
//   * frame-src: the YouTube embed is the only frame the app creates.
//   * connect-src: every fetch()/stream in the app is same-origin relative.
//   * img-src: local pieces, data:/blob: and i.ytimg.com thumbnails only.
//   * default-src 'self' is a safety net for anything not enumerated (workers,
//     manifests) - never a wildcard.
const APP_CSP = [
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'none'",
    "script-src 'self' https://www.youtube.com",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https://i.ytimg.com",
    "font-src 'self' data:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "frame-src https://www.youtube.com"
].join('; ');

// True only for the exact origin of our own local backend.
function isTrustedAppUrl(rawUrl) {
    try {
        return new URL(String(rawUrl)).origin === SERVER_URL;
    } catch (e) {
        return false;
    }
}

// Returns a normalised https URL when the target is on the external allow-list,
// otherwise null. Rejects file:, javascript:, data:, vbscript:, http: and every
// host that is not explicitly listed.
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

// shell.openExternal with a validated URL. Returns true when it was handed over.
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

// Check if a port is currently free to bind
function isPortAvailable(port) {
    return new Promise((resolve) => {
        const tester = net.createServer();
        tester.once('error', () => resolve(false));
        tester.once('listening', () => {
            tester.close(() => resolve(true));
        });
        tester.listen(port, '127.0.0.1');
    });
}

// Find a free port starting from preferred port
async function resolveFreePort(preferredPort) {
    let port = preferredPort;
    for (let i = 0; i < 50; i++) {
        const available = await isPortAvailable(port);
        if (available) return port;
        port++;
    }
    return preferredPort;
}

// Resolve python binary path (favor miniconda environment if available)
function getPythonPath() {
    if (process.env.PYTHON_PATH && fs.existsSync(process.env.PYTHON_PATH)) {
        return process.env.PYTHON_PATH;
    }
    const condaPython = '/home/robin/miniconda3/bin/python3';
    if (fs.existsSync(condaPython)) {
        return condaPython;
    }
    return 'python3';
}

// Identify our backend before adopting it. An unrelated local process that
// happens to answer 200-399 must never be treated as the app's backend, so we
// require the FastAPI OpenAPI identity title.
function probeBackend() {
    return new Promise((resolve) => {
        let settled = false;
        const done = (value) => {
            if (!settled) {
                settled = true;
                resolve(value);
            }
        };
        const req = http.get(`${SERVER_URL}/openapi.json`, { timeout: 2000 }, (res) => {
            if (res.statusCode !== 200) {
                res.resume();
                return done(false);
            }
            let body = '';
            res.setEncoding('utf8');
            res.on('data', (chunk) => {
                if (body.length < 131072) body += chunk;
            });
            res.on('end', () => {
                try {
                    const info = JSON.parse(body).info || {};
                    done(info.title === BACKEND_IDENTITY_TITLE);
                } catch (e) {
                    done(false);
                }
            });
        });
        req.on('error', () => done(false));
        req.on('timeout', () => {
            req.destroy();
            done(false);
        });
    });
}

// Poll server until ready or timeout
async function waitForServer(timeoutMs = 25000) {
    const startTime = Date.now();
    while (Date.now() - startTime < timeoutMs) {
        if (await probeBackend()) return true;
        // Fail fast when our own child already died (e.g. lost the bind race).
        if (serverStartedByUs && !pythonProcess) return false;
        await new Promise((r) => setTimeout(r, 250));
    }
    return false;
}

// Start python backend process
function startPythonBackend() {
    if (pythonProcess) return;
    const pythonPath = getPythonPath();
    const scriptPath = path.join(PROJECT_ROOT, 'start.py');
    console.log(`[Desktop] Starting Python backend with: ${pythonPath} ${scriptPath} --port ${PORT}`);

    pythonProcess = spawn(pythonPath, [scriptPath, '--port', String(PORT)], {
        cwd: PROJECT_ROOT,
        env: {
            ...process.env,
            CHESS_PORT: String(PORT),
            PYTHONUNBUFFERED: '1'
        },
        stdio: ['ignore', 'pipe', 'pipe'],
        // Own process group / session: the backend and the Stockfish engine it
        // spawns through python-chess can then be signalled as one unit, so no
        // shutdown path can leave an orphaned engine behind.
        detached: true
    });

    pythonProcess.stdout.on('data', (data) => {
        const str = data.toString().trim();
        if (str) console.log(`[Python] ${str}`);
    });

    pythonProcess.stderr.on('data', (data) => {
        const str = data.toString().trim();
        if (str) console.error(`[Python-Err] ${str}`);
    });

    pythonProcess.on('error', (err) => {
        console.error('[Desktop] Failed to spawn Python process:', err);
        dialog.showErrorBox('Python Backend Error', `Failed to start Python process: ${err.message}`);
    });

    pythonProcess.on('exit', (code, signal) => {
        console.log(`[Desktop] Python process exited with code ${code}, signal ${signal}`);
        pythonProcess = null;
    });

    serverStartedByUs = true;
    backendStopRequested = false;
}

// Signal the whole backend process group (uvicorn + Stockfish), falling back to
// the single process when it is not a group leader. Never signals a pid that
// has already been reaped: after a lost bind race the child's pid can be
// recycled by an unrelated process, and a group signal would hit that.
function signalProcessTree(proc, signal) {
    if (!proc || !proc.pid) return;
    if (proc.exitCode !== null || proc.signalCode !== null) return;
    try {
        process.kill(-proc.pid, signal);
        return;
    } catch (e) {
        // ESRCH (already gone) or not a process group leader - try the pid.
    }
    try {
        proc.kill(signal);
    } catch (e) {
        // Nothing left to signal.
    }
}

// Stop python backend on quit. Idempotent and safe to call from every exit path
// (window-all-closed, before-quit, quit, SIGINT, SIGTERM all reach it).
let backendStopRequested = false;
function stopPythonBackend() {
    const proc = serverStartedByUs ? pythonProcess : null;
    if (!proc || backendStopRequested) return;
    backendStopRequested = true;
    console.log(`[Desktop] Stopping Python backend process group (pid ${proc.pid})...`);
    signalProcessTree(proc, 'SIGINT');
    setTimeout(() => {
        if (proc.exitCode === null && proc.signalCode === null) {
            try {
                signalProcessTree(proc, 'SIGKILL');
            } catch (e) {
                // ignore
            }
        }
    }, BACKEND_STOP_GRACE_MS);
}

// Deny every renderer permission request (camera, microphone, geolocation,
// notifications, ...) and deliver our CSP on the app's main-frame documents.
function applySessionHardening() {
    const ses = session.defaultSession;

    ses.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    ses.setPermissionCheckHandler(() => false);

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
}

function createApplicationMenu() {
    const isMac = process.platform === 'darwin';
    const template = [
        ...(isMac ? [{ role: 'appMenu' }] : []),
        {
            label: 'File',
            submenu: [
                {
                    label: 'Refresh / Sync Games',
                    accelerator: 'CmdOrCtrl+Shift+S',
                    click: () => {
                        if (mainWindow) mainWindow.webContents.send('menu-sync-games');
                    }
                },
                { type: 'separator' },
                isMac ? { role: 'close' } : { role: 'quit' }
            ]
        },
        {
            label: 'Edit',
            submenu: [
                { role: 'undo' },
                { role: 'redo' },
                { type: 'separator' },
                { role: 'cut' },
                { role: 'copy' },
                { role: 'paste' },
                { role: 'selectAll' }
            ]
        },
        {
            label: 'View',
            submenu: [
                { role: 'reload' },
                { role: 'forceReload' },
                { role: 'toggleDevTools' },
                { type: 'separator' },
                { role: 'resetZoom' },
                { role: 'zoomIn' },
                { role: 'zoomOut' },
                { type: 'separator' },
                { role: 'togglefullscreen' }
            ]
        },
        {
            label: 'Window',
            submenu: [
                { role: 'minimize' },
                { role: 'zoom' },
                ...(isMac ? [{ type: 'separator' }, { role: 'front' }] : [{ role: 'close' }])
            ]
        },
        {
            label: 'Help',
            submenu: [
                {
                    label: 'About Chess Opening Improvement',
                    click: () => {
                        dialog.showMessageBox(mainWindow, {
                            type: 'info',
                            title: 'About Chess Opening Improvement',
                            message: 'Chess Opening Improvement Platform',
                            detail: 'Personalized Repertoire Builder & Trainer with Stockfish Engine.\nVersion 1.0.0\nCreated for Robin (RobiDeli).',
                            icon: path.join(__dirname, 'icon.png')
                        });
                    }
                }
            ]
        }
    ];

    const menu = Menu.buildFromTemplate(template);
    Menu.setApplicationMenu(menu);
}

function createWindow() {
    mainWindow = new BrowserWindow({
        title: 'Chess Opening Improvement',
        width: 1440,
        height: 940,
        minWidth: 1040,
        minHeight: 700,
        backgroundColor: '#0b1120',
        icon: path.join(__dirname, 'icon.png'),
        show: false,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            nodeIntegration: false,
            contextIsolation: true,
            sandbox: true,
            webSecurity: true,
            allowRunningInsecureContent: false,
            webviewTag: false
        }
    });

    createApplicationMenu();

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

    // Deny every window.open()/target=_blank: a second BrowserWindow would
    // inherit this preload. Allow-listed https links go to the system browser.
    contents.setWindowOpenHandler(({ url }) => {
        openExternalUrl(url);
        return { action: 'deny' };
    });

    // Load server URL
    mainWindow.loadURL(SERVER_URL);

    mainWindow.once('ready-to-show', () => {
        mainWindow.show();
        mainWindow.focus();
    });

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

// Single instance lock
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
    console.log('[Desktop] Another instance is already running. Quitting.');
    app.quit();
} else {
    app.on('second-instance', () => {
        if (mainWindow) {
            if (mainWindow.isMinimized()) mainWindow.restore();
            mainWindow.focus();
        }
    });

    app.whenReady().then(async () => {
        console.log('[Desktop] Electron initialized.');

        applySessionHardening();

        let backendReady = false;
        if (await probeBackend()) {
            console.log(`[Desktop] Adopting already running backend on ${SERVER_URL}.`);
            backendReady = true;
        } else {
            // resolveFreePort() closes its probe socket before uvicorn binds, so
            // a concurrent launch can still win the race. If the child dies
            // before answering, retry on the next port instead of showing a
            // misleading "Startup Timeout" dialog.
            let candidatePort = PORT;
            for (let attempt = 1; attempt <= BACKEND_START_ATTEMPTS && !backendReady; attempt++) {
                const freePort = await resolveFreePort(candidatePort);
                candidatePort = freePort + 1;
                if (freePort !== PORT) {
                    console.log(`[Desktop] Preferred port ${PORT} was occupied, using port ${freePort}`);
                }
                PORT = freePort;
                SERVER_URL = `http://127.0.0.1:${PORT}`;
                startPythonBackend();
                backendReady = await waitForServer(25000);
                if (!backendReady) {
                    console.warn(`[Desktop] Backend attempt ${attempt}/${BACKEND_START_ATTEMPTS} on port ${PORT} failed, retrying next port.`);
                    stopPythonBackend();
                    await new Promise((r) => setTimeout(r, 500));
                }
            }
        }

        if (!backendReady) {
            dialog.showErrorBox(
                'Server Startup Timeout',
                `The Python backend could not be reached at ${SERVER_URL}.\nPlease verify that Python and dependencies are installed.`
            );
            app.quit();
            return;
        }

        console.log('[Desktop] Backend is online. Opening application window.');
        createWindow();

        app.on('activate', () => {
            if (BrowserWindow.getAllWindows().length === 0) {
                createWindow();
            }
        });
    });

    app.on('window-all-closed', () => {
        stopPythonBackend();
        if (process.platform !== 'darwin') {
            app.quit();
        }
    });

    app.on('before-quit', () => {
        stopPythonBackend();
    });

    process.on('SIGINT', () => {
        stopPythonBackend();
        // Give the graceful SIGINT (and its SIGKILL escalation) time to land
        // before this process disappears.
        setTimeout(() => process.exit(0), BACKEND_STOP_GRACE_MS + 200);
    });

    process.on('SIGTERM', () => {
        stopPythonBackend();
        setTimeout(() => process.exit(0), BACKEND_STOP_GRACE_MS + 200);
    });

    // Last resort: if anything reaches 'quit' without going through
    // before-quit / window-all-closed, still signal the backend.
    app.on('quit', () => {
        stopPythonBackend();
    });
}

module.exports = {
    APP_CSP,
    BACKEND_IDENTITY_TITLE,
    EXTERNAL_HOST_ALLOWLIST,
    isTrustedAppUrl,
    resolveExternalUrl,
    openExternalUrl
};
