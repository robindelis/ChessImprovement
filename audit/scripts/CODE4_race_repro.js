#!/usr/bin/env node
// CODE-4: race / re-entrancy evidence for CODE-05, CODE-06, CODE-18, CODE-21.
//
// A tiny deterministic fake of the two async surfaces involved:
//   * an `OpeningScout.search()`-shaped method with NO generation guard
//   * `window.promptStudySelection` with NO re-entrancy guard
//
// Nothing here touches the filesystem, a database or the network.

function tick(ms) { return new Promise(r => setTimeout(r, ms)); }

// ---------------------------------------------------------------- (1) search()
class UnguardedSearch {
    constructor() { this.cachedResults = []; this.rendered = []; }
    // `latency` stands in for the server-side yt-dlp round trip.
    async search(query, latency, out) {
        out.push('request sent: ' + query);
        const data = { for: query };
        await tick(latency);
        this.cachedResults = data;           // <-- no seq check
        this.rendered.push(query);
        out.push('rendered: ' + query + '  (input box currently shows "' +
                 out.__input + '")');
    }
}

// ---------------------------------------------------------------- (2) prompt()
class FakeDialog {
    constructor() { this.open = false; }
    showModal() {
        if (this.open) {
            const e = new Error('Failed to execute \'showModal\' on \'HTMLDialogElement\': The element already has a \'open\' attribute.');
            e.name = 'InvalidStateError';
            throw e;
        }
        this.open = true;
    }
    close() { this.open = false; }
}

let failures = 0;
function check(name, cond, detail) {
    console.log((cond ? '  [FAIL-REPRODUCED] ' : '  [no-repro]        ') +
                name + (detail ? ' -> ' + detail : ''));
    if (cond) failures++;
}

(async () => {
    // (1) stale search response overwrites a newer one
    console.log('(1) search() with no generation counter');
    {
        const s = new UnguardedSearch();
        const log = [];
        log.__input = 'Sicilian Najdorf';
        // page load fires the default search, then the user types a new one
        const slow = s.search('Caro-Kann Defense', 120, log);   // slow
        const fast = s.search('Sicilian Najdorf', 20, log);     // fast
        await Promise.all([slow, fast]);
        log.forEach(l => console.log('    ' + l));
        console.log('    final rendered =', JSON.stringify(s.rendered),
                    ' input box = "Sicilian Najdorf"');
        check('stale response wins the render',
            s.rendered[s.rendered.length - 1] === 'Caro-Kann Defense',
            'last rendered = ' + s.rendered[s.rendered.length - 1]);
    }

    // (2) positional overlap map after the tree shifts
    console.log('\n(2) treeOverlapMap keyed by positional index');
    {
        const map = { white: {}, black: {} };
        map.white['0'] = { status: 'already_present', matched_plies: 4, total_plies: 4, message: 'line0' };
        map.white['1'] = { status: 'new', new_plies: 3, message: 'line1' };
        // user plays a NEW first move -> line0 is now at index 1
        const after = ['NEW-1st', 'OLD-line0', 'OLD-line1'];
        after.forEach((name, idx) => {
            const d = map.white[String(idx)];
            console.log('    card "' + name + '" (idx ' + idx + ') shows badge for: ' +
                        (d ? d.message : '<none>'));
        });
        check('badges are attached to the wrong line after an insert',
            after[1] === 'OLD-line0' && map.white['1'].message === 'line1',
            'card idx1 is "' + after[1] + '" (should get "line0") but got "' +
            map.white['1'].message + '"; card idx0 is "' + after[0] +
            '" and got "' + map.white['0'].message + '"');
    }

    // (3) promptStudySelection re-entrancy
    console.log('\n(3) promptStudySelection() re-entrancy');
    {
        const dialog = new FakeDialog();
        const caller = async (n) => {
            // the shape of frontend/scout.js:1625
            const name = await promptStudySelection(dialog);
            if (!name) return n + ': cancelled';
            return n + ': imported as ' + name;
        };
        async function promptStudySelection(dlg) {
            if (dlg.open) { /* falls through to the real showModal() below */ }
            dlg.showModal();          // throws on the 2nd concurrent call
            return 'Study';
        }
        let unhandled = null;
        process.once('unhandledRejection', (e) => { unhandled = e; });
        const p1 = caller('click1');
        const p2 = caller('click2');
        const res = await Promise.allSettled([p1, p2]);
        res.forEach((r, i) => console.log('    click' + (i + 1) + ' ->', r.status,
            r.status === 'rejected' ? '(' + r.reason.name + ')' : r.value));
        check('second concurrent open rejects (unhandled in the real app)',
            res[1].status === 'rejected',
            res[1].status === 'rejected' ? res[1].reason.name : 'resolved');
        if (unhandled) console.log('    unhandledRejection:', unhandled.name);
    }

    console.log('\nfailures reproduced: ' + failures);
    process.exit(0);
})();
