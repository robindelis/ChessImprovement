#!/usr/bin/env bash
# CODE-4: audio.js evidence for CODE-20 -- direct-to-destination voices with
# no master bus, no error boundary, discarded resume() promise, and a
# "success" timbre used for destructive actions.
# Read-only, idempotent, no DB / no network.
set -uo pipefail
cd "$(dirname "$0")/../.."

echo "=== 1) every voice connects straight to destination (no master gain / limiter)"
grep -n "connect(" frontend/audio.js
echo
echo "  master gain node present?  $(grep -c 'createGain()' frontend/audio.js) createGain() calls, all per-voice"
echo "  DynamicsCompressor present? $(grep -c 'createDynamicsCompressor' frontend/audio.js)"
echo "  any try/catch in audio.js?  $(grep -c 'try {' frontend/audio.js)"
echo "  any ctx.close()?            $(grep -c 'ctx.close' frontend/audio.js)"
echo "  disconnect() calls?         $(grep -c 'disconnect' frontend/audio.js)"
echo

echo "=== 2) peak gains (summed when voices overlap)"
grep -n "gain.gain.setValueAtTime" frontend/audio.js
echo
echo "  playSuccess fires 4 notes; playMove 0.3 + playCapture 0.4 + 2x playCheck 0.2"
echo "  -> worst case summed peak: 0.3 (move tail) + 4*0.25 (success) = 1.3 > 1.0"
echo

echo "=== 3) _init(): no try/catch, resume() promise discarded"
sed -n '7,15p' frontend/audio.js
echo
echo "  .catch on resume()?  $(grep -c 'resume()\.' frontend/audio.js)"
echo

echo "=== 4) call sites that reach the AudioContext AFTER an await"
grep -n -B 3 "chessAudio.play" frontend/scout.js | grep -n "await\|play" | head -20
echo
echo "  -> scout.js:1810-1818 (markLastMoveAsImportant), 1662-1668 & 1742-1748 (import),"
echo "     repertoire_trainer.js:209/680/847 (all downstream of await fetch)"
echo

echo "=== 5) 'success' chime used for destructive / no-op actions"
grep -n "playSuccess\|playBlunder" frontend/scout.js frontend/repertoire_trainer.js
echo
echo "  scout.js:1861 and 1895 -> playSuccess() next to a red 'Removed from Repertoire' toast"
echo "  scout.js:1818          -> playSuccess() fired even when persisted === false"
