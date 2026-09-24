#!/usr/bin/env bash
# FIXER-E-01 — CSP enforcement verification harness.
# Loads the app's real frontend/ through a throwaway Electron window that applies
# the exact APP_CSP from electron/main.js, then reports:
#   * every CSP violation the console emitted
#   * whether style="..." attributes still apply (needs style-src 'unsafe-inline')
#   * whether the YouTube IFrame Player API script loaded (needs script-src host)
#   * that inline script + eval are actually BLOCKED (proves enforcement)
# Does not touch chess_improvement.db, does not write anything.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
exec "$ROOT/node_modules/.bin/electron" "$HERE/FIXER-E-01_csp_probe.js" "$@"
