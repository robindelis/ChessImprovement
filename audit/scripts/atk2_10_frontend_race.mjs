/*
 * ATTACKER-2 (TRAINER) -- frontend harness for frontend/repertoire_trainer.js
 *
 * Loads the real trainer class with a minimal DOM stub + a faithful board stub
 * (real chess.js move application, board.js's _getSquareCenterPercent copied
 * verbatim) and drives it against the throwaway backend on 127.0.0.1:39311
 * (CHESS_DB_PATH=/tmp/atk2_ui.db).  Port 38281 / the production DB are untouched.
 *
 * Run:  node audit/scripts/atk2_10_frontend_race.mjs
 */
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { Chess } = require('../../frontend/vendor/chess.min.js');

const BASE = 'http://127.0.0.1:39311';
const PY = '/home/robin/miniconda3/bin/python3';
const DB = '/tmp/atk2_ui.db';
const ROOT = new URL('../../', import.meta.url).pathname;

// Bring up a THROWAWAY backend (never 38281, never the production DB).
for (const f of [DB, DB + '-wal', DB + '-shm']) { try { fs.unlinkSync(f); } catch { } }
const server = spawn(PY, ['-m', 'uvicorn', 'backend.api:app', '--host', '127.0.0.1',
  '--port', '39311', '--log-level', 'error'],
  { cwd: ROOT, env: { ...process.env, CHESS_DB_PATH: DB }, stdio: 'ignore' });
const bye = () => { try { server.kill('SIGTERM'); } catch { } };
process.on('exit', bye);
for (let i = 0; i < 60; i++) {
  try { const r = await fetch(`${BASE}/api/repertoire/train/openings`); if (r.ok) break; } catch { }
  await new Promise(r => setTimeout(r, 250));
}
console.log(`throwaway backend up on ${BASE} (CHESS_DB_PATH=${DB})`);

const SRC = fs.readFileSync(new URL('../../frontend/repertoire_trainer.js', import.meta.url), 'utf8')
    .replace(/window\.repertoireTrainer\s*=\s*new RepertoireTrainer\(\);?\s*$/, '');

// ------------------------------------------------------------------ DOM stub
const els = new Map();
function mkEl(id) {
  const el = {
    id, style: {}, _text: '', _html: '', value: '', title: '', disabled: false,
    options: [], children: [],
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
    },
    addEventListener() {}, appendChild(c) { this.children.push(c); this.options.push(c); },
    setAttribute() {}, focus() {}, querySelector() { return null; },
    get textContent() { return this._text; }, set textContent(v) { this._text = String(v); },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this.options = []; this.children = []; },
  };
  return el;
}
const document = {
  getElementById(id) { if (!els.has(id)) els.set(id, mkEl(id)); return els.get(id); },
  createElement(tag) { return mkEl('<' + tag + '>'); },
  createElementNS(ns, tag) { return mkEl('<' + tag + '>'); },
};
els.get('viewRepTrainer') || document.getElementById('viewRepTrainer');
document.getElementById('viewRepTrainer').classList.add('active');

// --------------------------------------------------------------- board stub
class Board {
  constructor(el, opts) {
    this.el = el; this.options = opts || {}; this.chess = new Chess();
    this.arrows = []; this.lastMove = null; this.selectedSquare = null;
    this.svgEl = mkEl('svg');
  }
  setOrientation(o) { this.options.orientation = o; }
  loadFEN(fen) { this.chess.load(fen); this.lastMove = null; }
  getFEN() { return this.chess.fen(); }
  setLastMove(f, t) { this.lastMove = { from: f, to: t }; }
  setArrows(a) { this.arrows = Array.isArray(a) ? [...a] : []; this._renderArrows(); }
  addArrow(f, t, c) { this.arrows.push({ from: f, to: t, color: c }); this._renderArrows(); }
  clearArrows() { this.arrows = []; this._renderArrows(); }
  // board.js:588 verbatim
  _getSquareCenterPercent(sq) {
    const file = sq.charCodeAt(0) - 97;
    const rank = parseInt(sq[1], 10);
    return { x: (file + 0.5) * 12.5, y: (8 - rank + 0.5) * 12.5 };
  }
  _renderArrows() { this.rendered = (this.rendered || 0) + 1; for (const a of this.arrows) this._getSquareCenterPercent(a.from); }
  playMove(f, t, promo) {
    const res = this.chess.move({ from: f, to: t, ...(promo ? { promotion: promo } : {}) });
    if (!res) return null;              // board.js:522  (silent failure)
    this.lastMove = { from: f, to: t };
    return res;
  }
  render() {}
}
const InteractiveChessboard = Board;

// ----------------------------------------------------------------- globals
const toasts = [];
const realFetch = globalThis.fetch;
let fetchLog = [];
let delayNextDrill = 0;
const window_ = {
  showToast: (m, c, d) => toasts.push({ m, c, d }),
  addEventListener: () => {},
  setTimeout, clearTimeout,
};
async function fetchShim(url, opts) {
  const t0 = Date.now();
  let res = await realFetch(url.startsWith('http') ? url : BASE + url, opts);
  const clone = res.clone();
  const body = await clone.json().catch(() => null);
  if (String(url).includes('/train/next') && delayNextDrill > 0) {
    const d = delayNextDrill; delayNextDrill = 0;
    await new Promise(r => setTimeout(r, d));
    console.log(`   [fetch] ${url} resolved +${d}ms AFTER later requests`);
  }
  fetchLog.push({ url: String(url), ms: Date.now() - t0, body });
  return res;
}
const sandboxGlobals = {
  document, window: window_, fetch: fetchShim, InteractiveChessboard,
  console, setTimeout, clearTimeout, setInterval, clearInterval, alert: (m) => toasts.push({ m, alert: true }),
  fetchShim,
};
const makeTrainer = new Function(
  'document', 'window', 'fetch', 'InteractiveChessboard', 'console', 'setTimeout',
  'clearTimeout', 'setInterval', 'clearInterval', 'alert',
  SRC + '\nreturn RepertoireTrainer;');
const RepertoireTrainer = makeTrainer(
  document, window_, fetchShim, InteractiveChessboard, console, setTimeout,
  clearTimeout, setInterval, clearInterval, sandboxGlobals.alert);

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const hdr = (t) => console.log('\n' + '='.repeat(78) + '\n' + t + '\n' + '='.repeat(78));
const api = async (p, o) => (await realFetch(BASE + p, o)).json();
const attemptCount = async (uci) => {
  // read the raw counters out of the temp DB through the progress endpoint
  return api('/api/repertoire/train/progress?color=white');
};

async function reset() {
  for (const col of ['white', 'black']) {
    await realFetch(`${BASE}/api/repertoire/clear?repertoire=${col}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  }
  for (const [study, sans] of [['RaceWhite', ['e4', 'e5', 'Nf3', 'Nc6', 'Bb5']],
    ['RaceBlack', ['e4', 'c5', 'd4', 'd6']]]) {
    const b = new Chess();
    for (const san of sans) {
      const parent = b.fen();
      const turn = b.turn();
      const mv = b.move(san);
      await realFetch(`${BASE}/api/repertoire/move`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          repertoire: study === 'RaceWhite' ? 'white' : 'black', parent_fen: parent,
          move_uci: mv.from + mv.to + (mv.promotion || ''),
          is_user_move: (study === 'RaceWhite') ? (turn === 'w') : (turn === 'b'),
          study_name: study, popularity_pct: 50.0,
        }),
      });
    }
  }
}

// =========================================================== scenario 1
hdr('1) DOUBLE SUBMIT: the board stays interactive while /train/attempt is in flight');
await reset();
const t1 = new RepertoireTrainer();
t1.init();
await t1.startNewDrill();
console.log('   drill:', t1.currentDrill.expected_san, ' isWaitingForUser =', t1.isWaitingForUser);
fetchLog = [];
const p1 = t1.onUserAttempt('e2', 'e4', undefined, { from: 'e2', to: 'e4' });
console.log('   -> move played, POST in flight; isWaitingForUser is STILL',
  t1.isWaitingForUser, '(onUserAttempt only clears it after the await)');
// the 'h'/ArrowLeft shortcut, or a fast second drag, while the POST is in flight
t1.retryCurrentPosition();
console.log('   -> retryCurrentPosition() (bound to the h / ArrowLeft key) re-armed input:',
  t1.isWaitingForUser);
t1.onUserAttempt('e2', 'e4', undefined, { from: 'e2', to: 'e4' });
await p1; await sleep(700);
console.log('   POSTs issued for ONE user move:', fetchLog.filter(f => f.url.includes('/train/attempt')).length);
console.log('   responses:', fetchLog.filter(f => f.url.includes('/train/attempt'))
  .map(f => ({ correct: f.body.correct, tp: f.body.times_practiced, tc: f.body.times_correct })));
const pr = await attemptCount();
console.log('   server progress for white:', { m: pr.mastered_count, l: pr.learning_count, u: pr.unlearned_count });

// =========================================================== scenario 2
hdr('2) STALE RESPONSE WINS: switchColor while the first /train/next is still in flight');
await reset();
const t2 = new RepertoireTrainer();
t2.init();
delayNextDrill = 700;                       // the white drill will be slow
const whiteDrill = t2.startNewDrill();     // do not await
await sleep(120);                          // white fetch is now pending
const blackDrill = t2.switchColor('black');// user clicks the BLACK button
await Promise.all([whiteDrill, blackDrill]);
console.log('   activeColor          :', t2.activeColor);
console.log('   currentDrill.study   :', t2.currentDrill.study_name);
console.log('   currentDrill.expected:', t2.currentDrill.expected_san, t2.currentDrill.expected_uci);
console.log('   board FEN            :', t2.board.getFEN());
console.log('   isWaitingForUser     :', t2.isWaitingForUser);
console.log('   --> the stale WHITE drill is now served while the user is training BLACK');
fetchLog = [];
await t2.onUserAttempt('e2', 'e4', undefined, { from: 'e2', to: 'e4' });
const last = fetchLog.find(f => f.url.includes('/train/attempt'));
console.log('   user plays 1.e4 ->', last.body.message, '| is_in_tree =', last.body.is_in_tree);
console.log('   frontend streak =', t2.streak, ' practicedCount =', t2.practicedCount,
  ' sessionMistakes =', t2.sessionMistakes);

// =========================================================== scenario 3
hdr('3) CLS: conditional toolbar controls toggled with display:none / inline-flex');
await reset();
const t3 = new RepertoireTrainer();
t3.init();
await t3.startNewDrill();
const badge = document.getElementById('trainImportantBadge');
const arrowsBtn = document.getElementById('btnTrainToggleArrows');
console.log('   #trainImportantBadge display after a NON-important drill:', JSON.stringify(badge.style.display));
// now mark 1.e4 important + give it arrows, and re-enter the drill
await realFetch(`${BASE}/api/repertoire/move_meta`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ repertoire: 'white', parent_fen: new Chess().fen(), move_uci: 'e2e4',
    is_important: true, arrows: '[{"from":"e2","to":"e4","color":"#22c55e"}]' }) });
await t3.startNewDrill();
console.log('   #trainImportantBadge display after an IMPORTANT drill  :', JSON.stringify(badge.style.display));
console.log('   #btnTrainToggleArrows display                          :', JSON.stringify(arrowsBtn.style.display));
console.log('   board arrows drawn BEFORE the user attempts anything  :', JSON.stringify(t3.board.arrows));
console.log('   (AGENTS.md 8.4 forbids display toggling in fixed toolbars)');

// =========================================================== scenario 4
hdr('4) malformed `arrows` JSON kills the drill (board.js:589 has no guard)');
await realFetch(`${BASE}/api/repertoire/move_meta`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ repertoire: 'white', parent_fen: new Chess().fen(), move_uci: 'e2e4',
    arrows: '[{"note":"e2-e4"}]' }) });
const t4 = new RepertoireTrainer();
t4.init();
await t4.startNewDrill();
console.log('   currentDrill        :', t4.currentDrill && t4.currentDrill.expected_san);
console.log('   isWaitingForUser    :', t4.isWaitingForUser, '  <-- input never enabled');
console.log('   hint button disabled:', document.getElementById('btnTrainHint').disabled);
console.log('   arrows that were stored:', (await api('/api/repertoire/train/next?color=white')).arrows);
console.log('   the exception is swallowed by the try/catch in startNewDrill() (line 510)');

hdr('toasts raised (window.showToast) instead of in-flow notices:');
console.log('  ', JSON.stringify(toasts));
process.exit(0);
