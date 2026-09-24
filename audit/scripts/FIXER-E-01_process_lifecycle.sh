#!/usr/bin/env bash
# FIXER-E-01 — process-lifecycle harness for the Electron shell.
#
# Starts the real app on a spare port (CHESS_PORT is overridden so the user's
# own server on 38281 is never touched and the app really spawns its own python
# child), then exercises every shutdown path and proves no orphan survives.
#
# It never clicks anything in the UI: no sync, no seed, no clear.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${FIXER_E_PORT:-39281}"
SHOTDIR="${FIXER_E_SHOTDIR:-/tmp/opencode/ps}"
CDP_PORT="${FIXER_E_CDP_PORT:-9333}"
mkdir -p "$SHOTDIR"
PS="$SHOTDIR/ps.txt"

# Precise matchers: match the executable + args, never a shell command line.
# shellcheck disable=SC2016
our_python() { ps -eo pid=,ppid=,pgid=,sid=,args= | awk -v p="start.py --port $PORT" '$5 ~ /python3?$/ || $5 ~ /python3$/ { if (index($0, p)) print }'; }
our_electron_main() { ps -eo pid=,args= | awk '/electron\/dist\/electron/ && !/--type=/ && !/awk/ {print $1; exit}'; }
our_stockfish() { local py; py=$(our_python | awk '{print $1}' | tr '\n' ' '); [ -z "$py" ] && return; ps -eo pid=,ppid=,args= | awk -v pys="$py" '/stockfish-linux/ { for (i = 1; i <= split(pys, a, " "); i++) if (a[i] == $2) { print; break } }'; }

snap() {  # snap <label>
    {
        echo "########## snapshot: $1"
        echo "--- electron main process:"
        our_electron_main | while read -r p; do [ -n "$p" ] && ps -o pid=,ppid=,pgid=,sid=,etime=,args= -p "$p" | cut -c1-140; done
        echo "--- our python backend (port $PORT) + its stockfish children:"
        our_python | cut -c1-160
        our_stockfish | cut -c1-160
        echo "--- verdict: python=$(our_python | wc -l) electron=$(our_electron_main | wc -l) stockfish=$(our_stockfish | wc -l)"
        echo
    } | tee -a "$SHOTDIR/timeline.txt"
}

wait_gone() { for _ in $(seq 1 24); do kill -0 "$1" 2>/dev/null || return 0; sleep 0.5; done; return 1; }
wait_ready() {
    for _ in $(seq 1 70); do
        [ -n "$(our_electron_main)" ] && [ -n "$(our_python)" ] && { sleep 5; return 0; }
        sleep 1
    done
    return 1
}
start_app() { ( cd "$ROOT" && CHESS_PORT="$PORT" setsid ./run-desktop.sh --remote-debugging-port="$CDP_PORT" --no-sandbox > "$SHOTDIR/app-$1.log" 2>&1 & ); }
cleanup() { pkill -f "electron/dist/electron .*--remote-debugging-port=$CDP_PORT" 2>/dev/null; sleep 1; }

: > "$SHOTDIR/timeline.txt"
echo "=== FIXER-E-01 process lifecycle harness (port $PORT) ==="
snap "00-BEFORE-LAUNCH"
cleanup

# ------------------------------------------------------------------ PATH 1
echo "########## PATH 1: SIGINT to the Electron main process (Ctrl+C equivalent)"
start_app p1
if wait_ready; then
    M="$(our_electron_main)"; P="$(our_python | awk '{print $1}')"
    echo "electron main=$M  python=[$P]"; snap "01-RUNNING-path1"
    kill -INT "$M"
    for p in $P; do wait_gone "$p"; done
    sleep 2; snap "02-AFTER-SIGINT"
else snap "01-PATH1-FAILED"; fi
cleanup

# ------------------------------------------------------------------ PATH 2
echo "########## PATH 2: SIGTERM to the Electron main process"
start_app p2
if wait_ready; then
    M="$(our_electron_main)"; P="$(our_python | awk '{print $1}')"
    echo "electron main=$M  python=[$P]"; snap "03-RUNNING-path2"
    kill -TERM "$M"
    for p in $P; do wait_gone "$p"; done
    sleep 2; snap "04-AFTER-SIGTERM"
else snap "03-PATH2-FAILED"; fi
cleanup

# ------------------------------------------------------------------ PATH 3
echo "########## PATH 3: real window close (close -> closed -> window-all-closed)"
start_app p3
if wait_ready; then
    M="$(our_electron_main)"; P="$(our_python | awk '{print $1}')"
    echo "electron main=$M  python=[$P]"; snap "05-RUNNING-path3"
    node "$ROOT/audit/scripts/FIXER-E-01_close_window.js" "$CDP_PORT" 2>&1 | tee -a "$SHOTDIR/close.log"
    for p in $P; do wait_gone "$p"; done
    sleep 2; snap "06-AFTER-WINDOW-CLOSE"
else snap "05-PATH3-FAILED"; fi
cleanup

# ------------------------------------------------------------------ PATH 4
echo "########## PATH 4: second launch while the first is running (single-instance lock)"
start_app p4
if wait_ready; then
    P1="$(our_python | awk '{print $1}' | tr '\n' ' ')"; M1="$(our_electron_main)"
    echo "first instance: electron=$M1 python=[$P1]"; snap "07-FIRST-INSTANCE-RUNNING"
    echo "--- launching a second instance (expect: it quits immediately, no new backend)"
    ( cd "$ROOT" && CHESS_PORT="$PORT" timeout 90 ./run-desktop.sh > "$SHOTDIR/second.log" 2>&1; echo "second-instance-exit=$?" >> "$SHOTDIR/second.log" )
    cat "$SHOTDIR/second.log"
    sleep 2
    P2="$(our_python | awk '{print $1}' | tr '\n' ' ')"
    echo "python after 2nd launch: [$P2]  (must equal [$P1])"
    snap "08-AFTER-SECOND-LAUNCH"
    kill -INT "$M1" 2>/dev/null
    for p in $P1; do wait_gone "$p"; done
    sleep 2; snap "09-AFTER-FIRST-QUIT"
else snap "07-PATH4-FAILED"; fi
cleanup

# ------------------------------------------------------------------ verdict
echo "########## FINAL ORPHAN CHECK (port $PORT)"
FINAL="$(our_python)"
if [ -z "$FINAL" ]; then
    echo "RESULT: PASS - no orphaned 'start.py --port $PORT' process remains."
else
    echo "RESULT: FAIL - orphaned backend:"; echo "$FINAL"
fi
echo "(stockfish on 38281 belongs to the user's own server and to other agents'"
echo " probes; only the port-$PORT python process is this app's.)"
