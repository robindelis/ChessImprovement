// FIXER-E-01 — minimal preload for the CSP probe. Exposes one function that
// runs inside the page and records what the CSP allowed / blocked.
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('__FIXER_E_REPORT__', () => {
    // Inject a dynamic INLINE <script> the way an attacker would; CSP must
    // refuse to execute it.
    window.__INLINE_SCRIPT_RAN__ = false;
    try {
        const s = document.createElement('script');
        s.textContent = 'window.__INLINE_SCRIPT_RAN__ = true;';
        document.head.appendChild(s);
    } catch (e) { /* blocked at creation time also counts as blocked */ }

    // Same for an inline event handler attribute (scout.js uses onerror=...,
    // which the CSP must also block - a functional note for the frontend team).
    const probeImg = document.createElement('img');
    probeImg.setAttribute('onerror', 'window.__INLINE_HANDLER_RAN__ = true;');
    probeImg.src = '/assets/pieces/does-not-exist.svg';
    document.body.appendChild(probeImg);

    return {
        // 1. style="" attributes must still apply (style-src 'unsafe-inline')
        inlineStyleApplied: (() => {
            const el = document.getElementById('repOpeningTitle');
            if (!el) return 'missing-element';
            return getComputedStyle(el).fontWeight;
        })(),
        // 2. the YouTube IFrame Player API must have loaded (script-src host)
        youtubeApiLoaded: typeof window.YT === 'object' && window.YT !== null,
        youtubePlayerCtor: !!(window.YT && window.YT.Player),
        // 3. app scripts must have executed (script-src 'self')
        appScriptRan: typeof window.ChessApp === 'function' || !!document.querySelector('.nav-tab'),
        boardScriptRan: !!document.querySelector('svg'),
        // 4. dynamically injected inline <script> must be BLOCKED
        inlineScriptBlocked: window.__INLINE_SCRIPT_RAN__ === false,
        // 5. eval must be BLOCKED
        evalBlocked: (() => { try { eval('1+1'); return false; } catch (e) { return true; } })(),
        // 6. new Function must be BLOCKED
        functionCtorBlocked: (() => { try { new Function('return 1')(); return false; } catch (e) { return true; } })(),
        // 7. inline event-handler attributes must be BLOCKED
        inlineHandlerBlocked: window.__INLINE_HANDLER_RAN__ !== true,
        // 8. cross-origin fetch must be refused by connect-src 'self'
        remoteFetchBlocked: (() => {
            try { fetch('https://example.com/'); return false; } catch (e) { return true; }
        })(),
        // 9. renderer has no Node: require / process / module must be undefined
        requireAbsent: typeof window.require === 'undefined',
        processAbsent: typeof window.process === 'undefined',
        nodeIntegrationAbsent: typeof window.module === 'undefined' && typeof window.__dirname === 'undefined',
        // 10. exact bridge surface exposed to the page
        desktopBridgeKeys: window.desktopApp ? Object.keys(window.desktopApp).sort() : null
    };
});
