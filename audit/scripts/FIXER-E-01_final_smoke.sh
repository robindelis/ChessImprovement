#!/usr/bin/env bash
# FIXER-E-01 — final smoke test of the hardened shell in a real Electron run.
# Verifies, live: the CSP header is actually delivered, the window opens, the
# navigation guard and window-open handler fire, and the app is still usable.
# Read-only: no sync / seed / clear is triggered.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${FIXER_E_PORT:-39281}"
CDP="${FIXER_E_CDP_PORT:-9333}"
LOG=/tmp/opencode/final/app.log
mkdir -p /tmp/opencode/final; rm -f "$LOG"

echo "=== launching hardened app on port $PORT ==="
( cd "$ROOT" && CHESS_PORT="$PORT" setsid ./run-desktop.sh \
    --remote-debugging-port="$CDP" --no-sandbox > "$LOG" 2>&1 & )
for _ in $(seq 1 80); do grep -q "Backend is online" "$LOG" 2>/dev/null && break; sleep 1; done
sleep 6

echo
echo "--- CSP actually delivered to the renderer (via CDP):"
node "$ROOT/audit/scripts/FIXER-E-01_final_checks.js" "$CDP" "$PORT" 2>&1 | sed 's/^/    /'

echo
echo "--- navigation guard: attempt to navigate the window to an external origin"
node "$ROOT/audit/scripts/FIXER-E-01_try_navigate.js" "$CDP" "https://evil.example/" 2>&1 | sed 's/^/    /'
sleep 2
echo "--- app still on its own origin?"
node "$ROOT/audit/scripts/FIXER-E-01_final_checks.js" "$CDP" "$PORT" 2>&1 | grep -E "currentUrl" | sed 's/^/    /'
echo "--- [Desktop] blocked-navigation warnings:"
grep "Blocked navigation" "$LOG" | sed 's/^/    /' | head -5

echo
echo "--- [Desktop] log:"
grep "\[Desktop\]" "$LOG" | sed 's/^/    /'

pkill -f "electron/dist/electron .*--remote-debugging-port=$CDP" 2>/dev/null
sleep 4
echo
echo "--- final orphan check: python=[$(ps -eo pid=,args= | awk '/start\.py --port 39281/ && !/awk/ {print $1}' | tr '\n' ' ')]"
echo "FINAL_DONE"
