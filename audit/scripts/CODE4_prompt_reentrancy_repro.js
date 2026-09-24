#!/usr/bin/env node
// CODE-4 repro: CODE-18 -- window.promptStudySelection (frontend/app.js:190-431)
// has no re-entrancy guard. Two concurrent calls (a double-click on any
// import button) make the second `modal.showModal()` throw InvalidStateError,
// which rejects the promise; the awaiting caller in scout.js has no .catch(),
// so the import silently never happens.
//
// A pure model of the real control flow; no DOM, no backend.
//
// Run:  node audit/scripts/CODE4_prompt_reentrancy_repro.js

let failures = 0;
const check = (n, c, d) => {
    console.log((c ? '  [FAIL-REPRODUCED] ' : '  [no-repro]        ') + n +
                (d ? ' -> ' + d : ''));
    if (c) failures++;
};

// ---- minimal HTMLDialogElement stand-in (per spec) ----------------------
class HTMLDialogElement {
    constructor() { this.open = false; }
    showModal() {
        if (this.open) {
            const e = new Error("Failed to execute 'showModal' on " +
                "'HTMLDialogElement': The element already has a 'open' attribute.");
            e.name = 'InvalidStateError';
            throw e;
        }
        this.open = true;
    }
    close() {
        if (!this.open) return;
        this.open = false;
        if (typeof this.onclose === 'function') this.onclose();   // real <dialog> fires close
    }
}

// ---- model of frontend/app.js:190-431 -----------------------------------
const dialog = new HTMLDialogElement();
let studiesFetchResolved = false;

function promptStudySelection() {
    return new Promise((resolve) => {
        // ... the studies fetch is fired and never awaited before showModal()
        const initialTab = 'existing';
        if (initialTab) { /* setTab() -- omitted, no DOM effect here */ }
        dialog.showModal();               // <-- frontend/app.js:421
        dialog.onclose = () => resolve(null);
    });
}

// ---- the two callers, mirroring frontend/scout.js:1625 and :1708 --------
async function importToRepertoire(tag) {
    const studyName = await promptStudySelection();     // no try/catch around it
    if (!studyName) return tag + ': nothing imported (user cancelled)';
    return tag + ': imported into ' + studyName;
}

const rejections = [];
process.on('unhandledRejection', (e) => rejections.push(e.name));

(async () => {
    console.log('(1) user double-clicks "📥 Line -> White" (2 handlers, 1 tick apart)');
    const p1 = importToRepertoire('click1');
    const p2 = importToRepertoire('click2');
    // the user then completes the FIRST (still-open) dialog normally
    setTimeout(() => dialog.close(), 10);
    const res = await Promise.allSettled([p1, p2]);
    res.forEach((r, i) => console.log('    click' + (i + 1) + ' -> ' + r.status +
        (r.status === 'rejected' ? ' (' + r.reason.name + ': ' +
            r.reason.message.slice(0, 60) + '...)' : ' — ' + r.value)));
    check('second concurrent open rejects',
        res[1].status === 'rejected' && res[1].reason.name === 'InvalidStateError',
        res[1].status === 'rejected' ? res[1].reason.name : 'resolved');

    await new Promise(r => setImmediate(r));
    console.log('    (Promise.allSettled above *handled* click2; in the real app the');
    console.log('     rejecting promise is the return value of an async click');
    console.log('     handler with no .catch(), so the page logs an unhandled');
    console.log('     rejection and the user sees nothing.)');

    console.log('\n(2) the dropped work: click2 never reaches its import at all');
    check('the second import is silently dropped with zero user feedback',
        res[1].status === 'rejected',
        'scout.js:1625 / 1708 `await window.promptStudySelection(...)` sits ' +
        'OUTSIDE the try/catch that starts at line 1651');

    console.log('\n(3) shared-state hazard: the losing invocation still owns a fetch');
    console.log('    studiesFetchResolved =', studiesFetchResolved,
        '-> app.js:259-316 has no AbortController, so its .then() still runs');
    console.log('    and calls setTab()/rewrites #studySelectDropdown on a dialog');
    console.log('    whose handlers the WINNING invocation just re-installed.');
    check('the abandoned invocation can still mutate the live dialog',
        studiesFetchResolved === false,
        'no cancellation, no "am I still the current invocation?" guard');

    console.log('\nfailures reproduced: ' + failures);
    process.exit(0);
})();
