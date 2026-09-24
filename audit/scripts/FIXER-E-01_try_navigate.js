// FIXER-E-01 — ask the live renderer to navigate to an external origin, so the
// main process's `will-navigate` guard is exercised for real.
// Usage: node FIXER-E-01_try_navigate.js <cdpPort> <url>
const http = require('http');
const CDP = process.argv[2] || '9333';
const TARGET = process.argv[3] || 'https://evil.example/';

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
    const evaluate = (expression) => new Promise((res) => {
        const myId = ++id;
        pending.set(myId, res);
        ws.send(JSON.stringify({ id: myId, method: 'Runtime.evaluate', params: { expression, returnByValue: true } }));
    });

    console.log('navigating renderer to', TARGET);
    await evaluate(`location.href = ${JSON.stringify(TARGET)}; 'requested'`);
    // Also try a same-origin navigation, which must still be ALLOWED.
    await new Promise(r => setTimeout(r, 1500));
    const after = await evaluate('location.href');
    console.log('renderer location after the attempt:', after && after.result && after.result.result && after.result.result.value);
    process.exit(0);
})().catch(e => { console.error('error:', e.message); process.exit(1); });
