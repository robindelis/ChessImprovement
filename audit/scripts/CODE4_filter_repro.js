#!/usr/bin/env node
// CODE-4: reproduction for CODE-16 -- the four `.scout-filter-btn` values in
// index.html vs the three literals tested in renderResults().
//
// Run:  node audit/scripts/CODE4_filter_repro.js
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(
    path.join(__dirname, '..', '..', 'frontend', 'index.html'), 'utf8');
const src = fs.readFileSync(
    path.join(__dirname, '..', '..', 'frontend', 'scout.js'), 'utf8');

const filters = [...html.matchAll(
    /class="scout-filter-btn[^"]*"\s+data-filter="([^"]+)"/g)].map(m => m[1]);
const body = src.slice(src.indexOf('renderResults()'),
                       src.indexOf('async analyzeSource'));
const tested = [...body.matchAll(/this\.activeFilter === '([^']+)'/g)].map(m => m[1]);

let failures = 0;
console.log('data-filter values in index.html :', JSON.stringify(filters));
console.log("literals tested in renderResults :", JSON.stringify(tested));

for (const f of filters) {
    const ok = tested.includes(f) || f === 'all';
    console.log((ok ? '  [ok]        ' : '  [FAIL-REPRODUCED] ') +
                `"${f}" ` + (ok ? 'is handled' : 'falls through to the unfiltered list'));
    if (!ok) failures++;
}
const orphans = tested.filter(t => !filters.includes(t));
if (orphans.length) {
    console.log('  [FAIL-REPRODUCED] unreachable branches (no matching button): ' +
                JSON.stringify(orphans));
    failures++;
}
console.log('\nfailures reproduced: ' + failures);
process.exit(0);
