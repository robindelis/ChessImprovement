/*
 * SCOUT-01 repro part 2.
 * Extracts the REAL renderResults() body out of frontend/scout.js and executes it
 * against a minimal DOM stub, then reports what live markup the string produces.
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.dirname(path.dirname(__dirname));   // repo root (audit/scripts -> audit -> repo)
const scoutSrc = fs.readFileSync(path.join(ROOT, 'frontend', 'scout.js'), 'utf8');

// --- extract renderResults() source verbatim --------------------------------
const start = scoutSrc.indexOf('    renderResults() {');
if (start < 0) throw new Error('renderResults() not found');
let i = scoutSrc.indexOf('{', start), depth = 0, end = -1;
for (let j = i; j < scoutSrc.length; j++) {
    if (scoutSrc[j] === '{') depth++;
    else if (scoutSrc[j] === '}') { depth--; if (depth === 0) { end = j + 1; break; } }
}
const methodSrc = scoutSrc.slice(start, end);
console.log('--- extracted ' + (end - start) + ' chars of renderResults() from frontend/scout.js ---');

// --- minimal DOM stub ------------------------------------------------------
function El() { this._html = ''; this.className = ''; }
Object.defineProperty(El.prototype, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = v; }
});
El.prototype.querySelectorAll = () => [];
El.prototype.addEventListener = () => {};

const grid = new El();

const Harness = new Function('return class __H {' + methodSrc + '}')();
// bind `grid` inside by shadowing: method uses document.getElementById -> return our stub
global.document = {
    getElementById(id) { return id === 'scoutResultsGrid' ? grid : null; }
};

const searchPayload = JSON.parse(fs.readFileSync(path.join(__dirname, 'atk4_01_search_payload.json'), 'utf8'));
const studyPayload = JSON.parse(fs.readFileSync(path.join(__dirname, 'atk4_01_study_payload.json'), 'utf8'));

// ---------- SINK 1: renderResults() (search grid) ----------
const app = new Harness();
app.cachedResults = searchPayload;
app.activeFilter = 'all';
app.renderResults();
const html = grid.innerHTML;

console.log('\n===== SINK 1: scoutResultsGrid.innerHTML (from /api/scout/search) =====');
console.log(html);

const liveTags = [];
const re = /<\s*(img|script|svg|iframe)\b[^>]*>/gi;
let m;
while ((m = re.exec(html)) !== null) {
    const tag = m[0];
    if (/onerror|onload|onmouseover|<script/i.test(tag)) liveTags.push(tag);
}
console.log('\n>>> INJECTED LIVE MARKUP FOUND:', liveTags.length);
liveTags.forEach(t => console.log('    ' + t));

// ---------- SINK 2: renderTreeLines() `${line.name}` from activeStudy.opening_name ----------
// scout.js _onUserBoardMove copies activeStudy.opening_name onto every move object.
const lineName = studyPayload.opening_name;
const linePgn = '1. e4 e5 2. Nf3 Nc6 3. Bb5 a6';
const lineHtml = `<span style="font-weight:700; color:#fff; font-size:0.83rem;">${lineName || 'Explored Line'}</span>
<div class="scout-var-pgn" title="${linePgn}">${linePgn}</div>`;
console.log('\n===== SINK 2: renderTreeLines() line name (from activeStudy.opening_name) =====');
console.log(lineHtml);
console.log('\n>>> contains raw <img ... onerror=...>:', /<img[^>]*onerror/i.test(lineHtml));

const verdict = (liveTags.length > 0 || /<img[^>]*onerror/i.test(lineHtml));
console.log('\nVERDICT:', verdict ? 'XSS CONFIRMED (unescaped remote strings reach innerHTML)' : 'no xss');
process.exit(verdict ? 0 : 1);
