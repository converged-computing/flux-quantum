#!/bin/bash
# End-to-end test of the pipeline with the mock vendor, so no token is needed.
#
# One flux submit with --quantum-vendor mock produces the held classical and
# the scout. stdout is the scout id and the classical id comes back on stderr.
#
# Needs 2 or more cores so the scout has a foothold while the classical holds
# its reservation.
#
#     flux start -s1 bash tests/integration/test-mock-e2e.sh
set -u
HERE=$(cd "$(dirname "$0")/../.." && pwd)
export FLUX_QUANTUM_MOCK=1
export FLUX_CLI_PLUGINPATH="$HERE/cli-plugins"
ERR=$(mktemp)
rc=0

echo "=== 0. plugin discovery (does flux see the quantum options?) ==="
flux submit --help 2>&1 | grep -E 'quantum' || { echo "FAIL: CLI plugin not discovered"; exit 1; }

echo ""
echo "=== 1. load fluxion + qmanager (normal acquire, no load-file) ==="
flux module remove -f sched-fluxion-qmanager 2>/dev/null || true
flux module remove -f sched-fluxion-resource 2>/dev/null || true
flux module remove -f sched-simple 2>/dev/null || true
flux module load sched-fluxion-resource
flux module load sched-fluxion-qmanager
echo "fluxion + qmanager loaded"

echo ""
echo "=== 2. ONE quantum submit -> plugin submits held classical AND scout ==="
# the plugin wraps the user command, so do not pre-wrap it here. stdout is the
# scout id and stderr carries the held classical job id
# a vendor option, so this value must flow from the CLI through the plugin
# and scout to the backend and into the classical environment
FORCED="mocksess-$$"
scout_id=$(flux submit --quantum-vendor mock --quantum-mock-session "$FORCED" \
    -n1 \
    -- sh -c 'echo QUANTUM_SESSION=$QUANTUM_SESSION_ID' 2>"$ERR")
cat "$ERR" >&2
main_id=$(grep -oE 'held classical job [0-9]+' "$ERR" | awk '{print $NF}')
echo "scout=$scout_id  classical=$main_id"
if [ -z "$main_id" ]; then
    echo "FAIL: plugin did not submit/report a held classical job"; rc=1
fi

echo ""
echo "=== 3. the pair runs itself: scout opens session -> releases classical ==="
if [ -n "$main_id" ]; then
    if flux job wait-event -t 30 "$main_id" clean </dev/null; then
        out=$(flux job attach "$main_id" </dev/null 2>&1)
        if echo "$out" | grep -q "QUANTUM_SESSION=$FORCED"; then
            echo "PASS: classical ran with the VENDOR-SUPPLIED session ($FORCED)"
            echo "      (--quantum-mock-session flowed CLI -> backend.open_session)"
        elif echo "$out" | grep -qE 'QUANTUM_SESSION=.+'; then
            echo "FAIL: got a session but NOT the vendor option value ($FORCED):"
            echo "$out" | grep 'QUANTUM_SESSION='; rc=1
        else
            echo "FAIL: classical ran but received no session"; echo "--- output ---"; echo "$out"; rc=1
        fi
    else
        echo "FAIL: classical never released/completed (scout did not release it?)"; rc=1
        flux jobs -a 2>&1 | head
    fi
fi

echo ""
echo "=== 3b. the scout HELD the qpu for the classical's lifetime ==="
# the scout must not exit at release time, and its own output shows it waited
if [ -n "$scout_id" ]; then
    if flux job wait-event -t 30 "$scout_id" clean </dev/null >/dev/null 2>&1; then
        sout=$(flux job attach "$scout_id" </dev/null 2>&1)
        echo "$sout" | sed 's/^/    /'
        echo "$sout" | grep -q "classical job .* finished" || {
            echo "FAIL: scout did not wait for the classical (qpu freed early)"; rc=1; }
        echo "$sout" | grep -q "closed mock session" || {
            echo "FAIL: scout did not close the vendor session"; rc=1; }
    else
        echo "FAIL: scout never completed"; rc=1
    fi
fi

echo ""
echo "=== 4. check the instance log for errors DURING the run (before teardown) ==="
errs=$(flux dmesg 2>&1 | grep -iE "\.err\[[0-9]+\]|: error:|fatal" || true)
if [ -n "$errs" ]; then
    echo "FAIL: instance logged errors during the run:"; echo "$errs"; rc=1
fi

echo "=== 5. graceful teardown (remove fluxion so shutdown is clean) ==="
flux module remove -f sched-fluxion-qmanager 2>/dev/null || true
flux module remove -f sched-fluxion-resource 2>/dev/null || true

rm -f "$ERR"
exit "$rc"
