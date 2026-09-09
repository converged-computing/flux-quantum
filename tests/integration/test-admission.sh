#!/bin/bash
# The jobtap plugin keeps a core budget over every unfinished pair, held or
# running, and rejects a pair that would exceed it. Launching a scout commits
# money, so the classical half has to be placeable.
#
#     FLUX_QUANTUM_MOCK=1 flux start bash tests/integration/test-admission.sh
#
# The variable has to be set BEFORE flux start. The CLIPlugin validate hook is
# also called by the job ingest validator, a separate process started with the
# broker, so a variable exported inside the instance never reaches it and every
# submit fails with no backend for vendor mock.

set -u
HERE=$(cd "$(dirname "$0")/../.." && pwd)
export FLUX_CLI_PLUGINPATH="$HERE/cli-plugins"
rc=0

if [ -z "${FLUX_QUANTUM_MOCK:-}" ]; then
    echo "FAIL FLUX_QUANTUM_MOCK must be exported before flux start"
    exit 1
fi

# submit a pair, print both job ids, or fail. Checking the exit status is not
# enough, the plugin prints its errors and still exits 0.
submit_pair () {
    local n="$1" err out
    err=$(mktemp)
    out=$(flux submit --quantum-vendor mock -n"$n" sleep 300 2>"$err")
    if grep -q "held classical job" "$err"; then
        printf '%s %s\n' "$out" \
            "$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')"
        rm -f "$err"
        return 0
    fi
    cat "$err" >&2
    rm -f "$err"
    return 1
}

echo "=== build and load ==="
make -s -C "$HERE/flux_quantum/jobtap" || { echo "FAIL build"; exit 1; }

# add the vendor before anything is allocated. Growing the fluxion graph while
# jobs hold resources breaks every later free and wedges the scheduler.
flux python -m flux_quantum.populate mock >/dev/null 2>&1 \
    || echo "WARNING could not populate the graph up front"

# a one core pair reserves two, so a budget of four takes exactly two pairs
flux jobtap load "$HERE/flux_quantum/jobtap/quantum.so" \
    vendors="mock,ibm,braket" total_cores=4 reserve_cores=0 \
    protect_types="qpu" || { echo "FAIL load"; exit 1; }

echo ""
echo "=== a pair reserves its cores plus one for the scout ==="
pairs=""
for i in 1 2; do
    if ids=$(submit_pair 1); then
        pairs="$pairs $ids"
        echo "  pair $i admitted"
    else
        echo "FAIL pair $i should have been admitted, two one core pairs fit a budget of four"
        rc=1
    fi
done

echo ""
echo "=== the third is rejected rather than left waiting ==="
err=$(mktemp)
if submit_pair 1 >/dev/null 2>"$err"; then
    echo "FAIL a third pair was admitted, the budget is not being enforced"
    rc=1
elif grep -q "no room for another pair" "$err"; then
    echo "  rejected, and the message says why"
    grep -o "no room for another pair.*" "$err" | head -1 | cut -c1-100 | sed 's/^/    /'
else
    echo "FAIL rejected for the wrong reason"; sed 's/^/    /' "$err"; rc=1
fi
rm -f "$err"

echo ""
echo "=== a pair too large for the budget is rejected on its own ==="
if submit_pair 64 >/dev/null 2>&1; then
    echo "FAIL a 64 core pair should not fit a budget of four"; rc=1
else
    echo "  rejected"
fi

echo ""
echo "=== both halves are marked protected, by the plugin and not the user ==="
for id in $pairs; do
    if flux job info "$id" jobspec 2>/dev/null | grep -q '"protected"'; then
        echo "  $id is protected"
    else
        echo "FAIL $id was not marked protected, so it is preemptible"; rc=1
    fi
done

echo ""
echo "=== a submitter cannot mark their own job protected ==="
err=$(mktemp)
if flux submit --setattr=system.protected=quantum -n1 true >/dev/null 2>"$err"; then
    echo "FAIL a user set the protection flag and was allowed to"; rc=1
elif grep -q "set by the scheduler" "$err"; then
    echo "  rejected, and the message says why"
else
    echo "FAIL rejected for the wrong reason"; sed 's/^/    /' "$err"; rc=1
fi
rm -f "$err"

echo ""
echo "=== an ordinary job is left preemptible ==="
plain=$(flux submit -n1 sleep 300)
if flux job info "$plain" jobspec 2>/dev/null | grep -q '"protected"'; then
    echo "FAIL an ordinary job was marked protected"; rc=1
else
    echo "  not protected, so it can be preempted"
fi

echo ""
echo "=== the budget is given back as pairs finish ==="
# shellcheck disable=SC2086
flux cancel $pairs "$plain" >/dev/null 2>&1
for _ in $(seq 1 30); do
    flux jobs -no "{id}" 2>/dev/null | grep -q . || break
    sleep 1
done
if ids=$(submit_pair 1); then
    echo "  a new pair is admitted again"
    # shellcheck disable=SC2086
    flux cancel $ids >/dev/null 2>&1
else
    echo "FAIL the budget did not recover, cores are leaking on job.destroy"; rc=1
fi

echo ""
echo "=== a pair is refused when the machine cannot reach its cores ==="
# The numbers fit the budget but the cores are held by protected work. Sized
# from the machine because fluxion refuses a pair bigger than the graph before
# the budget is asked. A pair of C-4 leaves C-4 promised and one scout
# running, so 3 are reachable, and a 4 core pair needs 5.
CORES=$(flux resource list -s all -no "{ncores}" 2>/dev/null)
CORES=${CORES:-0}
if [ "$CORES" -lt 6 ]; then
    echo "SKIP need at least 6 cores, have $CORES"
else
FIRST=$((CORES - 4))
flux jobtap remove quantum.so >/dev/null 2>&1
flux jobtap load "$HERE/flux_quantum/jobtap/quantum.so" \
    vendors="mock" total_cores="$CORES" reserve_cores=0 protect_types="qpu"
if big=$(submit_pair "$FIRST"); then
    echo "  a $FIRST core pair is admitted: $FIRST promised, 1 running scout, of $CORES"
    err=$(mktemp)
    if submit_pair 4 >/dev/null 2>"$err"; then
        echo "FAIL a second pair was admitted, it needs 5 and only 3 are reachable"
        rc=1
    elif grep -q "no room for another pair" "$err"; then
        echo "  the second pair is refused, and the message says what is reachable"
        grep -o "needs .*reachable" "$err" | head -1 | cut -c1-92 | sed 's/^/    /'
    else
        echo "FAIL refused for the wrong reason"; sed 's/^/    /' "$err"; rc=1
    fi
    rm -f "$err"
    # shellcheck disable=SC2086
    flux cancel $big >/dev/null 2>&1
else
    echo "FAIL the first pair should have fit a budget of $CORES"
    rc=1
fi
fi
for _ in $(seq 1 20); do
    flux jobs -no "{id}" 2>/dev/null | grep -q . || break
    sleep 1
done

echo ""
echo "=== vendor policy still applies ==="
if flux submit --quantum-vendor rigetti -n1 true >/dev/null 2>&1; then
    echo "FAIL an unconfigured vendor was accepted"; rc=1
else
    echo "  unconfigured vendor rejected"
fi

# jobtap plugins are listed and removed by file name, not by registered name
flux jobtap remove quantum.so >/dev/null 2>&1
echo ""
echo "=== admission test rc=$rc ==="
exit "$rc"
