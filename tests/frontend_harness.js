/*
 * tests/frontend_harness.js — REAL-frontend test harness.
 *
 * Loads the *shipped* `frontend/vendor/chess.min.js`, `frontend/board.js` and
 * `frontend/repertoire_builder.js` into a Node `vm` context behind a minimal DOM
 * shim, then evaluates a scenario file supplied as argv[2] and prints a single
 * JSON object on stdout.
 *
 * Why this file exists
 * --------------------
 * `tests/test_timeline_spam.py` used to embed a hand-written JavaScript
 * re-implementation of the timeline logic and execute *that* (audit finding
 * CODE-05-05). Four "spam resistance" tests therefore passed even with
 * `frontend/board.js` and `frontend/repertoire_builder.js` deleted. The mock was
 * an unverified second copy of the specification.
 *
 * This harness deletes the mock: the subject under test is now the real shipped
 * code, loaded from disk on every run. A regression in `jumpToStep`,
 * `onBoardMove`, `stepForward`/`stepBack`, `InteractiveChessboard.stopAnimations`
 * or `startEvalStream` makes the corresponding Python test fail.
 *
 * Usage:  node tests/frontend_harness.js <scenario.js>
 * Output: a JSON object on stdout, or a JSON object with an "error" key.
 *
 * Nothing here is application code — it is test scaffolding. `frontend/` is
 * never modified by this harness; the files are only read.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const PROJECT_ROOT = path.dirname(__dirname);
const FRONTEND_DIR = path.join(PROJECT_ROOT, 'frontend');

/* ------------------------------------------------------------------ *
 * Minimal DOM shim
 * ------------------------------------------------------------------ */

class ClassList {
    constructor(el) { this._el = el; this._set = new Set(); }
    add(...names) { names.forEach(n => n && this._set.add(n)); }
    remove(...names) { names.forEach(n => this._set.delete(n)); }
    contains(n) { return this._set.has(n); }
    toggle(n, force) {
        if (force === true) { this._set.add(n); return true; }
        if (force === false) { this._set.delete(n); return false; }
        if (this._set.has(n)) { this._set.delete(n); return false; }
        this._set.add(n); return true;
    }
    get value() { return [...this._set].join(' '); }
    toString() { return this.value; }
}

class FakeAnimation {
    constructor(el) {
        this.effect = { target: el };
        this.playState = 'running';
        this.onfinish = null;
        this.oncancel = null;
        this.cancelled = false;
    }
    cancel() { this.playState = 'idle'; this.cancelled = true; if (this.oncancel) this.oncancel({ type: 'cancel' }); }
    finish() { this.playState = 'finished'; if (this.onfinish) this.onfinish({ type: 'finish' }); }
    // Never resolves on its own: the shim has no compositor.
    get finished() { return new Promise(() => {}); }
    get currentTime() { return 0; }
}

class FakeElement {
    constructor(tagName, doc) {
        this.tagName = String(tagName).toUpperCase();
        this.ownerDocument = doc;
        this.children = [];
        this.parentElement = null;
        this.classList = new ClassList(this);
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this._listeners = {};
        this._textContent = '';
        this._innerHTML = '';
        this.draggable = false;
        this.title = '';
        this.value = '';
        this.disabled = false;
        this.checked = false;
        this.hidden = false;
        this.scrollTop = 0;
        this.scrollLeft = 0;
    }

    get className() { return this.classList.value; }
    set className(v) {
        this.classList._set = new Set(String(v || '').split(/\s+/).filter(Boolean));
    }

    get children_() { return this.children; }

    get firstChild() { return this.children[0] || null; }

    get textContent() {
        if (this.children.length) return this.children.map(c => c.textContent).join('');
        return this._textContent;
    }
    set textContent(v) {
        this.children.forEach(c => { c.parentElement = null; });
        this.children = [];
        this._textContent = v == null ? '' : String(v);
    }

    get innerHTML() { return this._innerHTML; }
    set innerHTML(v) {
        this._innerHTML = v == null ? '' : String(v);
        // Setting innerHTML replaces all children. Applications in this repo only
        // ever set it to markup that they then re-query; we drop the children but
        // keep the string so read-back assertions still work.
        this.children.forEach(c => { c.parentElement = null; });
        this.children = [];
    }

    appendChild(child) {
        if (!child) return child;
        if (child.parentElement) child.parentElement.removeChild(child);
        child.parentElement = this;
        this.children.push(child);
        return child;
    }
    removeChild(child) {
        const i = this.children.indexOf(child);
        if (i !== -1) { this.children.splice(i, 1); child.parentElement = null; }
        return child;
    }
    replaceChildren(...nodes) {
        this.children.forEach(c => { c.parentElement = null; });
        this.children = [];
        nodes.forEach(n => this.appendChild(n));
    }
    insertBefore(child, ref) {
        if (child.parentElement) child.parentElement.removeChild(child);
        child.parentElement = this;
        const i = ref ? this.children.indexOf(ref) : -1;
        if (i === -1) this.children.push(child); else this.children.splice(i, 0, child);
        return child;
    }
    remove() { if (this.parentElement) this.parentElement.removeChild(this); }
    contains(node) {
        if (node === this) return true;
        return this.children.some(c => c.contains && c.contains(node));
    }

    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
    removeAttribute(name) { delete this.attributes[name]; }
    hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name); }

    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
    removeEventListener(type, fn) {
        const l = this._listeners[type];
        if (!l) return;
        const i = l.indexOf(fn);
        if (i !== -1) l.splice(i, 1);
    }
    dispatchEvent(evt) {
        (this._listeners[evt.type] || []).slice().forEach(fn => fn.call(this, evt));
        return true;
    }

    _matches(sel) {
        // Supports: 'tag', '.cls', '#id', 'tag.cls', '.a.b', '.a .b' (descendant),
        // and comma-separated groups. Enough for this repository's usage.
        return sel.split(',').map(s => s.trim()).some(part => {
            const tokens = part.split(/\s+/);
            let scope = this;
            for (let t = 0; t < tokens.length; t++) {
                const token = tokens[t];
                const simple = token.split(/(?=[.#])/).filter(Boolean);
                let node = (t === 0) ? this : null;
                if (t === 0) {
                    if (!this._matchesSimple(simple)) return false;
                } else {
                    // descendant: any ancestor matching
                    node = null;
                    let anc = this.parentElement;
                    while (anc) {
                        if (anc._matchesSimple(simple)) { node = anc; break; }
                        anc = anc.parentElement;
                    }
                    if (!node) return false;
                }
            }
            return true;
        });
    }
    _matchesSimple(simple) {
        for (const s of simple) {
            if (s[0] === '.') { if (!this.classList.contains(s.slice(1))) return false; }
            else if (s[0] === '#') { if (this.getAttribute('id') !== s.slice(1)) return false; }
            else if (s[0] === '[') {
                const m = /^\[([a-zA-Z-]+)(?:=["']?([^\]"']*)["']?)?\]$/.exec(s);
                if (!m) return false;
                if (m[2] === undefined) { if (!this.hasAttribute(m[1])) return false; }
                else if (this.getAttribute(m[1]) !== m[2]) return false;
            } else if (this.tagName !== s.toUpperCase()) return false;
        }
        return true;
    }

    _descendants(out) {
        for (const c of this.children) { out.push(c); c._descendants(out); }
        return out;
    }
    querySelector(sel) { return this._descendants([]).find(el => el._matches(sel)) || null; }
    querySelectorAll(sel) { return this._descendants([]).filter(el => el._matches(sel)); }
    closest(sel) {
        let node = this;
        while (node) {
            if (node._matchesSimple(sel.split(/(?=[.#])/).filter(Boolean))) return node;
            node = node.parentElement;
        }
        return null;
    }

    getBoundingClientRect() {
        const sq = this.dataset && this.dataset.square;
        if (sq) {
            const file = sq.charCodeAt(0) - 97;
            const rank = parseInt(sq[1], 10);
            return { left: file * 64, top: (8 - rank) * 64, width: 64, height: 64, right: file * 64 + 64, bottom: (8 - rank) * 64 + 64, x: file * 64, y: (8 - rank) * 64 };
        }
        return { left: 0, top: 0, width: 64, height: 64, right: 64, bottom: 64, x: 0, y: 0 };
    }

    scrollIntoView() {}
    focus() {}
    blur() {}
    animate() { return new FakeAnimation(this); }
    getBoundingClientRects() { return [this.getBoundingClientRect()]; }
    getContext() { return { clearRect() {}, drawImage() {}, fillRect() {}, save() {}, restore() {} }; }
    toDataURL() { return 'data:,'; }
    click() { this.dispatchEvent({ type: 'click', target: this, preventDefault() {} }); }
    get offsetWidth() { return 64; }
    get offsetHeight() { return 64; }
    get scrollHeight() { return 64; }
}

class FakeDocument {
    constructor() {
        this.byId = new Map();
        this.documentElement = new FakeElement('html', this);
        this.body = new FakeElement('body', this);
        this.head = new FakeElement('head', this);
        this.documentElement.appendChild(this.head);
        this.documentElement.appendChild(this.body);
    }
    createElement(tag) { return new FakeElement(tag, this); }
    createElementNS(ns, tag) { return new FakeElement(tag, this); }
    createTextNode(t) { const e = new FakeElement('#text', this); e.textContent = t; return e; }
    getElementById(id) { return this.byId.get(id) || null; }
    querySelector(sel) { return this.documentElement.querySelector(sel); }
    querySelectorAll(sel) { return this.documentElement.querySelectorAll(sel); }
    getElementsByClassName(c) { return this.documentElement.querySelectorAll('.' + c); }
    getElementsByTagName(t) { return this.documentElement.querySelectorAll(t); }
    addEventListener() {}
    removeEventListener() {}
}

/* ------------------------------------------------------------------ *
 * Sandbox
 * ------------------------------------------------------------------ */

function buildSandbox(record) {
    const document = new FakeDocument();
    // A container element is what InteractiveChessboard is constructed with.
    document.byId.set('repertoireBoard', document.createElement('div'));

    const fetchCalls = [];
    const aborted = [];
    const pendingAborts = [];

    function fakeFetch(url, opts) {
        const signal = opts && opts.signal;
        fetchCalls.push({ url: String(url), signal: signal || null });
        if (signal) {
            pendingAborts.push(signal);
            signal.addEventListener('abort', () => {
                aborted.push(String(url));
            });
        }
        // Never resolves: the harness has no server. `startEvalStream` must rely on
        // its AbortController to stop these in-flight requests.
        return new Promise(() => {});
    }

    const storage = new Map();
    const localStorage = {
        getItem: (k) => (storage.has(k) ? storage.get(k) : null),
        setItem: (k, v) => { storage.set(k, String(v)); },
        removeItem: (k) => { storage.delete(k); },
        clear: () => { storage.clear(); }
    };

    const warnings = [];
    const errors = [];
    const sandbox = {
        console: {
            log: () => {}, warn: (...a) => warnings.push(a.map(String).join(' ')),
            error: (...a) => errors.push(a.map(String).join(' ')), info: () => {}, debug: () => {}
        },
        document,
        localStorage,
        sessionStorage: localStorage,
        fetch: fakeFetch,
        AbortController,
        TextDecoder,
        setTimeout, clearTimeout, setInterval, clearInterval,
        queueMicrotask,
        URL, URLSearchParams, Math, JSON, Date, Promise, Map, Set, Array, Object, String, Number, Boolean, Error, TypeError, isNaN, parseInt, parseFloat, encodeURIComponent, decodeURIComponent, RegExp, Symbol, BigInt, Proxy, Reflect, Intl,
        requestAnimationFrame: (fn) => setTimeout(() => fn(Date.now()), 0),
        cancelAnimationFrame: (id) => clearTimeout(id),
        __record: { fetchCalls, aborted, pendingAborts, warnings, errors, document }
    };
    sandbox.addEventListener = () => {};
    sandbox.removeEventListener = () => {};
    sandbox.getComputedStyle = () => ({ getPropertyValue: () => '' });
    sandbox.matchMedia = () => ({ matches: false, addEventListener: () => {} });
    sandbox.innerWidth = 1280;
    sandbox.innerHeight = 900;
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    sandbox.self = sandbox;
    record.sandbox = sandbox;
    return sandbox;
}

/*
 * Local `<script src>` entries, in the order `frontend/index.html` loads them.
 *
 * Read from index.html rather than hard-coded, because the app's files share ONE
 * global scope: `repertoire_builder.js` calls `normalizeTreeFen()`, which is
 * declared in `frontend/scout.js`. Loading only board.js + repertoire_builder.js
 * therefore raised `ReferenceError: normalizeTreeFen is not defined` — a harness
 * artefact, not an application bug (in the browser every classic script shares
 * the global scope, so a function declared in one file is callable from another).
 * Loading the real chain keeps the harness faithful to production and keeps it
 * working when a later change adds a cross-file dependency.
 */
function localScriptOrder() {
    const indexHtml = path.join(FRONTEND_DIR, 'index.html');
    if (!fs.existsSync(indexHtml)) {
        // Some harnesses stage a SUBSET of the tree (e.g. copy only board.js and
        // repertoire_builder.js into a temp dir) and run this file from there. Do
        // not hard-fail on a missing manifest; fall back to the core chain and let
        // the caller's own assertions decide whether that was enough.
        return ['frontend/board.js', 'frontend/repertoire_builder.js'];
    }
    const html = fs.readFileSync(indexHtml, 'utf8');
    const out = [];
    const seen = new Set();
    const re = /<script[^>]*\bsrc\s*=\s*["']([^"']+)["'][^>]*>/gi;
    let m;
    while ((m = re.exec(html)) !== null) {
        let src = m[1];
        if (!src.startsWith('/')) continue;            // not a local script
        src = src.split('?')[0].replace(/^\/+/, '');   // drop the cache-buster
        if (!/\.(js|mjs)$/i.test(src)) continue;        // not JavaScript
        if (!fs.existsSync(path.join(FRONTEND_DIR, src))) {
            throw new Error('index.html references a missing script: /' + src);
        }
        if (seen.has(src)) continue;
        seen.add(src);
        out.push('frontend/' + src);
    }
    if (out.length === 0) throw new Error('no local <script src> found in index.html');
    return out;
}

function loadRealFrontend(sandbox) {
    const loadedSources = [];
    const crypto = require('crypto');
    const recordSource = (relPath, src) => {
        loadedSources.push({
            filename: relPath,
            sha256: crypto.createHash('sha256').update(src, 'utf8').digest('hex'),
            length: src.length
        });
    };

    // chess.js first, exactly as index.html does.
    const chessRel = 'frontend/vendor/chess.min.js';
    const chessSrc = fs.readFileSync(path.join(FRONTEND_DIR, 'vendor', 'chess.min.js'), 'utf8');
    recordSource(chessRel, chessSrc);
    // chess.min.js is UMD; give it CommonJS globals so it exports instead of
    // assuming a browser <script> tag.
    const chessModule = { exports: {} };
    const chessCtx = vm.createContext(Object.assign(Object.create(null), sandbox, {
        module: chessModule, exports: chessModule.exports, require, define: undefined
    }));
    vm.runInContext(chessSrc, chessCtx, { filename: chessRel });
    const Chess = (chessModule.exports && chessModule.exports.Chess) || sandbox.Chess;
    if (!Chess) throw new Error('chess.min.js did not export Chess');
    sandbox.Chess = Chess;

    const ctx = vm.createContext(sandbox);
    for (const rel of localScriptOrder()) {
        if (rel === chessRel) continue;
        const src = fs.readFileSync(path.join(PROJECT_ROOT, rel), 'utf8');
        recordSource(rel, src);
        vm.runInContext(src, ctx, { filename: rel });
    }
    sandbox.__record.loadedSources = loadedSources;
    return ctx;
}

function main() {
    const scenarioPath = process.argv[2];
    if (!scenarioPath) {
        process.stdout.write(JSON.stringify({ error: 'usage: node tests/frontend_harness.js <scenario.js>' }));
        return;
    }
    let out;
    const record = {};
    try {
        const sandbox = buildSandbox(record);
        const ctx = loadRealFrontend(sandbox);
        const scenario = fs.readFileSync(scenarioPath, 'utf8');
        const run = vm.runInContext(
            `(function (api) {\n${scenario}\n})`,
            ctx,
            { filename: 'scenario.js' }
        );
        const result = run({
            sandbox,
            Chess: sandbox.Chess,
            InteractiveChessboard: sandbox.InteractiveChessboard,
            window: sandbox,
            record: sandbox.__record
        });
        out = result && typeof result.then === 'function'
            ? { __promise: true, result }
            : (result === undefined ? {} : result);
        if (out && out.__promise) {
            out = out.result;
            out.then((resolved) => {
                emit(resolved, record);
            }, (err) => {
                emit({ __error: String(err && err.stack || err) }, record);
            });
            return;
        }
    } catch (err) {
        emit({ __error: String(err && err.stack || err) }, record);
        return;
    }
    emit(out, record);
}

function emit(value, record) {
    const rec = (record && record.sandbox && record.sandbox.__record) || null;
    const payload = (value && typeof value === 'object' && !Array.isArray(value)) ? value : { value };
    if (rec) {
        payload.__harness = {
            loadedSources: rec.loadedSources || [],
            abortedFetches: rec.aborted,
            fetchCount: rec.fetchCalls.length,
            fetchUrls: rec.fetchCalls.map(f => f.url),
            consoleWarnings: rec.warnings,
            consoleErrors: rec.errors
        };
    }
    try {
        process.stdout.write(JSON.stringify(payload));
    } catch (e) {
        process.stdout.write(JSON.stringify({ __error: 'unserialisable result: ' + String(e) }));
    }
}

main();
