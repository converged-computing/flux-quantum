#!/bin/bash
##############################################################
# Token-free end-to-end test of the PRODUCTION flux-quantum pipeline.
#
# ONE `flux submit --quantum-vendor mock` does the whole thing via the CLI
# plugin's modify_jobspec: it submits the user's work as a HELD classical job
# (wrapped to wait for the session), gates on it entering the queue, populates
# qdevice_mock->qpu into the graph, and rewrites the submit into the SCOUT.
# The scout opens a mock session, posts it to the classical job's eventlog, and
# releases the classical -- which then runs with QUANTUM_SESSION_ID set.
#
# stdout of `flux submit` is the SCOUT id; the held classical id is reported by
# the plugin on stderr. No manual scout launch -- that is the point.
#
# NO real vendor token: FLUX_QUANTUM_MOCK enables the mock backend. Run inside
# the container from a clone of this repo (needs >=2 cores so the scout has a
# foothold while the classical holds its reservation):
#     flux start -s1 bash tests/integration/test-mock-e2e.sh
##############################################################
set -u
HERE=$(cd "$(dirname "$0")/../.." && pwd)          # repo root
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
# The user command is NOT pre-wrapped; the plugin wraps it. stdout=scout id,
# stderr carries 'held classical job <id>'.
# --quantum-mock-session is a VENDOR option (declared by the mock backend); it
# must flow CLI -> plugin -> scout -> backend.open_session -> session id, so the
# classical should print exactly this value. Proves the vendor-option seam.
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
# The scout must not exit at release time: it holds the fluxion qpu allocation
# (and the vendor session) until the classical finishes, then closes the
# session. Its own output is the deterministic proof that it waited.
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
