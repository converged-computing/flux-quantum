#!/bin/bash
##############################################################
# Populate the LIVE fluxion graph with a token-free quantum vendor REGISTRY:
# qdevice_* marker vertices the CLI plugin/selector discover via find (used by
# --quantum-select auto-pick). Markers are plain graph vertices added with
# `flux ion-resource add-subgraph` -- no vendor API, no token.
#
# Unlike load-file, add-subgraph works WITH qmanager: fluxion is loaded normally
# (real acquire, clean qmanager handshake) and the markers are grown into the
# already-running graph. (A load-file graph cannot handshake with qmanager:
# "cannot notify when load-file set".)
#
# Run INSIDE the container, in a flux instance with flux-sched@add-hold.
# Vendors default to: mock ibm braket.
##############################################################
set -eu
VENDORS="${VENDORS:-mock ibm braket}"
SUB="${SUB:-/tmp/quantum-subgraph.json}"
LIVE="${LIVE:-/tmp/live-graph.json}"
SELF=$(cd "$(dirname "$0")" && pwd)

echo "-- release preloaded scheduler; load fluxion + qmanager NORMALLY --"
flux module remove -f sched-fluxion-qmanager 2>/dev/null || true
flux module remove -f sched-fluxion-resource 2>/dev/null || true
flux module remove -f sched-simple 2>/dev/null || true
flux module load sched-fluxion-resource
flux module load sched-fluxion-qmanager

echo "-- populate qdevice_<vendor> -> qpu into the live graph (find + add_subgraph RPC) --"
flux python -c "
import flux
from flux_quantum import graph
added = graph.populate(flux.Flux(), \"\"\"$VENDORS\"\"\".split())
print('added vendors:', sorted(added))
"

echo "-- verify markers are discoverable via find --"
found=$(flux ion-resource find --format=jgf status=up | sed -n '/^{/,$p' \
        | grep -oE 'qdevice_[a-z_]+' | sort -u | tr '\n' ' ')
echo "discoverable vendor types: $found"
for v in $VENDORS; do
    case " $found " in
        *" qdevice_${v} "*) : ;;
        *) echo "FAIL: qdevice_${v} not found in registry"; exit 1 ;;
    esac
done
echo "PASS: vendor registry present ($found)"
