#!/usr/bin/env python3
"""
FIXER-E-01 — runtime probe for the pure security helpers exported by
electron/main.js.

Loads electron/main.js in Node with a *stubbed* `electron` module (so no window,
no backend spawn, no app lock is ever touched) and exercises:
  * resolveExternalUrl()  - the shell.openExternal allow-list
  * isTrustedAppUrl()     - the navigation / CSP trust anchor
  * APP_CSP               - the injected Content-Security-Policy string

Idempotent and re-runnable: it never touches chess_improvement.db, never opens a
network connection and never starts a process.
"""
import json
import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NODE_STUB = r"""
const Module = require('module');
const path = require('path');

// Stub `electron` so importing main.js has zero side effects.
const stub = {
    app: {
        on() {}, once() {}, removeAllListeners() {},
        commandLine: { appendSwitch() {} },
        requestSingleInstanceLock: () => true,
        whenReady: () => new Promise(() => {}),   // never resolves
        quit() {}, exit() {}
    },
    BrowserWindow: function () { throw new Error('BrowserWindow must not be constructed'); },
    Menu: { buildFromTemplate: () => ({}), setApplicationMenu() {} },
    dialog: { showErrorBox() {}, showMessageBox() {} },
    shell: { openExternal: async () => {} },
    session: {
        defaultSession: {
            setPermissionRequestHandler() {},
            setPermissionCheckHandler() {},
            webRequest: { onHeadersReceived() {} }
        }
    }
};
const origLoad = Module._load;
Module._load = function (request, parent, isMain) {
    if (request === 'electron') return stub;
    return origLoad.apply(this, arguments);
};

const api = require(process.env.FIXER_E_MAIN_JS);

const CASES = [
    // (url, expected: 'allow' | 'deny')
    ['https://lichess.org/gVf9kL3M', 'allow'],
    ['https://lichess.org/account/oauth/token', 'allow'],
    ['https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'allow'],
    ['https://youtu.be/dQw4w9WgXcQ', 'allow'],
    ['https://www.youtube-nocookie.com/embed/abc', 'allow'],
    // --- must be denied ---
    ['http://lichess.org/gVf9kL3M', 'deny'],           // not https
    ['https://evil.example/steal', 'deny'],            // not allow-listed
    ['https://lichess.org.evil.example/', 'deny'],     // suffix confusion
    ['https://LICHESS.org.evil.example/', 'deny'],
    ['file:///etc/passwd', 'deny'],
    ['file:///home/robin/.bashrc', 'deny'],
    ['javascript:alert(1)', 'deny'],
    ['JavaScript:alert(1)', 'deny'],
    ['data:text/html,<script>alert(1)</script>', 'deny'],
    ['vbscript:msgbox(1)', 'deny'],
    ['about:blank', 'deny'],
    ['https://user:pass@lichess.org/', 'deny'],        // embedded credentials
    ['', 'deny'],
    ['not a url at all', 'deny'],
    ['//lichess.org/protocol-relative', 'deny'],
    ['https://localhost:38281/', 'deny']
];

const results = { cases: [], trusted: [], csp: api.APP_CSP, allowlist: Array.from(api.EXTERNAL_HOST_ALLOWLIST) };
for (const [url, expected] of CASES) {
    let got = 'deny';
    try {
        if (api.resolveExternalUrl(url) !== null) got = 'allow';
    } catch (e) {
        got = 'threw:' + e.message;
    }
    results.cases.push({ url, expected, got, pass: got === expected });
}

const TRUST = [
    ['http://127.0.0.1:38281/', true],
    ['http://127.0.0.1:38281/index.html', true],
    ['http://127.0.0.1:38281/api/dashboard', true],
    ['http://127.0.0.1:38282/', false],
    ['http://localhost:38281/', false],
    ['https://127.0.0.1:38281/', false],
    ['http://127.0.0.1:38281@evil.example/', false],
    ['file:///home/robin/secret.html', false],
    ['https://evil.example/', false]
];
for (const [url, expected] of TRUST) {
    let got;
    try { got = api.isTrustedAppUrl(url); } catch (e) { got = 'threw:' + e.message; }
    results.trusted.push({ url, expected, got, pass: got === expected });
}
process.stdout.write(JSON.stringify(results));
"""


def main():
    main_js = os.path.join(PROJECT_ROOT, "electron", "main.js")
    proc = subprocess.run(
        ["node", "-e", NODE_STUB],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "CHESS_PORT": "38281", "FIXER_E_MAIN_JS": main_js},
    )
    if proc.returncode != 0:
        print("node harness FAILED")
        print(proc.stdout)
        print(proc.stderr)
        return 1
    data = json.loads(proc.stdout)

    failed = 0
    print("=== resolveExternalUrl() allow-list ===")
    for c in data["cases"]:
        mark = "PASS" if c["pass"] else "FAIL"
        if not c["pass"]:
            failed += 1
        print(f"  [{mark}] {c['expected']:5s} -> {c['got']:5s}  {c['url']!r}")
    print("=== isTrustedAppUrl() navigation anchor ===")
    for t in data["trusted"]:
        mark = "PASS" if t["pass"] else "FAIL"
        if not t["pass"]:
            failed += 1
        print(f"  [{mark}] {str(t['expected']):5s} -> {str(t['got']):5s}  {t['url']!r}")

    print("=== APP_CSP ===")
    print("  " + data["csp"])
    print("=== allow-list hosts ===")
    print("  " + ", ".join(data["allowlist"]))

    print(f"\n{failed} failing case(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
