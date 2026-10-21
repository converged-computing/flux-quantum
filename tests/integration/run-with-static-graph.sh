#!/bin/bash
# Launch a throwaway flux instance that carries the static scheduling graph,
# and run a command (usually an integration test) inside it:
#
#   bash tests/integration/run-with-static-graph.sh 2 "mock ibm braket" 2 \
#       bash tests/integration/test-static-graph.sh
#
# This is the same wiring the real cluster gets at boot: a generated
# scheduling.json next to R, handed to the broker with [resource] scheduling.
set -eu
SIZE="$1"; VENDORS="$2"; QPUS="$3"; shift 3
cd "$(dirname "$0")/../.."
source tests/integration/static-graph.sh
dir=$(make_static_config "$SIZE" "$VENDORS" "$QPUS")
exec flux start -s "$SIZE" -o,--config-path="$dir/conf" "$@"
