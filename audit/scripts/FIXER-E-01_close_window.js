// FIXER-E-01 — dispatch a real window close through the Chrome DevTools
// Protocol, so the app goes through the genuine 'close' -> 'closed' ->
// 'window-all-closed' path instead of a synthetic signal.
//
// Uses Node's built-in global WebSocket (Node >= 22), which is EventTarget-based
// (addEventListener), not the `ws` package API.
//
// Usage: node FIXER-E-01_close_window.js [cdpPort]
const http = require('http');

const PORT = process.argv[2] || '9333';

function getJson(path) {
    return new Promise((resolve, reject) => {
        http.get({ host: '127.0.0.1', port: PORT, path }, (res) => {
            let body = '';
            res.on('data', c => body += c);
            res.on('end', () => { try { resolve(JSON.parse(body)); } catch (e) { reject(e); } });
        }).on('error', reject);
    });
}

(async () => {
    const list = await getJson('/json/list');
    const page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
    if (!page) { console.error('no debuggable page found'); process.exit(1); }
    console.log('target:', page.url);

    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
        ws.addEventListener('open', res, { once: true });
        ws.addEventListener('error', rej, { once: true });
    });

    const reply = new Promise((resolve) => {
        ws.addEventListener('message', (ev) => {
            const msg = JSON.parse(typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString());
            if (msg.id === 1) resolve(msg);
        });
    });

    ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression: 'window.close()' } }));
    const r = await Promise.race([reply, new Promise(res => setTimeout(() => res({ timeout: true }), 8000))]);
    console.log('window.close() ->', JSON.stringify(r));
    process.exit(0);
})().catch(e => { console.error('failed:', e.message); process.exit(1); });
