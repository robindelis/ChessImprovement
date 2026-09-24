#!/usr/bin/env bash
# CODE-4: evidence for the AGENTS.md §8 zero-CLS findings.
#   CODE-12  #trainNoteBox     display:none<->block   (in flow, no min-height)
#   CODE-13  #trainFocusBanner display:none<->flex    (first child of sidebar)
#            #trainImportantBadge display:none<->inline-flex (flex-wrap parent)
#   CODE-14  #btnScoutMarkImportant label width change + display toggles
#            (.btn-quick-rep has NO min-width)
#   §8.5 compliant: .scout-line-overlap-strip / #scoutActiveLineOverlap
#
# Read-only, idempotent, no DB / no network.
set -uo pipefail
cd "$(dirname "$0")/../.."

echo "=== 1) CSS: any min-height / min-width reserving space for the toggled elements?"
for sel in '#trainNoteBox' '#trainFocusBanner' '#trainImportantBadge' \
           '.btn-quick-rep' '.scout-line-overlap-strip'; do
  n=$(grep -c -- "$sel" frontend/style.css)
  printf '  %-28s rules in style.css: %s\n' "$sel" "$n"
done
echo
echo "  .btn-quick-rep body (NOTE: no min-width => label length drives layout):"
grep -n -A 6 '^\.btn-quick-rep {' frontend/style.css
echo
echo "  .scout-line-overlap-strip body (the §8.5-compliant reference):"
grep -n -A 8 '^\.scout-line-overlap-strip {' frontend/style.css
echo

echo "=== 2) JS: display-toggling of the above (AGENTS.md §8.1 / §8.4 violations)"
grep -n "noteBox.style.display" frontend/repertoire_trainer.js
grep -n "banner.style.display" frontend/repertoire_trainer.js
grep -n "badgeEl.style.display" frontend/repertoire_trainer.js
grep -n "btnRemoveRep.style.display\|btnTrain.style.display" frontend/scout.js
echo

echo "=== 3) JS: dynamic-length labels with no width clamp (§8.2)"
grep -n "btnImportant.textContent" frontend/scout.js
echo "  #scoutNoteMoveLabel (contains a SAN + a move number):"
grep -n "noteLabel.textContent" frontend/scout.js
echo

echo "=== 4) §8.5 compliance check: ply-0 placeholder + always display:flex"
grep -n "Repertoire: Starting Position" frontend/scout.js frontend/index.html
grep -n "strip.style.display = 'flex'" frontend/scout.js
echo

echo "=== 5) Correct §8.4 pattern used elsewhere in the codebase (for contrast)"
grep -n "style.visibility = 'hidden'" frontend/repertoire_builder.js | head -5
