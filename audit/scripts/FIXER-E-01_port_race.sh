#!/usr/bin/env bash
# FIXER-E-01 — port-race probe.
#
# Reproduces the TOCTOU that CODE-05-09 flagged: two Electron instances started
# at the same instant on the same preferred port. Before the fix the loser died
# with a "Server Startup Timeout" dialog; after the fix it retries the next port.
#
# The single-instance lock normally prevents this, so the lock is bypassed here
# by giving each launch its own user-data-dir, which is what actually scopes the
# lock. Each launch then races for the same preferred port.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${FIXER_E_PORT:-39311}"
N="${FIXER_E_LAUNCHES:-4}"
LOGDIR="${FIXER_E_LOGDIR:-/tmp/opencode/portrace}"
mkdir -p "$LOGDIR"
rm -f "$LOGDIR"/*.log

echo "=== FIXER-E-01 port race: $N simultaneous launches, all preferring port $PORT ==="
echo "--- BEFORE: is $PORT free?"
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then echo "    port $PORT is OCCUPIED before the test"; else echo "    port $PORT is free"; fi

for i in $(seq 1 "$N"); do
    ( cd "$ROOT" && CHESS_PORT="$PORT" setsid ./run-desktop.sh \
        --user-data-dir="$LOGDIR/udd-$i" --no-sandbox > "$LOGDIR/launch-$i.log" 2>&1 & )
done

for _ in $(seq 1 75); do
    running=$(ps -eo args= | grep -c "start\.py --port $PORT" || true)
    [ "$running" -ge 1 ] && sleep 12 && break
    sleep 1
done

echo
echo "--- python backends now serving the app (any port), per launch log:"
for i in $(seq 1 "$N"); do
    echo "  launch $i:"
    grep -E "\[Desktop\] (Preferred port|Starting Python backend|Adopting|Backend attempt|Backend is online|Stopping)" \
        "$LOGDIR/launch-$i.log" | sed 's/^/      /' | head -12
done

echo
echo "--- live backend processes for the app:"
ps -eo pid,ppid,args --no-headers | awk '/start\.py --port 39[0-9][0-9][0-9]/ && !/awk/ {print "      " $0}' | cut -c1-120

echo
echo "--- verdict per launch:"
ok=0; failed=0
for i in $(seq 1 "$N"); do
    if grep -q "Backend is online" "$LOGDIR/launch-$i.log"; then
        echo "      launch $i: ONLINE  -> $(grep -o "Starting Python backend with: .*" "$LOGDIR/launch-$i.log" | head -1)"
        ok=$((ok+1))
    else
        echo "      launch $i: FAILED  -> $(tail -2 "$LOGDIR/launch-$i.log" | tr '\n' ' ' | cut -c1-140)"
        failed=$((failed+1))
    fi
done
echo "      online=$ok failed=$failed (of $N)"

# clean up every electron we started
pkill -f "electron/dist/electron .*--user-data-dir=$LOGDIR" 2>/dev/null
sleep 4
echo
echo "--- cleanup: remaining backends for this test:"
LEFT=$(ps -eo pid,args --no-headers | awk '/start\.py --port 39[0-9][0-9][0-9]/ && !/awk/' | wc -l)
echo "      count=$LEFT"
[ "$LEFT" -gt 0 ] && ps -eo pid,args --no-headers | awk '/start\.py --port 39[0-9][0-9][0-9]/ && !/awk/ {print "      " $0}' | cut -c1-120
echo "PORTRACE_DONE"
