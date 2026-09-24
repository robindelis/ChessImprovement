#!/usr/bin/env node
// CODE-4: reproduction for CODE-22 -- the Scout keydown guard omits <select>,
// unlike the Trainer's, so the #scoutChapterSelect dropdown cannot be operated
// with the keyboard.
//
// Run:  node audit/scripts/CODE4_keyboard_repro.js
const fs = require('fs');
const path = require('path');

let failures = 0;

function makeHandler(guardTags, nav) {
    return function (e) {
        const tag = e.target.tagName.toLowerCase();
        if (guardTags.includes(tag)) return 'ignored (focus is a form control)';
        if (['ArrowLeft', 'h'].includes(e.key)) { e.preventDefault(); nav.push('prevStep'); return 'prevStep'; }
        if (['ArrowRight', 'l'].includes(e.key)) { e.preventDefault(); nav.push('nextStep'); return 'nextStep'; }
        if (['Home', 'ArrowUp', 'k'].includes(e.key)) { e.preventDefault(); nav.push('firstStep'); return 'firstStep'; }
        if (['End', 'ArrowDown', 'j'].includes(e.key)) { e.preventDefault(); nav.push('lastStep'); return 'lastStep'; }
        if (e.key === 'u' || (e.ctrlKey && e.key === 'z')) { e.preventDefault(); nav.push('undoUserMove'); return 'undoUserMove'; }
        return 'pass-through';
    };
}

const SELECT = () => ({ tagName: 'SELECT', value: 'B10' });
const ev = (key, extra = {}) => ({ key, target: SELECT(), preventDefault() { this.defaultPrevented = true; }, ...extra });

console.log('(1) ArrowDown with #scoutChapterSelect focused');
const scoutNav = [];
const scout = makeHandler(['input', 'textarea'], scoutNav);   // frontend/scout.js:325
let e1 = ev('ArrowDown');
const r1 = scout(e1);
console.log('    scout.js handler ->', r1, '| board nav calls:', JSON.stringify(scoutNav),
            '| select value still', SELECT().value, '| defaultPrevented =', !!e1.defaultPrevented);
if (scoutNav.length) { console.log('  [FAIL-REPRODUCED] the <select> value change is cancelled and the board jumps instead'); failures++; }

console.log('\n(2) same key, Trainer handler (frontend/repertoire_trainer.js:118-121)');
const trainerNav = [];
const trainer = makeHandler(['input', 'textarea', 'select'], trainerNav);
const r2 = trainer(ev('ArrowDown'));
console.log('    trainer handler ->', r2, '| nav calls:', JSON.stringify(trainerNav));
if (!trainerNav.length) console.log('    [ok] correctly ignored -- the two handlers diverged');

console.log('\n(3) Ctrl/Cmd+Z anywhere in the Scout tab');
const nav3 = [];
const h3 = makeHandler(['input', 'textarea'], nav3);
const e3 = ev('z', { ctrlKey: true });
h3(e3);
console.log('    result ->', JSON.stringify(nav3), '| defaultPrevented =', !!e3.defaultPrevented);
if (nav3.includes('undoUserMove')) { console.log('  [FAIL-REPRODUCED] native undo/redo is swallowed by the Scout view'); failures++; }

console.log('\nfailures reproduced: ' + failures);
process.exit(0);
