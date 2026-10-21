#!/bin/bash
# Load fluxion and verify the vendor registry is in the graph, so the selector
# has something to discover with find. The qdevice vertices are part of the
# static scheduling graph the instance was configured with - start the
# instance through run-with-static-graph.sh. Nothing is grown here: the old
# add_subgraph populate is gone, mutating a live graph is what corrupted
# fluxion.
#
# Vendors default to mock ibm braket.
set -eu
VENDORS="${VENDORS:-mock ibm braket}"

echo "-- release preloaded scheduler; load fluxion + qmanager NORMALLY --"
. "$(dirname "$0")/fluxion.sh"
ensure_fluxion || { echo "FAIL could not load fluxion"; exit 1; }

echo "-- verify the configured qdevice markers are discoverable via find --"
# the same find RPC the plugin uses, so this does not need flux ion-resource
found=$(flux python -c "
import flux
from flux_quantum import graph
print(' '.join('qdevice_' + v for v in sorted(graph.vendors_present(graph.get_live_graph(flux.Flux())))))")
echo "discoverable vendor types: $found"
for v in $VENDORS; do
    case " $found " in
        *" qdevice_${v} "*) : ;;
        *)
            echo "FAIL: qdevice_${v} not in the graph. Start the instance with"
            echo "      the static scheduling graph, e.g."
            echo "      bash tests/integration/run-with-static-graph.sh 1 \"$VENDORS\" 2 bash <test>"
            exit 1
            ;;
    esac
done
echo "PASS: vendor registry present ($found)"
