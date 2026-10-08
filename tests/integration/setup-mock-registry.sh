#!/bin/bash
# Populate the live fluxion graph with qdevice marker vertices, so the selector
# has something to discover with find. These are plain graph vertices, no
# vendor API and no token.
#
# add-subgraph works with qmanager, unlike load-file. Fluxion is loaded the
# normal way and the markers are grown into the running graph. A load-file
# graph cannot handshake with qmanager at all.
#
# Run inside the container, in a flux instance with flux-sched add-hold.
# Vendors default to mock ibm braket.
set -eu
VENDORS="${VENDORS:-mock ibm braket}"
SUB="${SUB:-/tmp/quantum-subgraph.json}"
LIVE="${LIVE:-/tmp/live-graph.json}"

echo "-- release preloaded scheduler; load fluxion + qmanager NORMALLY --"
# fluxion with the coschedule policy and without feasibility, see fluxion.sh
. "$(dirname "$0")/fluxion.sh"
ensure_fluxion || { echo "FAIL could not load fluxion"; exit 1; }

echo "-- populate qdevice_<vendor> -> qpu into the live graph (find + add_subgraph RPC) --"
flux python -c "
import flux
from flux_quantum import graph
added = graph.populate(flux.Flux(), \"\"\"$VENDORS\"\"\".split())
print('added vendors:', sorted(added))
"

echo "-- verify markers are discoverable via find --"
# the same find RPC the plugin uses, so this does not need flux ion-resource
found=$(flux python -c "
import flux
from flux_quantum import graph
print(' '.join('qdevice_' + v for v in sorted(graph.vendors_present(graph.get_live_graph(flux.Flux())))))")
echo "discoverable vendor types: $found"
for v in $VENDORS; do
    case " $found " in
        *" qdevice_${v} "*) : ;;
        *) echo "FAIL: qdevice_${v} not found in registry"; exit 1 ;;
    esac
done
echo "PASS: vendor registry present ($found)"
