"""
Static + behavioural regression tests for the Electron desktop shell
(`electron/main.js`, `electron/preload.js`, `package.json`).

Added by FIXER-E-01 for audit finding CODE-05-09 (Electron shell had no
navigation lockdown, no CSP, and executed a remote YouTube script in a
preload-enabled renderer).

SCOPE / HONESTY NOTE
--------------------
The Electron wiring assertions below are **static source checks**, not runtime
checks: they parse the shipped source and assert on its shape. They cannot
prove that Electron honours the flags, only that the flags are present and
correct in the file that creates the window. The *behavioural* half of this
file is real: the pure URL-validation helpers exported by `electron/main.js`
are executed in Node (see TestExternalUrlAllowListRuntime) against a stubbed
`electron` module, and they are genuinely exercised.

Runtime evidence that the window options, the navigation guard, the window-open
handler and the CSP all work in a real Electron process is produced by the
probe scripts, which are not part of `unittest discover`:
  * audit/scripts/FIXER-E-01_mainjs_probe.py   - allow-list + trust anchor
  * audit/scripts/FIXER-E-01_csp_probe.{sh,js} - CSP enforced in a real renderer
  * audit/scripts/FIXER-E-01_process_lifecycle.sh - no orphan python/stockfish
  * audit/scripts/FIXER-E-01_port_race.sh, FIXER-E-01_backend_retry.sh
"""
import json
import os
import re
import shutil
import subprocess
import unittest

import tests.test_setup  # noqa: F401  - isolates any DB access to a temp file

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_JS = os.path.join(PROJECT_ROOT, "electron", "main.js")
PRELOAD_JS = os.path.join(PROJECT_ROOT, "electron", "preload.js")
PACKAGE_JSON = os.path.join(PROJECT_ROOT, "package.json")
INDEX_HTML = os.path.join(PROJECT_ROOT, "frontend", "index.html")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


MAIN_SRC = read(MAIN_JS)
PRELOAD_SRC = read(PRELOAD_JS)
PKG = json.loads(read(PACKAGE_JSON))


def webprefs_block():
    """Extract the webPreferences object literal from createWindow()."""
    m = re.search(r"webPreferences:\s*\{(.*?)\n\s*\}", MAIN_SRC, re.S)
    return m.group(1) if m else ""


class TestRendererIsolation(unittest.TestCase):
    """The renderer must be locked down (Electron security checklist items 1-5)."""

    def setUp(self):
        self.prefs = webprefs_block()

    def test_webprefs_block_exists(self):
        self.assertIn("webPreferences", MAIN_SRC)
        self.assertTrue(self.prefs.strip(), "could not locate the webPreferences block")

    def test_context_isolation_is_on(self):
        self.assertRegex(self.prefs, r"contextIsolation:\s*true")

    def test_node_integration_is_off(self):
        self.assertRegex(self.prefs, r"nodeIntegration:\s*false")

    def test_sandbox_is_on(self):
        self.assertRegex(self.prefs, r"sandbox:\s*true")

    def test_web_security_is_on(self):
        self.assertRegex(self.prefs, r"webSecurity:\s*true")

    def test_insecure_content_and_webview_are_off(self):
        self.assertRegex(self.prefs, r"allowRunningInsecureContent:\s*false")
        self.assertRegex(self.prefs, r"webviewTag:\s*false")

    def test_no_remote_module_anywhere(self):
        # `@electron/remote` re-enables Node in the renderer; it must not appear.
        for name, src in (("main.js", MAIN_SRC), ("preload.js", PRELOAD_SRC)):
            self.assertNotIn("@electron/remote", src, f"{name} must not use @electron/remote")
            self.assertNotIn("enableRemoteModule", src, f"{name} must not enable the remote module")

    def test_preload_bridge_exposes_no_node_primitives(self):
        # The bridge must not hand the page require/ipcRenderer/process itself.
        for forbidden in ("exposeInMainWorld('require'", 'exposeInMainWorld("require"',
                          "exposeInMainWorld('ipcRenderer'", "exposeInMainWorld('process'"):
            self.assertNotIn(forbidden, PRELOAD_SRC)

    def test_preload_bridge_surface_is_minimal(self):
        """Only `desktopApp` may be exposed, and it carries no window control."""
        exposed = re.findall(r"exposeInMainWorld\(\s*['\"]([^'\"]+)['\"]", PRELOAD_SRC)
        self.assertEqual(exposed, ["desktopApp"],
                         f"unexpected bridge surface: {exposed} (only 'desktopApp' is expected)")
        # minimize/maximize/close let any page script drive the app window.
        for method in ("minimize", "maximize", "close"):
            self.assertNotRegex(
                PRELOAD_SRC, rf"^\s*{method}\s*:", f"desktopApp.{method} must not be exposed"
            )
        # ...and the corresponding ipcMain channels must be gone too.
        for channel in ("window-minimize", "window-maximize", "window-close"):
            self.assertNotIn(channel, MAIN_SRC, f"ipcMain channel {channel} should be removed")
            self.assertNotIn(channel, PRELOAD_SRC)

    def test_on_sync_games_returns_an_unsubscribe_function(self):
        # A bare ipcRenderer.on() inside the bridge leaks a listener per call.
        self.assertIn("removeListener", PRELOAD_SRC)
        self.assertIn("onSyncGames", PRELOAD_SRC)


class TestNavigationControl(unittest.TestCase):
    """Nothing may navigate the renderer off the local backend origin."""

    def test_will_navigate_guard_exists(self):
        self.assertIn("will-navigate", MAIN_SRC)
        self.assertIn("will-redirect", MAIN_SRC)

    def test_navigation_guard_blocks_untrusted_urls(self):
        self.assertRegex(MAIN_SRC, r"event\.preventDefault\(\)")

    def test_webview_attach_is_blocked(self):
        self.assertIn("will-attach-webview", MAIN_SRC)

    def test_trust_anchor_is_exact_origin(self):
        """isTrustedAppUrl must compare origins, not just 'is local'."""
        self.assertRegex(MAIN_SRC, r"new URL\(String\(rawUrl\)\)\.origin\s*===\s*SERVER_URL")


class TestWindowOpenHandler(unittest.TestCase):
    """window.open()/target=_blank must never spawn a second Electron window."""

    def test_set_window_open_handler_is_installed(self):
        self.assertIn("setWindowOpenHandler", MAIN_SRC)

    def test_handler_denies_by_default(self):
        m = re.search(r"setWindowOpenHandler\(\s*\(?\{?.*?\}\)?\s*=>\s*\{(.*?)\}\s*\)", MAIN_SRC, re.S)
        self.assertIsNotNone(m, "could not locate the setWindowOpenHandler callback")
        self.assertIn("action: 'deny'", m.group(1))

    def test_allowlisted_links_go_to_the_system_browser(self):
        self.assertIn("shell.openExternal", MAIN_SRC)
        self.assertRegex(MAIN_SRC, r"openExternalUrl\(url\)")

    def test_open_external_is_gated_by_the_allowlist(self):
        """shell.openExternal must only ever be called with a validated URL."""
        # The single call site must pass the normalised value, never the raw one.
        for call in re.findall(r"shell\.openExternal\(([^)]*)\)", MAIN_SRC):
            self.assertIn("safe", call, f"shell.openExternal({call}) must use the validated URL")

    def test_allowlist_requires_https(self):
        self.assertRegex(MAIN_SRC, r"parsed\.protocol\s*!==\s*'https:'\s*\)\s*return null")

    def test_allowlist_has_no_wildcards(self):
        m = re.search(r"EXTERNAL_HOST_ALLOWLIST\s*=\s*new Set\(\[(.*?)\]\)", MAIN_SRC, re.S)
        self.assertIsNotNone(m)
        hosts = re.findall(r"'([^']+)'", m.group(1))
        self.assertTrue(hosts)
        for h in hosts:
            self.assertNotIn("*", h, "wildcard host in the external allow-list")
            self.assertIn(".", h, f"{h} looks like a bare TLD; use an explicit host")


class TestContentSecurityPolicy(unittest.TestCase):
    """A CSP must exist, be injected where it takes effect, and be minimal."""

    @classmethod
    def setUpClass(cls):
        m = re.search(r"const APP_CSP = \[(.*?)\]\.join\('; '\);", MAIN_SRC, re.S)
        assert m, "APP_CSP not found in electron/main.js"
        cls.directives = [d.strip() for d in re.findall(r'"([^"]+)"', m.group(1))]
        cls.csp = "; ".join(cls.directives)

    def directive(self, name):
        """Return the source list of a directive, e.g. "'self' https://x"."""
        for d in self.directives:
            parts = d.split(None, 1)
            if parts[0] == name:
                return parts[1] if len(parts) > 1 else ""
        return None

    def test_csp_is_defined(self):
        self.assertTrue(self.directives)

    def test_csp_is_injected_as_a_response_header(self):
        """A <meta> CSP cannot express frame-ancestors; the header can."""
        self.assertIn("onHeadersReceived", MAIN_SRC)
        self.assertIn("Content-Security-Policy", MAIN_SRC)
        self.assertIn("mainFrame", MAIN_SRC)

    def test_no_unsafe_inline_or_eval_in_script_src(self):
        script_src = self.directive("script-src")
        self.assertIsNotNone(script_src, "script-src must be present")
        self.assertNotIn("'unsafe-inline'", script_src)
        self.assertNotIn("'unsafe-eval'", script_src)
        self.assertNotIn("'wasm-unsafe-eval'", script_src)

    def test_script_src_is_pinned_to_self_and_the_youtube_host(self):
        self.assertEqual(self.directive("script-src"), "'self' https://www.youtube.com")

    def test_style_src_allows_inline_styles(self):
        """index.html carries ~360 style="" attributes, so this one is required."""
        self.assertEqual(self.directive("style-src"), "'self' 'unsafe-inline'")

    def test_frame_src_allows_only_the_youtube_embed(self):
        self.assertEqual(self.directive("frame-src"), "https://www.youtube.com")

    def test_connect_src_is_self_only(self):
        self.assertEqual(self.directive("connect-src"), "'self'")

    def test_object_and_base_are_denied(self):
        self.assertEqual(self.directive("object-src"), "'none'")
        self.assertEqual(self.directive("base-uri"), "'none'")
        self.assertEqual(self.directive("form-action"), "'none'")

    def test_default_src_has_no_wildcard(self):
        default = self.directive("default-src")
        self.assertIsNotNone(default, "default-src must be present")
        self.assertNotIn("*", default)
        self.assertIn("'self'", default)

    def test_no_csp_uses_a_wildcard_anywhere(self):
        for d in self.directives:
            for token in d.split()[1:]:
                self.assertNotEqual(token, "*", f"wildcard source in {d!r}")
                self.assertFalse(token.endswith(":*"), f"wildcard port in {d!r}")


class TestBackendLifecycle(unittest.TestCase):
    """The Python child must not be orphaned and must be identified correctly."""

    def test_backend_runs_in_its_own_process_group(self):
        """Stockfish is python-chess' child; group signalling reaches it."""
        self.assertRegex(MAIN_SRC, r"detached:\s*true")

    def test_stop_never_signals_a_reaped_pid(self):
        self.assertRegex(MAIN_SRC, r"proc\.exitCode\s*!==\s*null\s*\|\|\s*proc\.signalCode\s*!==\s*null")

    def test_every_exit_path_calls_stop(self):
        for hook in ("window-all-closed", "before-quit", "SIGINT", "SIGTERM"):
            self.assertIn(hook, MAIN_SRC, f"missing exit path: {hook}")
        # SIGINT/SIGTERM must give the graceful window a chance to complete
        # instead of calling process.exit(0) synchronously.
        for sig in ("SIGINT", "SIGTERM"):
            m = re.search(rf"process\.on\('{sig}'.*?\}}\);", MAIN_SRC, re.S)
            self.assertIsNotNone(m)
            self.assertIn("setTimeout", m.group(0),
                          f"{sig} handler must defer process.exit so SIGINT can land")

    def test_backend_adoption_requires_identity(self):
        """Any 2xx on some path must not be enough to adopt a stranger's server."""
        self.assertIn("BACKEND_IDENTITY_TITLE", MAIN_SRC)
        self.assertIn("/openapi.json", MAIN_SRC)
        self.assertRegex(MAIN_SRC, r"info\.title\s*===\s*BACKEND_IDENTITY_TITLE")

    def test_port_race_is_retried(self):
        self.assertIn("BACKEND_START_ATTEMPTS", MAIN_SRC)
        self.assertRegex(MAIN_SRC, r"for \(let attempt = 1; attempt <= BACKEND_START_ATTEMPTS")

    def test_single_instance_lock_is_requested(self):
        self.assertIn("requestSingleInstanceLock", MAIN_SRC)
        self.assertIn("second-instance", MAIN_SRC)

    def test_ipc_handlers_are_not_registered_per_window(self):
        """createWindow() runs again on macOS 'activate'; handlers must not."""
        body = re.search(r"function createWindow\(\)\s*\{(.*?)\n\}", MAIN_SRC, re.S)
        self.assertIsNotNone(body)
        self.assertNotIn("ipcMain.on", body.group(1),
                         "ipcMain handlers inside createWindow() duplicate on re-activation")


class TestPermissionHardening(unittest.TestCase):
    def test_permission_requests_are_denied(self):
        self.assertIn("setPermissionRequestHandler", MAIN_SRC)
        self.assertIn("setPermissionCheckHandler", MAIN_SRC)
        m = re.search(r"setPermissionRequestHandler\(.*?callback\((false|true)\)", MAIN_SRC, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "false", "permission requests must be denied by default")


@unittest.skipUnless(shutil.which("node"), "node is required for the runtime helper test")
class TestExternalUrlAllowListRuntime(unittest.TestCase):
    """Runtime execution of main.js's URL validators with a stubbed `electron`."""

    HARNESS = r"""
const Module = require('module');
const origLoad = Module._load;
const stub = {
    app: {
        on() {}, once() {}, removeAllListeners() {},
        commandLine: { appendSwitch() {} },
        requestSingleInstanceLock: () => true,
        whenReady: () => new Promise(() => {}),
        quit() {}, exit() {}
    },
    BrowserWindow: function () { throw new Error('no window expected'); },
    Menu: { buildFromTemplate: () => ({}), setApplicationMenu() {} },
    dialog: { showErrorBox() {}, showMessageBox() {} },
    shell: { openExternal: async () => {} },
    session: { defaultSession: {
        setPermissionRequestHandler() {}, setPermissionCheckHandler() {},
        webRequest: { onHeadersReceived() {} } } }
};
Module._load = function (request) {
    if (request === 'electron') return stub;
    return origLoad.apply(this, arguments);
};
const api = require(process.env.FIXER_E_MAIN_JS);
const cases = {};
for (const raw of JSON.parse(process.env.FIXER_E_CASES)) {
    cases[raw] = api.resolveExternalUrl(raw);
}
const trusted = {};
for (const raw of JSON.parse(process.env.FIXER_E_TRUSTED)) {
    trusted[raw] = api.isTrustedAppUrl(raw);
}
process.stdout.write(JSON.stringify({ cases, trusted, csp: api.APP_CSP }));
"""

    ALLOWED = [
        "https://lichess.org/gVf9kL3M",
        "https://lichess.org/account/oauth/token",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
    ]
    DENIED = [
        "http://lichess.org/gVf9kL3M",          # not https
        "https://evil.example/steal",           # not allow-listed
        "https://lichess.org.evil.example/",    # suffix confusion
        "https://LICHESS.org.evil.example/",    # case + suffix confusion
        "file:///etc/passwd",
        "file:///home/robin/.bashrc",
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "about:blank",
        "https://user:pass@lichess.org/",       # embedded credentials
        "",
        "not a url at all",
        "//lichess.org/protocol-relative",
        "https://localhost:38281/",             # our own backend is not "external"
    ]
    TRUSTED = [
        ("http://127.0.0.1:38281/", True),
        ("http://127.0.0.1:38281/index.html", True),
        ("http://127.0.0.1:38281/api/dashboard", True),
        ("http://127.0.0.1:38282/", False),
        ("http://localhost:38281/", False),
        ("https://127.0.0.1:38281/", False),
        ("http://127.0.0.1:38281@evil.example/", False),
        ("file:///home/robin/secret.html", False),
        ("https://evil.example/", False),
    ]

    @classmethod
    def setUpClass(cls):
        env = dict(os.environ)
        env.update({
            "CHESS_PORT": "38281",          # pin the origin so the test is deterministic
            "FIXER_E_MAIN_JS": MAIN_JS,
            "FIXER_E_CASES": json.dumps(cls.ALLOWED + cls.DENIED),
            "FIXER_E_TRUSTED": json.dumps([u for u, _ in cls.TRUSTED]),
        })
        proc = subprocess.run(["node", "-e", cls.HARNESS], capture_output=True,
                              text=True, timeout=60, env=env)
        if proc.returncode != 0:
            raise AssertionError(f"node harness failed:\n{proc.stdout}\n{proc.stderr}")
        cls.out = json.loads(proc.stdout)

    def test_allowlisted_https_urls_are_accepted(self):
        for url in self.ALLOWED:
            self.assertIsNotNone(self.out["cases"][url], f"{url} should be allowed")

    def test_everything_else_is_rejected(self):
        for url in self.DENIED:
            self.assertIsNone(self.out["cases"][url], f"{url} must be denied")

    def test_navigation_trust_anchor(self):
        for url, expected in self.TRUSTED:
            self.assertEqual(self.out["trusted"][url], expected, url)

    def test_exported_csp_matches_the_source_constant(self):
        self.assertIn("script-src 'self' https://www.youtube.com", self.out["csp"])


class TestFrontendContract(unittest.TestCase):
    """Invariants the shell relies on, asserted against the (read-only) frontend.

    These are assertions, not fixes: if one fails, the Electron-side CSP needs
    a matching adjustment or frontend/index.html needs the listed change.
    """

    def test_index_html_has_no_inline_script_block(self):
        html = read(INDEX_HTML)
        for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S):
            self.assertEqual(m.group(1).strip(), "",
                             "inline <script> would be blocked by script-src without 'unsafe-inline'")

    def test_inline_event_handlers_are_listed_for_the_frontend_owner(self):
        """Documented CSP/frontend interaction, not a hard requirement.

        scout.js builds thumbnails with `onerror="..."`, which the CSP blocks.
        The app degrades gracefully (broken-image icon instead of the fallback
        SVG), so this is reported rather than enforced. See the patch report's
        hand-off list.
        """
        offenders = []
        for name in os.listdir(os.path.join(PROJECT_ROOT, "frontend")):
            if not name.endswith(".js"):
                continue
            src = read(os.path.join(PROJECT_ROOT, "frontend", name))
            offenders += [(name, m.group(0)) for m in re.finditer(r'\bon\w+=["\']', src)]
        self.assertLessEqual(len(offenders), 1,
                             f"more inline handlers than the documented budget: {offenders}")

    def test_index_html_uses_no_external_script_other_than_youtube(self):
        html = read(INDEX_HTML)
        for src in re.findall(r"<script[^>]*\bsrc=[\"']([^\"']+)[\"']", html):
            if src.startswith("http"):
                self.assertIn("youtube.com", src, f"unexpected remote script: {src}")

    def test_app_never_navigates_the_window_away(self):
        """The shell blocks real navigation, so the SPA must not rely on it."""
        frontend = os.path.join(PROJECT_ROOT, "frontend")
        for name in os.listdir(frontend):
            if not name.endswith(".js"):
                continue
            src = read(os.path.join(frontend, name))
            self.assertNotIn("window.location =", src, f"{name} performs a real navigation")
            self.assertNotIn("location.href =", src, f"{name} performs a real navigation")
            self.assertNotIn("window.open(", src, f"{name} opens a window")
            self.assertNotIn("history.pushState", src, f"{name} performs a real navigation")


class TestPackageJson(unittest.TestCase):
    def test_main_entry_is_the_electron_main_process(self):
        self.assertEqual(PKG.get("main"), "electron/main.js")

    def test_no_install_hooks(self):
        """A postinstall script is arbitrary code execution on `npm install`."""
        for hook in ("preinstall", "install", "postinstall", "prepare", "prepublish"):
            self.assertNotIn(hook, PKG.get("scripts", {}), f"dangerous npm hook: {hook}")

    def test_start_script_launches_the_desktop_app(self):
        scripts = PKG.get("scripts", {})
        self.assertIn("start", scripts)
        self.assertRegex(scripts["start"], r"\belectron\b")

    def test_scripts_do_not_chain_shell_commands(self):
        for name, cmd in PKG.get("scripts", {}).items():
            for token in ("&&", ";", "|", "$(", "`"):
                self.assertNotIn(token, cmd, f"script {name!r} chains shell commands: {cmd!r}")

    def test_scripts_do_not_touch_production_data(self):
        for name, cmd in PKG.get("scripts", {}).items():
            self.assertNotIn("chess_improvement.db", cmd, f"script {name!r} touches the production DB")
            self.assertNotIn("/seed", cmd, f"script {name!r} may seed the production repertoire")
            self.assertNotIn("/clear", cmd, f"script {name!r} may clear the production repertoire")

    def test_electron_is_a_pinned_dependency(self):
        self.assertIn("electron", PKG.get("devDependencies", {}))


if __name__ == "__main__":
    unittest.main()
