#!/bin/bash
# Unload the plugin with a grace timer pending, then reload it with jobs
# running. The timer must die with the plugin, and the reloaded plugin must
# still see the running jobs, since job.new is replayed but job.state.run is
# not.
#
#     FLUX_QUANTUM_MOCK=1 flux start bash tests/integration/test-reload.sh
#
# Needs at least 3 cores.

set -u
HERE=$(cd "$(dirname "$0")/../.." && pwd)
export FLUX_CLI_PLUGINPATH="$HERE/cli-plugins"
PLUGIN="$HERE/flux_quantum/jobtap/quantum.so"
rc=0

if [ -z "${FLUX_QUANTUM_MOCK:-}" ]; then
    echo "FAIL FLUX_QUANTUM_MOCK must be exported before flux start"
    exit 1
fi

CORES=$(flux resource list -s all -no "{ncores}" 2>/dev/null)
CORES=${CORES:-0}
echo "=== cluster has $CORES cores ==="
if [ "$CORES" -lt 3 ]; then
    echo "SKIP need at least 3 cores"
    exit 0
fi

make -s -C "$HERE/flux_quantum/jobtap" || { echo "FAIL build"; exit 1; }
flux python -m flux_quantum.populate mock >/dev/null 2>&1 \
    || echo "WARNING could not populate the graph up front"

query () {
    flux jobtap query quantum.so
}

load () {
    flux jobtap load "$PLUGIN" vendors="mock" total_cores="$CORES" \
        reserve_cores=0 protect_types="qpu" preempt_after=4 \
        || { echo "FAIL load"; exit 1; }
}

echo ""
echo "=== fill every core with unprotected work ==="
load
fill=""
for _ in $(seq 1 "$CORES"); do
    id=$(flux submit -n1 sleep 600) && fill="$fill $id"
done
for _ in $(seq 1 20); do
    [ "$(flux jobs -no '{state}' | grep -c RUN)" -eq "$CORES" ] && break
    sleep 0.5
done
echo "  $(flux jobs -no '{state}' | grep -c RUN) running"

echo ""
echo "=== the plugin reports what it sees ==="
before=$(query)
echo "$before" | sed 's/^/    /'
if ! echo "$before" | grep -q '"preemptible_cores": *'"$CORES"; then
    echo "FAIL expected $CORES preemptible cores with the machine full"
    rc=1
fi

echo ""
echo "=== remove the plugin while a grace timer is pending ==="
# the classical half reaching SCHED arms the timer, and cancelling the pair
# afterwards leaves it armed with nothing promised
err=$(mktemp)
scout=$(flux submit --quantum-vendor mock -n1 -- true 2>"$err")
main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
rm -f "$err"
if [ -z "$main" ]; then
    echo "FAIL no pair was created"; exit 1
fi
flux cancel "$main" "$scout" 2>/dev/null
flux jobtap remove quantum.so || { echo "FAIL remove"; rc=1; }
sleep 6
if flux jobs -a >/dev/null 2>&1 && flux ping -c1 job-manager >/dev/null 2>&1; then
    echo "  job manager is still alive after the timer would have fired"
else
    echo "FAIL the job manager is gone, the timer fired into the unloaded plugin"
    rc=1
fi
if flux dmesg 2>/dev/null | grep -qiE "segfault|job-manager.*(exited|crash)"; then
    echo "FAIL the log shows a crash"; rc=1
fi

echo ""
echo "=== reload with jobs running: they must still count as running ==="
load
after=$(query)
echo "$after" | sed 's/^/    /'
if ! echo "$after" | grep -q '"preemptible_cores": *'"$CORES"; then
    echo "FAIL after reload the running jobs are not counted, cores would be handed out twice"
    rc=1
fi
if ! echo "$after" | grep -q '"free_cores": *0'; then
    echo "FAIL after reload the plugin thinks cores are free on a full machine"
    rc=1
fi

echo ""
echo "=== and a pair too big for what is reachable is still refused ==="
err=$(mktemp)
if flux submit --quantum-vendor mock -n"$CORES" -- true >/dev/null 2>"$err"; then
    echo "FAIL a pair needing $((CORES + 1)) was admitted onto $CORES reachable cores"
    rc=1
elif grep -q "no room for another pair" "$err"; then
    echo "  refused, and the message says why"
else
    echo "FAIL refused for the wrong reason"; sed 's/^/    /' "$err"; rc=1
fi
rm -f "$err"

# shellcheck disable=SC2086
flux cancel $fill 2>/dev/null
flux jobtap remove quantum.so 2>/dev/null

echo ""
echo "=== reload test rc=$rc ==="
exit "$rc"
