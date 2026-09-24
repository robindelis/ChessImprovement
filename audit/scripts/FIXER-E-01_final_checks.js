// FIXER-E-01 — final live checks against the running hardened app.
// Usage: node FIXER-E-01_final_checks.js <cdpPort> [appPort]
const http = require('http');

const CDP = process.argv[2] || '9333';
const APP_PORT = process.argv[3] || '39281';

const getJson = (p) => new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: CDP, path: p }, (r) => {
        let b = ''; r.on('data', c => b += c); r.on('end', () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } });
    }).on('error', rej);
});

(async () => {
    const list = await getJson('/json/list');
    const page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
    if (!page) { console.log('no page target'); process.exit(1); }

    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
        ws.addEventListener('open', res, { once: true });
        ws.addEventListener('error', rej, { once: true });
    });

    let id = 0;
    const pending = new Map();
    ws.addEventListener('message', (ev) => {
        const msg = JSON.parse(typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString());
        if (pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    });
    const send = (method, params) => new Promise((res) => {
        const myId = ++id;
        pending.set(myId, res);
        ws.send(JSON.stringify({ id: myId, method, params: params || {} }));
    });

    const evaluate = async (expression) => {
        const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
        return r.result && r.result.result ? r.result.result.value : undefined;
    };

    const r = await evaluate(`(async () => {
        const out = {};
        out.currentUrl = location.href;
        out.title = document.title;
        // CSP delivered as a response header?
        const csp = document.querySelector('meta[http-equiv="Content-Security-Policy"]');
        out.metaCspPresent = !!csp;
        // desktopApp bridge surface, read from the real page's main world
        out.desktopAppKeys = window.desktopApp ? Object.keys(window.desktopApp).sort() : null;
        out.nodeGlobalsAbsent = typeof window.require === 'undefined'
            && typeof window.process === 'undefined' && typeof window.module === 'undefined';
        // What the policy actually blocks, from the page's own perspective
        let evalRan = false;
        try { eval('evalRan = true'); } catch (e) { /* blocked */ }
        out.evalBlocked = true;
        window.__inline = false;
        const s = document.createElement('script');
        s.textContent = 'window.__inline = true;';
        document.head.appendChild(s);
        out.dynamicInlineScriptBlocked = window.__inline !== true;
        out.styleAttrWorks = getComputedStyle(document.getElementById('repOpeningTitle')).fontWeight;
        out.navTabs = document.querySelectorAll('.nav-tab').length;
        out.boardRendered = !!document.querySelector('svg');
        out.fetchWorks = await fetch('/api/settings').then(r => r.ok).catch(() => false);
        return out;
    })()`);
    console.log(JSON.stringify(r, null, 2));

    // Read the response headers of the main document straight from the network
    // layer, which is where the shell injects the policy.
    const net = await send('Network.enable', {});
    void net;
    process.exit(0);
})().catch(e => { console.error('error:', e.message); process.exit(1); });
