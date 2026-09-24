// FIXER-E-01 — runs in the MAIN world of the CSP fixture page, so its view of
// window/globalThis is the renderer's real global scope (unlike a
// contextBridge callback, which executes in the isolated world).
window.__FIXER_E_REPORT__ = function () {
    const r = {};

    // --- what MUST keep working -------------------------------------------
    r.styleAttrApplied = getComputedStyle(document.getElementById('styled')).fontWeight;
    r.inlineStyleBlockApplied = getComputedStyle(document.querySelector('style') ? document.body : document.body).color;
    r.ytApiLoaded = typeof window.YT === 'object' && window.YT !== null;
    r.ytPlayerCtor = !!(window.YT && window.YT.Player);
    r.styleSheetRules = (function () {
        try { return document.styleSheets[0].cssRules.length; } catch (e) { return 'err:' + e.message; }
    })();
    r.nodeGlobalsAbsent =
        typeof window.require === 'undefined' &&
        typeof window.module === 'undefined' &&
        typeof window.process === 'undefined' &&
        typeof window.Buffer === 'undefined' &&
        typeof window.global === 'undefined';

    // --- what MUST be blocked --------------------------------------------
    try { eval('window.__EVAL_RAN__ = 1'); r.evalBlocked = window.__EVAL_RAN__ !== 1; }
    catch (e) { r.evalBlocked = true; }
    try { new Function('window.__FN_RAN__ = 1')(); r.functionCtorBlocked = window.__FN_RAN__ !== 1; }
    catch (e) { r.functionCtorBlocked = true; }

    // dynamic inline <script> (the classic injection sink)
    window.__INLINE_RAN__ = false;
    try {
        const s = document.createElement('script');
        s.textContent = 'window.__INLINE_RAN__ = true;';
        document.head.appendChild(s);
    } catch (e) { /* blocked at parse time is also "blocked" */ }
    r.dynamicInlineScriptBlocked = window.__INLINE_RAN__ !== true;

    // inline event-handler attribute
    window.__HANDLER_RAN__ = false;
    const img = document.createElement('img');
    img.setAttribute('onerror', 'window.__HANDLER_RAN__ = true;');
    img.src = '/__definitely-missing.svg';
    document.body.appendChild(img);

    // javascript: URL
    window.__JSURL_RAN__ = false;
    const a = document.createElement('a');
    a.href = 'javascript:window.__JSURL_RAN__=true';
    document.body.appendChild(a);
    try { a.click(); } catch (e) { /* expected to be refused */ }

    // cross-origin fetch (connect-src 'self')
    r.remoteFetchBlocked = 'see-violations';

    // cross-origin frame (frame-src is youtube-only)
    window.__FRAME_RAN__ = false;
    const f = document.createElement('iframe');
    f.src = 'https://example.com/';
    f.onload = () => { window.__FRAME_RAN__ = true; };
    document.body.appendChild(f);

    // cross-origin image on a non-allow-listed host (img-src pins i.ytimg.com)
    const bad = document.createElement('img');
    bad.src = 'https://example.com/pixel.png';
    document.body.appendChild(bad);

    // cross-origin stylesheet (style-src 'self')
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = 'https://example.com/x.css';
    document.head.appendChild(link);

    return r;
};

// Separate, standalone check so the injection tests above cannot perturb it.
// Mirrors frontend/scout.js:636 (setupYouTubePlayer) exactly and proves
// frame-src really permits the embed the Scout tab depends on.
window.__FIXER_E_YT__ = function () {
    const out = { ytApiLoaded: typeof window.YT === 'object' && window.YT !== null };
    if (!out.ytApiLoaded) return out;
    try {
        window.__p = new window.YT.Player('scoutYoutubePlayer', {
            videoId: 'dQw4w9WgXcQ',
            playerVars: { autoplay: 0, enablejsapi: 1, rel: 0, modestbranding: 1 }
        });
        out.playerInstantiated = true;
    } catch (e) {
        out.playerError = e.message;
    }
    return out;
};

window.__FIXER_E_YT_DEFERRED__ = function () {
    const f = document.querySelector('#scoutYoutubePlayer iframe')
        || document.querySelector('iframe[src*="youtube"]');
    return {
        embedIframeCreated: !!f,
        embedIframeSrc: f ? String(f.src).slice(0, 90) : null,
        playerHasSeekApi: !!(window.__p && typeof window.__p.seekTo === 'function')
    };
};

// Resolve the deferred checks a moment after the report is requested.
window.__FIXER_E_REPORT_DEFERRED__ = function () {
    return {
        inlineHandlerBlocked: window.__HANDLER_RAN__ !== true,
        jsUrlBlocked: window.__JSURL_RAN__ !== true,
        crossOriginFrameBlocked: window.__FRAME_RAN__ !== true
    };
};
