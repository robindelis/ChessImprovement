#!/usr/bin/env node
// CODE-4: reproduction for CODE-19 -- three divergent renderings of the same
// overlap badge, so the text the user sees mutates under them.
//
// Run:  node audit/scripts/CODE4_badge_drift_repro.js
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(
    path.join(__dirname, '..', '..', 'frontend', 'scout.js'), 'utf8');

// AGENTS.md §4E: `⚡ White: 4/8 in Rep (+4 new)`
const AGENTS_PARTIAL = 'White: {m}/{t} in Rep (+{n} new)';
const AGENTS_NEW = 'New Line';

const SUB = { '{m}': 4, '{t}': 8, '{n}': 4 };

// Pull the three "partially_present" template literals straight out of the file.
const partialLiterals = [
    ['renderTreeLines   scout.js:1206',
     '⚡ White: ${wData.matched_plies}/${wData.total_plies} (+${wData.new_plies} new)'],
    ['_applyTreeBadges  scout.js:1547',
     '⚡ White: ${wData.matched_plies}/${wData.total_plies} in Rep (+${wData.new_plies} new)'],
    ['checkActiveLineOverlap getBadge  scout.js:1443',
     '⚡ ${repName}: ${data.matched_plies}/${data.total_plies} (+${data.new_plies} new)']
];

let failures = 0;
const norm = (s) => s
    .replace('${wData.matched_plies}', '{m}').replace('${wData.total_plies}', '{t}')
    .replace('${wData.new_plies}', '{n}')
    .replace('${data.matched_plies}', '{m}').replace('${data.total_plies}', '{t}')
    .replace('${data.new_plies}', '{n}')
    .replace('${repName}', 'White')
    .replace('⚡ ', '');

console.log('AGENTS.md §4E mandates: ' + JSON.stringify(AGENTS_PARTIAL) + '\n');
const seen = new Set();
for (const [name, lit] of partialLiterals) {
    if (!src.includes(lit)) {
        console.log('  [??]        ' + name + ' literal not found verbatim (source drifted)');
        failures++;
        continue;
    }
    const out = norm(lit);
    seen.add(out);
    const ok = out === AGENTS_PARTIAL;
    console.log((ok ? '  [ok]        ' : '  [FAIL-REPRODUCED] ') + name);
    console.log('                renders -> "' + out + '"');
    if (!ok) failures++;
}

console.log('\ndistinct renderings of ONE badge state: ' + seen.size);
if (seen.size > 1) {
    console.log('  [FAIL-REPRODUCED] the badge text mutates under the user: ' +
                'renderTreeLines writes one form, then ~120ms later ' +
                '_applyTreeBadges rewrites it.');
    failures++;
}

// the "new" form
const newLits = [
    '★ White New',                                        // scout.js:1208 / 1549
    '★ ${repName}: New'                                   // scout.js:1445
];
console.log('\nAGENTS.md §4E mandates: ' + JSON.stringify(AGENTS_NEW));
for (const l of newLits) {
    const ok = src.includes(l) ? l.includes('New Line') : false;
    console.log((ok ? '  [ok]        ' : '  [FAIL-REPRODUCED] ') +
                JSON.stringify(l) + ' -> does not match "★ New Line"');
    if (!ok) failures++;
}

console.log('\nfailures reproduced: ' + failures);
process.exit(0);
