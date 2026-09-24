#!/usr/bin/env node
// CODE-4 repro: CODE-07 -- the two anonymous setTimeouts in
// repertoire_trainer.js:onUserAttempt() are not tracked, so
// startNewDrill() cannot cancel them and they mutate the board/drill of the
// position that has since replaced the one they belong to.
//
// A pure state-machine model of the real control flow; no DOM, no backend.
//
// Run:  node audit/scripts/CODE4_trainer_timer_repro.js

let failures = 0;
const check = (n, c, d) => {
    console.log((c ? '  [FAIL-REPRODUCED] ' : '  [no-repro]        ') + n +
                (d ? ' -> ' + d : ''));
    if (c) failures++;
};

const tick = (ms) => new Promise(r => setTimeout(r, ms));

// ---- model of the real object (frontend/repertoire_trainer.js) ----------
class Trainer {
    constructor() {
        this.currentDrill = null;
        this.currentFen = 'startpos';
        this.isWaitingForUser = false;
        this.boardFen = 'startpos';
        this.pending = [];          // anonymous timers, NOT tracked
        this.autoNextTimeout = null;   // the only tracked one
        this.moves = [];            // log of board mutations
    }
    // startNewDrill(): repertoire_trainer.js:370-513
    async startNewDrill(drill) {
        this.isWaitingForUser = false;
        this.currentDrill = null;
        this.currentFen = drill.parent_fen;
        this.boardFen = drill.parent_fen;
        clearTimeout(this.autoNextTimeout);   // <-- the ONLY clear
        await tick(5);
        this.currentDrill = drill;
        this.isWaitingForUser = true;
    }
    // the 550ms opponent-reply timer: repertoire_trainer.js:701-804
    scheduleOpponentReply(reply, next) {
        this.pending.push(setTimeout(async () => {
            // this.board.playMove(oppFrom, oppTo, oppPromo)
            const legal = this.boardFen === next.parent_fen;   // "is it legal here?"
            if (legal) { this.boardFen = next.parent_fen; this.moves.push('played ' + reply.uci); }
            else { this.moves.push('playMove REJECTED (' + reply.uci + ' not legal)'); }
            this.currentFen = this.boardFen;                  // <-- overwrites
            if (this.currentDrill) {
                this.currentDrill.last_move = { from: reply.uci.slice(0, 2), to: reply.uci.slice(2, 4) };
                if (next.expected_uci) {                      // <-- metadata corruption
                    this.currentDrill.expected_uci = next.expected_uci;
                    this.moves.push('drill.expected_uci := ' + next.expected_uci);
                }
            }
        }, 550));
    }
    // the 800ms mistake-reset timer: repertoire_trainer.js:863-872
    scheduleMistakeReset(expectedUci) {
        this.pending.push(setTimeout(() => {
            this.boardFen = this.currentFen;                   // <-- NEW drill's fen
            if (expectedUci) this.moves.push('arrow ' + expectedUci +
                ' painted on ' + this.boardFen.slice(0, 24) + '...');
        }, 800));
    }
}

(async () => {
    // Drill A: 1.e4 position, opponent replies ...c5
    const drillA = { id: 'A', parent_fen: 'FEN_after_1e4' };
    const replyA = { uci: 'c7c5', san: 'c5' };
    const nextA = { parent_fen: 'FEN_after_1e4_c5', expected_uci: 'g1f3' };

    // Drill B (loaded 200ms later): a completely unrelated position where
    // ...c5 happens to be legal.
    const drillB = { id: 'B', parent_fen: 'FEN_after_1d4_d5' };

    console.log('(1) answer correctly in drill A, then hit "New Position / Line"');
    const t = new Trainer();
    await t.startNewDrill(drillA);
    t.scheduleOpponentReply(replyA, nextA);
    await tick(200);                       // user clicks within the 550ms window
    await t.startNewDrill(drillB);
    console.log('    pending timers still alive:', t.pending.length,
                '| tracked autoNextTimeout:', t.autoNextTimeout);
    await tick(600);                       // let the stale timer fire
    console.log('    board mutations:', JSON.stringify(t.moves));
    console.log('    currentDrill is now:', t.currentDrill.id);
    check('stale 550ms timer fired against the NEW drill',
        t.moves.length > 0,
        'board mutations after the switch: ' + JSON.stringify(t.moves));
    check('stale timer wrote its metadata onto the new drill',
        t.currentDrill.expected_uci === 'g1f3',
        "new drill B's expected_uci = " + t.currentDrill.expected_uci +
        " (belongs to drill A)");

    console.log('\n(2) blunder, then hit "New Position / Line" inside 800ms');
    const t2 = new Trainer();
    await t2.startNewDrill(drillA);
    t2.scheduleMistakeReset('e2e4');
    await tick(100);
    await t2.startNewDrill(drillB);
    await tick(900);
    console.log('    board mutations:', JSON.stringify(t2.moves));
    check('stale 800ms timer paints drill A\'s arrow on drill B\'s position',
        t2.moves.some(m => m.includes('arrow e2e4')),
        JSON.stringify(t2.moves));

    console.log('\n(3) the ONLY timer startNewDrill() cancels is autoNextTimeout');
    let trackedFired = false;
    const t3 = new Trainer();
    t3.autoNextTimeout = setTimeout(() => { trackedFired = true; }, 900);
    t3.scheduleMistakeReset('g1f3');
    await t3.startNewDrill(drillB);
    await tick(1000);
    console.log('    tracked autoNextTimeout fired?  ', trackedFired, '(cleared at line 386)');
    console.log('    anonymous 800ms timer survived?', t3.moves.length > 0,
                JSON.stringify(t3.moves));
    console.log('    and this.autoNextTimeout is left dangling (never set to null):',
                t3.autoNextTimeout !== null && t3.autoNextTimeout !== undefined);
    check('anonymous timers are unreachable by any clearTimeout',
        trackedFired === false && t3.moves.length > 0,
        'clearTimeout(this.autoNextTimeout) at repertoire_trainer.js:386 is the ' +
        'only cancellation in the file; the 550ms/800ms handles are anonymous');

    console.log('\nfailures reproduced: ' + failures);
    process.exit(0);
})();
