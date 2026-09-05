#!/bin/bash
# Admission promises the classical half can be placed, but ordinary work may be
# sitting on the cores when the scout releases it. With preempt_after set, the
# plugin waits and then cancels unprotected jobs to make room.
#
#     flux start bash tests/integration/test-preemption.sh
#
# Needs at least 3 cores, so the pair has somewhere to go once room is made.

set -u
HERE=$(cd "$(dirname "$0")/../.." && pwd)
export FLUX_CLI_PLUGINPATH="$HERE/cli-plugins"
rc=0

if [ -z "${FLUX_QUANTUM_MOCK:-}" ]; then
    echo "FAIL FLUX_QUANTUM_MOCK must be exported before flux start"
    exit 1
fi

CORES=$(flux resource list -no "{ncores}" 2>/dev/null | head -1)
CORES=${CORES:-0}
echo "=== cluster has $CORES cores ==="
if [ "$CORES" -lt 3 ]; then
    echo "SKIP need at least 3 cores"
    exit 0
fi

make -s -C "$HERE/flux_quantum/jobtap" || { echo "FAIL build"; exit 1; }

# Add the vendor to the graph before anything is allocated. Growing the graph
# while jobs hold resources corrupts fluxion, every later free fails, and the
# instance stops scheduling entirely.
flux python -m flux_quantum.populate mock >/dev/null 2>&1 \
    || echo "WARNING could not populate the graph up front"
flux jobtap load "$HERE/flux_quantum/jobtap/quantum.so" \
    vendors="mock" total_cores="$CORES" reserve_cores=0 \
    protect_types="qpu" preempt_after=3 || { echo "FAIL load"; exit 1; }

echo ""
echo "=== fill every core with unprotected work ==="
fill=""
for _ in $(seq 1 "$CORES"); do
    id=$(flux submit -n1 sleep 600) && fill="$fill $id"
done
sleep 3
echo "  submitted $CORES one core jobs"
flux jobs -no "{id} {state} {ntasks}" | head -5 | sed 's/^/    /'

echo ""
echo "=== submit a pair, its classical half cannot be placed ==="
err=$(mktemp)
scout=$(flux submit --quantum-vendor mock -n1 -- sh -c 'echo classical ran' 2>"$err")
main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
sed 's/^/    /' "$err"; rm -f "$err"
if [ -z "$main" ]; then
    echo "FAIL no pair was created"
    exit 1
fi
echo "  scout=$scout classical=$main"

echo ""
echo "=== after the grace period something unprotected is cancelled ==="
if flux job wait-event -t 120 "$main" clean </dev/null >/dev/null 2>&1; then
    echo "  the classical half ran, so room was made"
else
    echo "FAIL the classical half never started, preemption did not free cores"
    flux jobs -a | sed 's/^/    /'
    rc=1
fi

victims=0
# shellcheck disable=SC2086
for id in $fill; do
    if flux job eventlog "$id" 2>/dev/null | grep -q 'type="preempt"'; then
        victims=$((victims + 1))
    fi
done
if [ "$victims" -gt 0 ]; then
    echo "  $victims unprotected job(s) preempted"
else
    echo "FAIL nothing was preempted, so the cores came free some other way"
    rc=1
fi

echo ""
echo "=== neither half of the pair was a victim ==="
for half in "classical:$main" "scout:$scout"; do
    name=${half%%:*}; id=${half#*:}
    if flux job eventlog "$id" 2>/dev/null | grep -q 'type="preempt"'; then
        echo "FAIL the $name half was preempted, protection is not working"
        rc=1
    else
        echo "  $name was not preempted"
    fi
done

# shellcheck disable=SC2086
for id in $fill; do flux cancel "$id" 2>/dev/null; done
flux cancel "$scout" 2>/dev/null
flux jobtap remove quantum.so 2>/dev/null

echo ""
echo "=== preemption test rc=$rc ==="
exit "$rc"
