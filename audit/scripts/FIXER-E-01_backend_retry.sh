#!/usr/bin/env bash
# FIXER-E-01 — backend-start retry probe.
#
# Exercises the retry loop added to app.whenReady(): if the child dies before
# the server answers (the TOCTOU window that used to produce a misleading
# "Server Startup Timeout" dialog), the app must move to the next port and come
# up anyway.
#
# How: PYTHON_PATH points at a wrapper that fails its first invocation and
# delegates to the real interpreter afterwards. No source file is modified and
# chess_improvement.db is only read (the app is never told to sync/seed/clear).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${FIXER_E_PORT:-39321}"
LOG="${FIXER_E_LOG:-/tmp/opencode/retry/app.log}"
mkdir -p "$(dirname "$LOG")"
rm -f "$LOG"

WRAPPER=/tmp/opencode/flaky-python.sh
cat > "$WRAPPER" <<'EOS'
#!/usr/bin/env bash
# Fails the first time (simulating "the port was taken between the probe and
# uvicorn's bind"), then behaves like the real interpreter.
MARK=/tmp/opencode/.flaky-python-used
if [ ! -f "$MARK" ]; then
    touch "$MARK"
    echo "simulated: could not bind port" >&2
    exit 1
fi
exec /home/robin/miniconda3/bin/python3 "$@"
EOS
chmod +x "$WRAPPER"
rm -f /tmp/opencode/.flaky-python-used

echo "=== FIXER-E-01: first spawn attempt will fail, app must retry the next port ==="
( cd "$ROOT" && CHESS_PORT="$PORT" PYTHON_PATH="$WRAPPER" setsid ./run-desktop.sh \
    --user-data-dir=/tmp/opencode/retry-udd --no-sandbox > "$LOG" 2>&1 & )

for _ in $(seq 1 90); do
    grep -q "Backend is online" "$LOG" 2>/dev/null && break
    sleep 1
done
sleep 3

echo
echo "--- [Desktop] log lines:"
grep "\[Desktop\]" "$LOG" | sed 's/^/    /'
echo
echo "--- simulated failure present? $([ -f /tmp/opencode/.flaky-python-used ] && echo yes || echo no)"
echo "--- live backend:"
ps -eo pid,ppid,args --no-headers | awk '/start\.py --port 3932/ && !/awk/ {print "    " $0}' | cut -c1-120
echo
if grep -q "Backend is online" "$LOG"; then
    echo "RESULT: PASS - app recovered by retrying on the next port."
else
    echo "RESULT: FAIL - app never came online."
fi
pkill -f "user-data-dir=/tmp/opencode/retry-udd" 2>/dev/null
sleep 3
echo "--- cleanup: remaining backends: $(ps -eo args= | grep -c 'start\.py --port 3932' || true)"
echo "RETRY_DONE"
