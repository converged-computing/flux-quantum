#!/bin/bash
# Token free test of the auto select path. Markers go into the graph, the CLI
# plugin discovers vendors from the graph rather than from the backends, picks
# the usable one, and the usual held job and scout follow. test-mock-e2e.sh
# names a vendor, so it never covers discovery.
#
# The check works because the graph carries mock, ibm and braket but not
# mock_busy, while mock_busy is a registered backend. A candidate list with
# mock_busy in it means the plugin fell back to the backends. A list matching
# the graph exactly means discovery really read the graph.
#
#   flux start bash tests/integration/test-select-discovery.sh
##############################################################
set -u
HERE=$(cd "$(dirname "$0")/../.." && pwd)
SELF=$(cd "$(dirname "$0")" && pwd)
export FLUX_QUANTUM_MOCK=1
export FLUX_CLI_PLUGINPATH="$HERE/cli-plugins"
ERR=/tmp/qsel.err.$$

echo "=== 1. plant the vendor registry (mock ibm braket) via add-subgraph ==="
VENDORS="mock ibm braket" bash "$SELF/setup-mock-registry.sh" || exit 1

echo ""
echo "=== 2. submit with --quantum-select any (NO explicit vendor) ==="
# the plugin wraps the command itself, so do not pass wrap.py here
scout_id=$(flux submit --quantum-select any --quantum-mock-session AUTOSESS456 -n1 \
        -- sh -c 'echo QUANTUM_SESSION=$QUANTUM_SESSION_ID' 2>"$ERR")
main_id=$(grep -oE 'held classical job [0-9]+' "$ERR" | awk '{print $NF}')
echo "scout=$scout_id  classical=$main_id"
echo "--- plugin stderr ---"; sed 's/^/    /' "$ERR"

echo ""
echo "=== 3. confirm discovery READ THE GRAPH (not backends) ==="
if ! grep -q "discovered from registry:" "$ERR"; then
    echo "FAIL: plugin did not discover from the registry (fell back to backends)"; exit 1
fi
echo "PASS: plugin discovered vendors from the fluxion registry"
if grep "discovered from registry:" "$ERR" | grep -q "mock_busy"; then
    echo "FAIL: mock_busy in candidates -> came from backends, not the graph"; exit 1
fi
echo "PASS: candidate set matches the graph (no backend-only mock_busy)"
grep -q "selected: mock" "$ERR" \
    && echo "PASS: selected the only usable vendor (mock)" \
    || echo "WARN: expected mock to be selected"

echo ""
echo "=== 4. full pipeline: SCOUT co-allocates core + qdevice_mock->qpu, unholds main ==="
# the submit already produced the pair. The scout has to match slot->core plus
# qdevice_mock->qpu against the injected graph, so this covers the coschedule.
if [ -z "$main_id" ] || [ -z "$scout_id" ]; then
    echo "FAIL: the plugin did not produce a classical+scout pair"; exit 1
fi
if ! flux job wait-event -t 30 "$main_id" clean </dev/null; then
    echo "FAIL: classical never completed"
    flux jobs -a | sed 's/^/    /'
    flux job attach "$scout_id" </dev/null 2>&1 | sed 's/^/    /'
    exit 1
fi
echo "PASS: scout matched core + qdevice_mock->qpu and released the classical"
out=$(flux job attach "$main_id" </dev/null 2>&1)
if echo "$out" | grep -q "AUTOSESS456"; then
    echo "PASS: main ran with the handed-off session (AUTOSESS456)"
else
    echo "FAIL: session not observed"; echo "--- output ---"; echo "$out"; rc=1
fi
# the scout exits after the classical
flux job wait-event -t 30 "$scout_id" clean </dev/null >/dev/null 2>&1 || true

echo "=== check the instance log for errors DURING the run (before teardown) ==="
errs=$(flux dmesg 2>&1 | grep -iE "\.err\[[0-9]+\]|: error:|fatal" || true)
if [ -n "$errs" ]; then
    echo "FAIL: instance logged errors during the run:"; echo "$errs"; rc=1
fi

# graceful teardown so shutdown does not log an acquire failure
flux module remove -f sched-fluxion-qmanager 2>/dev/null || true
flux module remove -f sched-fluxion-resource 2>/dev/null || true

rm -f "$ERR"
exit "${rc:-0}"
