#!/bin/bash
# Admission promises the classical half can be placed, but ordinary work may be
# sitting on the cores when the scout releases it. With preempt=1 the plugin
# watches the classical half, and when the scout's release does not get it
# running it cancels unprotected jobs for the shortfall, lowest priority
# first and youngest among equals.
#
#     flux start bash tests/integration/test-preemption.sh
#
# Needs at least 3 cores, so there is room for fillers beside the pair.

set -u
HERE=$(cd "$(dirname "$0")/../.." && pwd)
export FLUX_CLI_PLUGINPATH="$HERE/cli-plugins"
rc=0

if [ -z "${FLUX_QUANTUM_MOCK:-}" ]; then
    echo "FAIL FLUX_QUANTUM_MOCK must be exported before flux start"
    exit 1
fi

# -s all, not a bare -no "{ncores}". Without a state field in the format,
# flux resource list merges free, allocated and down onto one line, so the bare
# form returns the total while reading like the free count.
CORES=$(flux resource list -s all -no "{ncores}" 2>/dev/null)
CORES=${CORES:-0}
echo "=== cluster has $CORES cores ==="
if [ "$CORES" -lt 3 ]; then
    echo "SKIP need at least 3 cores"
    exit 0
fi

make -s -C "$HERE/flux_quantum/jobtap" || { echo "FAIL build"; exit 1; }

# a pair needs fluxion, and rc1 does not always load it
. "$HERE/tests/integration/fluxion.sh"
ensure_fluxion || { echo "FAIL could not load fluxion"; exit 1; }

# Add the vendor to the graph before anything is allocated. Growing the graph
# while jobs hold resources corrupts fluxion, every later free fails, and the
# instance stops scheduling entirely.
flux python -m flux_quantum.populate mock >/dev/null 2>&1 \
    || echo "WARNING could not populate the graph up front"
flux jobtap load "$HERE/flux_quantum/jobtap/quantum.so" \
    vendors="mock" total_cores="$CORES" reserve_cores=0 \
    protect_types="qpu" preempt=1 settle=2 || { echo "FAIL load"; exit 1; }

# submit n one core sleepers, the first with the urgency given, and wait for
# them all to run. Prints the ids.
fill_cores () {
    local n="$1" first_urgency="${2:-16}" ids="" id i
    for i in $(seq 1 "$n"); do
        if [ "$i" -eq 1 ]; then
            id=$(flux submit --urgency="$first_urgency" -n1 sleep 600)
        else
            id=$(flux submit -n1 sleep 600)
        fi
        ids="$ids $id"
    done
    for _ in $(seq 1 40); do
        [ "$(flux jobs -no '{state}' | grep -c RUN)" -ge "$n" ] && break
        sleep 0.5
    done
    echo "$ids"
}

# submit a pair, print "scout classical", or fail
submit_pair () {
    local err out main
    err=$(mktemp)
    out=$(flux submit --quantum-vendor mock -n1 -- sh -c 'echo classical ran' 2>"$err")
    main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
    if [ -z "$main" ]; then
        cat "$err" >&2; rm -f "$err"
        return 1
    fi
    rm -f "$err"
    echo "$out $main"
}

# the ids among those given with a preempt exception on their eventlog
victims_of () {
    local id
    for id in "$@"; do
        if flux job eventlog "$id" 2>/dev/null | grep -q 'type="preempt"'; then
            echo "$id"
        fi
    done
}

# cancel everything and wait for the instance to drain
drain () {
    local id
    for id in $(flux jobs -no '{id}'); do flux cancel "$id" 2>/dev/null; done
    for _ in $(seq 1 40); do
        flux jobs -no '{id}' 2>/dev/null | grep -q . || break
        sleep 0.5
    done
}

echo ""
echo "=== a released pair that cannot be placed preempts the shortfall ==="
# all but one core taken, so the scout has somewhere to run and the
# classical does not
fill=$(fill_cores $((CORES - 1)))
echo "  $((CORES - 1)) one core jobs running"
if pair=$(submit_pair); then
    scout=${pair%% *}; main=${pair##* }
    echo "  scout=$scout classical=$main"
else
    echo "FAIL no pair was created"; exit 1
fi
if flux job wait-event -t 120 "$main" clean </dev/null >/dev/null 2>&1; then
    echo "  the classical half ran, so room was made"
else
    echo "FAIL the classical half never started, preemption did not free cores"
    flux jobs -a | sed 's/^/    /'
    rc=1
fi
# shellcheck disable=SC2086
victims=$(victims_of $fill | wc -l)
if [ "$victims" -eq 1 ]; then
    echo "  exactly one job was preempted, the shortfall and no more"
else
    echo "FAIL expected 1 job preempted for a 1 core shortfall, got $victims"
    rc=1
fi
for half in "classical:$main" "scout:$scout"; do
    name=${half%%:*}; id=${half#*:}
    if flux job eventlog "$id" 2>/dev/null | grep -q 'type="preempt"'; then
        echo "FAIL the $name half was preempted, protection is not working"
        rc=1
    else
        echo "  the $name half was not preempted"
    fi
done
prio=$(flux jobs -a -no '{priority}' "$main" 2>/dev/null)
sprio=$(flux jobs -a -no '{priority}' "$scout" 2>/dev/null)
if [ "$prio" = 1000 ] && [ "$sprio" = 1001 ]; then
    echo "  the pair ran at priority $prio, the scout one ahead at $sprio"
else
    echo "FAIL expected priorities 1000 and 1001, got classical=$prio scout=$sprio"
    rc=1
fi
drain

echo ""
echo "=== with cores free nothing is preempted ==="
fill=$(fill_cores $((CORES - 2)))
echo "  $((CORES - 2)) one core jobs running, two cores free"
if pair=$(submit_pair); then
    scout=${pair%% *}; main=${pair##* }
    if flux job wait-event -t 120 "$main" clean </dev/null >/dev/null 2>&1; then
        echo "  the classical half ran on the free core"
    else
        echo "FAIL the classical half never started with a core free"; rc=1
    fi
    # shellcheck disable=SC2086
    victims=$(victims_of $fill | wc -l)
    if [ "$victims" -eq 0 ]; then
        echo "  nothing was preempted"
    else
        echo "FAIL $victims job(s) preempted though the pair fit"; rc=1
    fi
else
    echo "FAIL no pair was created"; rc=1
fi
drain

echo ""
echo "=== the scout needs a free core, or the pair is refused ==="
fill=$(fill_cores "$CORES")
echo "  every core taken"
err=$(mktemp)
if flux submit --quantum-vendor mock -n1 -- true >/dev/null 2>"$err"; then
    echo "FAIL a pair was admitted with no core for its scout, it could never be released"
    rc=1
elif grep -q "no free core for the scout" "$err"; then
    echo "  refused, and the message says the scout has nowhere to run"
else
    echo "FAIL refused for the wrong reason"; sed 's/^/    /' "$err"; rc=1
fi
rm -f "$err"
drain

echo ""
echo "=== the lowest priority job goes first, not the youngest ==="
# the oldest filler has the lowest priority, every other one is younger
fill=$(fill_cores $((CORES - 1)) 8)
low=$(echo "$fill" | awk '{print $1}')
echo "  $((CORES - 1)) jobs running, the oldest ($low) at urgency 8"
if pair=$(submit_pair); then
    scout=${pair%% *}; main=${pair##* }
    if ! flux job wait-event -t 120 "$main" clean </dev/null >/dev/null 2>&1; then
        echo "FAIL the classical half never started"; rc=1
    fi
    # shellcheck disable=SC2086
    victims=$(victims_of $fill)
    if [ "$victims" = "$low" ]; then
        echo "  only the low priority job was preempted"
    else
        echo "FAIL expected only $low preempted, got: $victims"; rc=1
    fi
else
    echo "FAIL no pair was created"; rc=1
fi
drain

flux jobtap remove quantum.so 2>/dev/null

echo ""
echo "=== preemption test rc=$rc ==="
exit "$rc"
