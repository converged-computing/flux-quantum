#!/bin/bash
# End-to-end test of the pipeline with the IonQ backend against the fake
# api.ionq.co, so no key is needed and nothing is billed.
#
# One flux submit with --quantum-vendor ionq produces the held classical and
# the scout. The scout opens a session on the fake service, the classical
# job submits a circuit into it, and the scout ends the session afterwards.
#
#     flux start -s1 bash tests/integration/test-ionq-e2e.sh
#     FAKE_IONQ_NO_SESSIONS=1 flux start bash tests/integration/test-ionq-e2e.sh
set -u
HERE=$(cd "$(dirname "$0")/../.." && pwd)
export FLUX_CLI_PLUGINPATH="$HERE/cli-plugins"
export FLUX_QUANTUM_MOCK=1
export IONQ_API_KEY="fake-key-$$"
ERR=$(mktemp)
LOG=$(mktemp)
rc=0

echo "=== 0. start the fake service ==="
flux python -m flux_quantum.backends.ionq.fake --port 0 >"$LOG" 2>&1 &
FAKE=$!
for _ in $(seq 1 50); do
    grep -q "listening on" "$LOG" && break
    sleep 0.1
done
IONQ_API_URL=$(grep -oE 'http://[0-9.]+:[0-9]+' "$LOG")
export IONQ_API_URL
[ -n "$IONQ_API_URL" ] || { echo "FAIL: fake service did not start"; cat "$LOG"; exit 1; }
echo "fake ionq at $IONQ_API_URL"
trap 'kill $FAKE 2>/dev/null; rm -f "$ERR" "$LOG"' EXIT

echo ""
echo "=== 1. plugin discovery ==="
flux submit --help 2>&1 | grep -q -- --quantum-dry-run || { echo "FAIL: no --quantum-dry-run"; exit 1; }

echo ""
echo "=== 2. load fluxion + qmanager ==="
. "$HERE/tests/integration/fluxion.sh"
ensure_fluxion || { echo "FAIL could not load fluxion"; exit 1; }

echo ""
echo "=== 3. one quantum submit -> held classical and scout ==="
scout_id=$(flux submit --quantum-vendor ionq --quantum-dry-run \
    --quantum-device qpu.forte-1 -n1 \
    -- flux python "$HERE/examples/ionq/workload.py" 2>"$ERR")
cat "$ERR" >&2
main_id=$(grep -oE 'held classical job [0-9]+' "$ERR" | awk '{print $NF}')
echo "scout=$scout_id  classical=$main_id"
[ -n "$main_id" ] || { echo "FAIL: no held classical job"; rc=1; }

echo ""
echo "=== 4. the classical job submitted into the session and got a result ==="
if [ -n "$main_id" ]; then
    if flux job wait-event -t 60 "$main_id" clean </dev/null; then
        out=$(flux job attach "$main_id" </dev/null 2>&1)
        echo "$out" | sed 's/^/    /'
        echo "$out" | grep -q "^session: " || { echo "FAIL: no session reached the classical job"; rc=1; }
        echo "$out" | grep -q "^target:  simulator" || { echo "FAIL: dry run did not target the simulator"; rc=1; }
        echo "$out" | grep -q "^status: completed" || { echo "FAIL: the circuit did not complete"; rc=1; }
    else
        echo "FAIL: classical never completed"; rc=1
        flux jobs -a 2>&1 | head
    fi
fi

echo ""
echo "=== 5. the scout held the session for the classical's lifetime, then ended it ==="
if [ -n "$scout_id" ]; then
    if flux job wait-event -t 60 "$scout_id" clean </dev/null >/dev/null 2>&1; then
        sout=$(flux job attach "$scout_id" </dev/null 2>&1)
        echo "$sout" | sed 's/^/    /'
        echo "$sout" | grep -q "classical job .* finished" || { echo "FAIL: scout did not wait"; rc=1; }
        echo "$sout" | grep -q "closed ionq session" || { echo "FAIL: scout did not close the session"; rc=1; }
    else
        echo "FAIL: scout never completed"; rc=1
    fi
fi

echo ""
echo "=== 6. teardown ==="
flux module remove -f sched-fluxion-qmanager 2>/dev/null || true
flux module remove -f sched-fluxion-feasibility 2>/dev/null || true
flux module remove -f sched-fluxion-resource 2>/dev/null || true
exit "$rc"
