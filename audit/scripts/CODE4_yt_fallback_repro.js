#!/usr/bin/env node
// CODE-4: reproduction for CODE-11 (YouTube iframe API not ready on first
// open -> chapter seeking dead for the session) and CODE-18-adjacent
// "no retry" behaviour. A pure logic model of scout.js:setupYouTubePlayer() /
// seekVideoTo(); no browser, no network.
//
// Run:  node audit/scripts/CODE4_yt_fallback_repro.js

let failures = 0;
const check = (n, c, d) => {
    console.log((c ? '  [FAIL-REPRODUCED] ' : '  [no-repro]        ') + n +
                (d ? ' -> ' + d : ''));
    if (c) failures++;
};

// ---- minimal stand-ins -------------------------------------------------
const win = { YT: undefined };            // iframe_api has not finished loading
const dom = {
    scoutYoutubePlayer: {
        _html: '',
        set innerHTML(v) { this._html = v; },
        get innerHTML() { return this._html; }
    }
};

class Scout {
    constructor() { this.ytPlayer = null; this.ytPlayerReady = false; this.seeks = []; }
    // verbatim structure of frontend/scout.js:630-658
    setupYouTubePlayer(videoId) {
        if (win.YT && win.YT.Player) {
            if (this.ytPlayer && typeof this.ytPlayer.loadVideoById === 'function') {
                this.ytPlayer.loadVideoById(videoId);
                this.ytPlayerReady = true;              // optimistic, pre-load
            } else {
                this.ytPlayer = new win.YT.Player('scoutYoutubePlayer', { videoId });
            }
        } else {
            const playerEl = dom.scoutYoutubePlayer;
            if (playerEl) {
                playerEl.innerHTML =
                    `<iframe id="scoutIframeFallback" src="https://www.youtube.com/embed/${videoId}?enablejsapi=1" allowfullscreen></iframe>`;
            }
        }
    }
    // verbatim structure of frontend/scout.js:660-664
    seekVideoTo(seconds) {
        if (this.ytPlayer && this.ytPlayerReady &&
            typeof this.ytPlayer.seekTo === 'function') {
            this.ytPlayer.seekTo(seconds, true);
            this.seeks.push(seconds);
        }
    }
}

console.log('(1) first study opened while window.YT is still undefined');
const s = new Scout();
s.setupYouTubePlayer('dQw4w9WgXcQ');
console.log('    window.YT                =', win.YT);
console.log('    ytPlayer                 =', s.ytPlayer);
console.log('    ytPlayerReady            =', s.ytPlayerReady);
console.log('    fallback iframe injected =',
    /scoutIframeFallback/.test(dom.scoutYoutubePlayer.innerHTML));

// ...the iframe API finishes loading a moment later, as it always does
win.YT = { Player: function (id, o) {
    this.loadVideoById = () => {}; this.seekTo = () => {};
    this.id = id; this.opts = o;
} };

console.log('\n(2) user clicks a chapter in #scoutChapterSelect');
s.seekVideoTo(212.5);
console.log('    seeks issued =', JSON.stringify(s.seeks));
check('seekVideoTo is a permanent no-op after the fallback path',
    s.seeks.length === 0 && s.ytPlayer === null,
    'ytPlayer stayed null for the whole session; 0 seeks issued');

console.log('\n(3) a SECOND study is opened (window.YT is now available)');
s.setupYouTubePlayer('abcdefghijk');
console.log('    ytPlayer created =', !!s.ytPlayer);
console.log('    the stale #scoutIframeFallback is still in the DOM =',
    /scoutIframeFallback/.test(dom.scoutYoutubePlayer.innerHTML));
check('the fallback iframe is never removed',
    /scoutIframeFallback/.test(dom.scoutYoutubePlayer.innerHTML),
    'scout.js never deletes #scoutIframeFallback; YT.Player is then built on a ' +
    'div that still contains it');

console.log('\n(4) optimistic ytPlayerReady on loadVideoById (fast path)');
const s2 = new Scout();
win.YT = { Player: function () {
    this.loadVideoById = function (v) { this.loading = v; };  // async in reality
    this.seekTo = function (t) { s2.seeks.push('seek@' + t + ' while loading ' + this.loading); };
} };
s2.setupYouTubePlayer('aaaaaaaaaaa');
s2.setupYouTubePlayer('bbbbbbbbbbb');
s2.seekVideoTo(60);
console.log('    ' + JSON.stringify(s2.seeks));
check('seekTo is called against a player that has not finished loading',
    s2.seeks.length === 1,
    'scout.js:634 sets ytPlayerReady=true immediately after loadVideoById()');

console.log('\nfailures reproduced: ' + failures);
process.exit(0);
