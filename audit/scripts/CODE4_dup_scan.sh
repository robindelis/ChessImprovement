#!/usr/bin/env bash
# CODE-4: duplication scan between frontend/scout.js and
# frontend/repertoire_builder.js (evidence for DUP-01).
# Read-only, idempotent, no DB / no network.
set -uo pipefail
cd "$(dirname "$0")/../.."

echo "=== 1) the two SessionTreeNode / SessionLineTree definitions"
grep -n "class SessionTreeNode\|class SessionLineTree\|window.SessionTreeNode\|window.SessionLineTree\|typeof SessionTreeNode === 'undefined'\|typeof SessionLineTree === 'undefined'" \
     frontend/repertoire_builder.js frontend/scout.js

echo
echo "=== 2) script order in index.html (decides which class wins)"
grep -n 'script src="/\(repertoire_builder\|scout\|repertoire_trainer\|app\)' frontend/index.html

echo
echo "=== 3) addMove metadata sync: fields handled in each copy"
for f in frontend/repertoire_builder.js frontend/scout.js; do
  printf '  %-32s ' "$f"
  for k in san opening_name eco comment is_important arrows; do
    n=$(awk '/^ *addMove\(moveObj\)/,/^ *stepBack\(\)/' "$f" | grep -c "child.move.$k")
    printf '%s=%s ' "$k" "$n"
  done
  echo
done

echo
echo "=== 4) getAllLines: does the copy track studyName / lineName?"
grep -n "studyName: studyName\|node.lineName" frontend/scout.js frontend/repertoire_builder.js

echo
echo "=== 5) getAllLines DFS guard"
grep -n "if (child && child.move)" frontend/scout.js frontend/repertoire_builder.js

echo
echo "=== 6) method-level duplication (line counts of the forked clusters)"
python3 - <<'PY'
import re
sb = open('frontend/scout.js').read().split('\n')
rb = open('frontend/repertoire_builder.js').read().split('\n')

def span(lines, start_pat, end_pat, frm=0):
    s = next((i for i in range(frm, len(lines)) if re.search(start_pat, lines[i])), None)
    if s is None: return None
    e = next((i for i in range(s + 1, len(lines)) if re.search(end_pat, lines[i])), None)
    return (s + 1, (e + 1) if e else len(lines))

clusters = [
    ('SessionTreeNode+SessionLineTree',
     (r'^class SessionTreeNode', r'^class OpeningScout'),
     (r'^if \(typeof SessionTreeNode', r'^class RepertoireBuilder')),
    ('loadStudies',
     (r'^    async loadStudies\(\)', r'^    (async )?on\w+'),
     (r'^    async loadStudies\(\)', r'^    (async )?on\w+')),
    ('updateActiveLineCounts',
     (r'^    updateActiveLineCounts\(\)', r'^    scheduleOverlapCheck'),
     (r'^    updateActiveLineCounts\(\)', r'^    scheduleOverlapCheck')),
    ('scheduleOverlapCheck',
     (r'^    scheduleOverlapCheck\(\)', r'^    async checkActiveLineOverlap'),
     (r'^    scheduleOverlapCheck\(\)', r'^    async checkActiveLineOverlap')),
    ('checkActiveLineOverlap',
     (r'^    async checkActiveLineOverlap\(\)', r'^    async checkTreeOverlap'),
     (r'^    async checkActiveLineOverlap\(\)', r'^    async checkTreeOverlap')),
    ('checkTreeOverlap',
     (r'^    async checkTreeOverlap\(\)', r'^    (async )?_applyTreeBadges'),
     (r'^    async checkTreeOverlap\(\)', r'^    (async )?renderTreeLines')),
    ('renderTreeLines (+_applyTreeBadges)',
     (r'^    renderTreeLines\(\)', r'^    (async )?importToRepertoire'),
     (r'^    renderTreeLines\(\)', r'^    (async )?importToRepertoire')),
    ('importToRepertoire',
     (r'^    async importToRepertoire\(', r'^    async importAllLinesToRepertoire'),
     (r'^    async importToRepertoire\(', r'^    async importAllLinesToRepertoire')),
    ('importAllLinesToRepertoire',
     (r'^    async importAllLinesToRepertoire\(', r'^    async addCurrentMove'),
     (r'^    async importAllLinesToRepertoire', r'^    (async )?markLastMove')),
]
total = 0
print('  %-32s %-20s %-20s' % ('cluster', 'scout.js', 'builder.js'))
for name, sa, sb_ in clusters:
    a = span(sb, sa[0], sa[1])
    b = span(rb, sb_[0], sb_[1])
    la = (a[1] - a[0] + 1) if a else 0
    lb = (b[1] - b[0] + 1) if b else 0
    total += min(la, lb)
    print('  %-32s %-20s %-20s' % (name,
          ('%d-%d (%d L)' % (a[0], a[1], la)) if a else '-',
          ('%d-%d (%d L)' % (b[0], b[1], lb)) if b else '-'))
print('  ---- smaller-of-pair total (duplicated lines): ~%d' % total)
PY

echo
echo "=== 7) the API layer is forked the same way"
grep -n '@app.post("/api/scout/check_line")\|@app.post("/api/scout/check_tree")\|@app.post("/api/repertoire/check_line")\|@app.post("/api/repertoire/check_tree")' backend/api.py
grep -n "api/scout/check_line\|api/scout/check_tree" frontend/scout.js | head -4
grep -n "api/repertoire/check_line\|api/repertoire/check_tree" frontend/repertoire_builder.js | head -4
