#!/bin/bash
##############################################################
# Token-free end-to-end test of the core flux-quantum pipeline:
#   CLI plugin (vendor=mock) -> job held+reserved -> scout opens a mock session
#   -> rendezvous handoff -> job releases and runs with QUANTUM_SESSION_ID.
#
# Uses an EXPLICIT vendor (--quantum-vendor mock), so no graph registry /
# discovery is needed -- the plugin selects the mock backend directly. fluxion +
# qmanager are loaded NORMALLY (a load-file graph cannot handshake with
# qmanager: "cannot notify when load-file set").
#
# NO real vendor token: FLUX_QUANTUM_MOCK enables the mock backend and the
# scout session is mocked. Run INSIDE the container from a clone of this repo:
#     flux start bash tests/integration/test-mock-e2e.sh
##############################################################
set -u
HERE=$(cd "$(dirname "$0")/../.." && pwd)          # repo root
export FLUX_QUANTUM_MOCK=1
export FLUX_CLI_PLUGINPATH="$HERE/cli-plugins"
RDV=/tmp/qrdv.$$; rm -rf "$RDV"; mkdir -p "$RDV"

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
echo "=== 2. submit a quantum job via the CLI plugin (vendor=mock, no token) ==="
# the CLI plugin's preinit picks 'mock'; modify_jobspec stamps the vendor and
# sets system.hold=1 -> the job should sit SCHED (held), reserved not allocated.
cid=$(flux submit --quantum-vendor mock --quantum-rendezvous "$RDV" -n1 \
        flux python "$HERE/flux_quantum/wrap.py" --rendezvous "$RDV" \
        -- sh -c 'echo QUANTUM_SESSION=$QUANTUM_SESSION_ID')
echo "submitted: $cid"
sleep 1
st=$(flux jobs -no '{state}' "$cid" 2>/dev/null)
echo "state (expect SCHED, i.e. held): $st"
if [ "$st" != "SCHED" ]; then
    echo "WARN: job is not SCHED -- the plugin's hold may not have taken."
    echo "      (check that modify_jobspec set system.hold=1)"
fi

echo ""
echo "=== 3. run the scout (mock session), which unholds the job ==="
flux run -n1 flux python "$HERE/flux_quantum/scout.py" \
        --job "$cid" --rendezvous "$RDV" --vendor mock --session MOCKSESS123 </dev/null

echo ""
echo "=== 4. confirm the job released and received the session ==="
flux job wait-event -t 20 "$cid" clean </dev/null
out=$(flux job attach "$cid" </dev/null 2>&1)
echo "$out" | grep -q "MOCKSESS123" && echo "PASS: job ran with the handed-off session (MOCKSESS123)" \
    || { echo "FAIL: session not observed by the job"; echo "--- job output ---"; echo "$out"; }

rm -rf "$RDV"
