#!/bin/bash
##############################################################
# Token-free test of the AUTO-SELECT / discovery path:
#   registry markers (add-subgraph) -> CLI plugin discovers vendors from the
#   fluxion graph (NOT just backends) -> selects the usable one -> held ->
#   scout -> handoff. Complements test-mock-e2e.sh (which uses an explicit
#   vendor and never exercises graph discovery).
#
# Discriminating check: the graph carries mock/ibm/braket but NOT mock_busy,
# while mock_busy IS a registered backend. So if the plugin's candidate list
# contains mock_busy it fell back to backends; if it lists exactly the graph
# vendors, discovery genuinely read the graph.
#
#   flux start bash tests/integration/test-select-discovery.sh
##############################################################
set -u
HERE=$(cd "$(dirname "$0")/../.." && pwd)
SELF=$(cd "$(dirname "$0")" && pwd)
export FLUX_QUANTUM_MOCK=1
export FLUX_CLI_PLUGINPATH="$HERE/cli-plugins"
RDV=/tmp/qrdv.$$; rm -rf "$RDV"; mkdir -p "$RDV"
ERR=/tmp/qsel.err.$$

echo "=== 1. plant the vendor registry (mock ibm braket) via add-subgraph ==="
VENDORS="mock ibm braket" bash "$SELF/setup-mock-registry.sh" || exit 1

echo ""
echo "=== 2. submit with --quantum-select any (NO explicit vendor) ==="
cid=$(flux submit --quantum-select any --quantum-rendezvous "$RDV" -n1 \
        flux python "$HERE/flux_quantum/wrap.py" --rendezvous "$RDV" \
        -- sh -c 'echo QUANTUM_SESSION=$QUANTUM_SESSION_ID' 2>"$ERR")
echo "submitted: $cid"
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
echo "=== 4. full pipeline via auto-select: held -> scout -> handoff ==="
sleep 1
st=$(flux jobs -no '{state}' "$cid" 2>/dev/null)
echo "state (expect SCHED, i.e. held): $st"
flux run -n1 flux python "$HERE/flux_quantum/scout.py" \
        --job "$cid" --rendezvous "$RDV" --vendor mock --session AUTOSESS456 </dev/null
flux job wait-event -t 20 "$cid" clean </dev/null
out=$(flux job attach "$cid" </dev/null 2>&1)
echo "$out" | grep -q "AUTOSESS456" \
    && echo "PASS: auto-selected job ran with handed-off session (AUTOSESS456)" \
    || { echo "FAIL: session not observed"; echo "--- output ---"; echo "$out"; }

rm -rf "$RDV" "$ERR"
