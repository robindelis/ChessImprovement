/*
 * audit/scripts/FIXER-J_trainer14_scenario.js
 *
 * TRAINER-14 (attacker ATK-2) end-to-end regression probe.
 *
 * This file is NOT a standalone scenario: `tests/test_builder_xss_contracts.py` concatenates
 * the REAL `frontend/repertoire_trainer.js` source in front of it and hands the result to
 * `tests/frontend_harness.js`, which has already loaded the REAL `frontend/board.js` and
 * `frontend/repertoire_builder.js` into the same Node `vm` context. Nothing is re-implemented
 * and nothing is mocked except the network.
 *
 * The bug being guarded: `repertoire_trainer.js:591-605` parses the persisted
 * `repertoire_moves.arrows` JSON, calls `board.setArrows(...)` at :602 and only then sets
 * `isWaitingForUser = true` at :605 — all inside one `try { … } catch { console.error }` that
 * ends at :609. A `TypeError` thrown out of `setArrows()` therefore left `currentDrill` set and
 * the board loaded while `isWaitingForUser` stayed `false`. `onUserAttempt` (:731) and
 * `showHint` (:663) both gate on `isWaitingForUser`, so the drill was permanently unanswerable
 * and the only way out was "New Drill".
 *
 * Every case below asserts BOTH halves of the fix:
 *   (1) no exception escapes `startNewDrill()`, and
 *   (2) `isWaitingForUser === true`, i.e. line 605 is actually reached.
 */
'use strict';

const results = [];
function t(name, cond, detail) {
    results.push({ name: name, pass: !!cond, detail: cond ? undefined : String(detail) });
}

// The trainer touches many ids; inject every one of them so a lookup can never silently
// degrade into a no-op (the CODE3-03 failure mode). The list is derived from the trainer
// source by the Python wrapper; this literal list is the standalone fallback.
const TRAINER_IDS = (typeof window.__FIXER_J_TRAINER_IDS__ === 'object' && window.__FIXER_J_TRAINER_IDS__)
    || ['trainerBoard', 'btnTrainWhite', 'btnTrainBlack', 'btnTrainNewDrill', 'btnTrainHint',
        'btnTrainFlip', 'btnTrainRetry', 'trainOpeningFilter', 'btnClearTrainFocus',
        'btnTrainToggleArrows', 'trainModeSelect', 'trainFeedback', 'trainColorSpan',
        'trainOpeningName', 'trainOpeningEco', 'trainBreadcrumbMoves', 'trainDrillReasonBadge',
        'trainScopeBadge', 'trainMasteredHeadline', 'masterySegMastered', 'masterySegLearning',
        'masterySegUnlearned', 'trainNoteBox', 'trainImportantBadge', 'trainStatsStreak',
        'trainStatsAccuracy', 'trainStatsPracticed', 'trainFocusBanner', 'trainSessionProgress',
        'trainHintBtn'];
TRAINER_IDS.forEach(function (id) {
    if (id && !document.getElementById(id)) document.byId.set(id, document.createElement('div'));
});

const trainer = window.repertoireTrainer;
t('TRAINER-14: the real RepertoireTrainer was loaded (no re-implementation)',
    !!trainer && typeof trainer.startNewDrill === 'function',
    typeof trainer);

const GOOD_DRILL = {
    id: 7,
    parent_fen: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1',
    expected_uci: 'e7e5',
    expected_san: 'e5',
    path_formatted: '1. e4',
    drill_reason: 'repertoire',
    ply_depth: 1,
    opening_name: "King's Pawn",
    eco: 'C40',
    is_important: 0,
    comment: 'a note',
    last_move: { from: 'e2', to: 'e4' },
    study_name: 'My Study',
    opening_ply: 2
};

/** Each payload is the exact content of a `repertoire_moves.arrows` column. */
const ARROWS_PAYLOADS = [
    ['valid', '[{"from":"e2","to":"e4"}]'],
    ['[null]  (ATK-2 payload)', '[null]'],
    ['[undefined via hole]', '[ , {"from":"e2","to":"e4"} ]'],
    ['out-of-bounds squares', '[{"from":"e2","to":"j9"},{"from":"i4","to":"e7"}]'],
    ['missing "from"', '[{"to":"e5"}]'],
    ['a bare object, not an array', '{"from":"e2","to":"e4"}'],
    ['a nested array', '[["e2","e4"]]'],
    ['non-JSON garbage', 'not json at all {{{'],
    ['an empty string', ''],
    ['a huge string', '[' + new Array(500).join('{"from":"e2","to":"e4"},') + ']'],
    ['a hostile string value', '[{"from":"e2","to":"e4","color":"<img src=x onerror=alert(1)>"}]'],
    ['numbers instead of squares', '[{"from":1,"to":4}]'],
    ['null', 'null']
];

const run = async () => {
    const seenAttempt = [];
    let current = null;

    window.fetch = function (url, opts) {
        const u = String(url);
        if (u.indexOf('/api/repertoire/train/next') !== -1) {
            return Promise.resolve({
                ok: true,
                status: 200,
                json: function () { return Promise.resolve(Object.assign({}, GOOD_DRILL, { arrows: current })); }
            });
        }
        if (u.indexOf('/api/repertoire/train/attempt') !== -1) {
            seenAttempt.push(u);
            return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({}); } });
        }
        return Promise.resolve({
            ok: true, status: 200,
            json: function () { return Promise.resolve({ studies: [], openings: [] }); }
        });
    };

    for (let i = 0; i < ARROWS_PAYLOADS.length; i++) {
        const label = ARROWS_PAYLOADS[i][0];
        current = ARROWS_PAYLOADS[i][1];

        // Fresh trainer state per case so one case cannot mask another.
        trainer.isWaitingForUser = false;
        trainer.currentDrill = null;
        trainer.sessionLapses = [];
        trainer.sessionDrilledIds = [];
        trainer.savedPlanArrows = [];
        trainer.drillToken = 0;

        let threw = null;
        try {
            await trainer.startNewDrill();
        } catch (e) {
            threw = e;
        }
        // Give the two 550ms/800ms continuations a chance to fire (they are token-guarded, so
        // they must be no-ops for a drill that never produced a reply).
        await new Promise(function (r) { setTimeout(r, 5); });

        t('TRAINER-14 [' + label + ']: startNewDrill() does not throw', threw === null,
            threw ? String(threw && threw.stack || threw) : '');

        // THE regression: line 605 must be reached, otherwise the drill is unanswerable.
        t('TRAINER-14 [' + label + ']: the drill becomes answerable (isWaitingForUser === true)',
            trainer.isWaitingForUser === true,
            'isWaitingForUser=' + trainer.isWaitingForUser +
            ' currentDrill=' + (trainer.currentDrill ? 'set' : 'null'));

        t('TRAINER-14 [' + label + ']: the drill payload is loaded and the board is positioned',
            !!trainer.currentDrill && trainer.currentFen === GOOD_DRILL.parent_fen,
            'currentFen=' + trainer.currentFen);

        // Every arrow that reached the board must be a real a1..h8 pair. This is checked
        // defensively so a pre-fix board that lets a `null` entry through is *graded*, not a
        // crash that swallows the rest of the report.
        let arrows = [];
        let arrowsThrew = null;
        try {
            arrows = trainer.board.getArrows();
        } catch (e) {
            arrowsThrew = e;
        }
        const isSquare = function (s) {
            return typeof s === 'string' && s.length === 2 &&
                s.charCodeAt(0) >= 97 && s.charCodeAt(0) <= 104 &&
                s.charCodeAt(1) >= 49 && s.charCodeAt(1) <= 56;
        };
        t('TRAINER-14 [' + label + ']: every rendered arrow is a well-formed a1..h8 pair',
            arrowsThrew === null && Array.isArray(arrows) &&
            arrows.every(function (a) { return !!a && isSquare(a.from) && isSquare(a.to); }),
            arrowsThrew ? String(arrowsThrew) : JSON.stringify(arrows));
        // SEC-J: the persisted colour is a paint value, never markup.
        t('SEC-J/board [' + label + ']: every rendered arrow colour is a real paint value',
            Array.isArray(arrows) && arrows.every(function (a) {
                return !!a && typeof a === 'object' && /^#[0-9a-f]{3,8}$/i.test(String(a.color));
            }),
            JSON.stringify(arrows));
    }

    // And the drill really is playable: `onUserAttempt` is gated on isWaitingForUser, so a
    // successful POST proves the answer path is open.
    current = '[null]';
    trainer.isWaitingForUser = false;
    trainer.currentDrill = null;
    await trainer.startNewDrill();
    seenAttempt.length = 0;
    await trainer.onUserAttempt('e7', 'e5');
    t('TRAINER-14: onUserAttempt() is not short-circuited for a hostile-arrows drill',
        seenAttempt.length === 1,
        'attempt requests issued: ' + seenAttempt.length);

    const allPass = results.every(function (r) { return r.pass; });
    return {
        allPass: allPass,
        total: results.length,
        failures: results.filter(function (r) { return !r.pass; }),
        checks: results
    };
};

const safeRun = async () => {
    try {
        return await run();
    } catch (err) {
        results.push({
            name: 'scenario completed without an exception',
            pass: false,
            detail: String((err && err.stack) || err)
        });
        return {
            allPass: false,
            total: results.length,
            failures: results.filter(function (r) { return !r.pass; }),
            checks: results
        };
    }
};

return safeRun();
