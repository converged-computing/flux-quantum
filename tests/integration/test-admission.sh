#!/bin/bash
# The jobtap plugin keeps a core budget over every unfinished pair, held or
# running, and rejects a pair that would exceed it. Launching a scout commits
# money, so the classical half has to be placeable.
#
#     flux start -s1 bash tests/integration/test-admission.sh

set -u
export FLUX_QUANTUM_MOCK=1
HERE=$(cd "$(dirname "$0")/../.." && pwd)
export FLUX_CLI_PLUGINPATH="$HERE/cli-plugins"
rc=0

echo "=== build and load with a small budget ==="
make -s -C "$HERE/flux_quantum/jobtap" || { echo "FAIL build"; exit 1; }
# 20 cores for quantum, none held back, so a 4 core pair takes 5 and four fit
flux jobtap load "$HERE/flux_quantum/jobtap/quantum.so" \
    vendors="mock,ibm,braket" total_cores=20 reserve_cores=0 \
    protect_types="qpu" || {
    echo "FAIL load"; exit 1; }
flux jobtap list

echo ""
echo "=== a pair is accounted for as its cores plus one for the scout ==="
# submitted held, so it stays in the budget without running
ids=""
for i in 1 2 3 4; do
    if id=$(flux submit --quantum-vendor mock -n4 sleep 300 2>&1 | tail -1); then
        ids="$ids $id"
        echo "  pair $i admitted"
    else
        echo "FAIL pair $i should have been admitted, budget is 20 and 4 pairs need 20"
        rc=1
    fi
done

echo ""
echo "=== the fifth is rejected rather than admitted and left to wait ==="
if err=$(flux submit --quantum-vendor mock -n4 sleep 300 2>&1); then
    echo "FAIL the fifth pair was admitted, the budget is not being enforced"
    rc=1
else
    if echo "$err" | grep -q "no room for another pair"; then
        echo "  rejected, and the message says why"
        echo "$err" | grep -o "no room for another pair.*" | head -1 | sed 's/^/    /'
    else
        echo "FAIL rejected for the wrong reason: $err"
        rc=1
    fi
fi

echo ""
echo "=== a pair that does not fit at all is rejected on its own ==="
if flux submit --quantum-vendor mock -n64 sleep 300 >/dev/null 2>&1; then
    echo "FAIL a 64 core pair should not fit a 20 core budget"; rc=1
else
    echo "  rejected"
fi

echo ""
echo "=== the budget comes back as pairs finish ==="
# shellcheck disable=SC2086
for id in $ids; do flux cancel "$id" 2>/dev/null; done
for _ in $(seq 1 30); do
    flux jobs -no "{id}" 2>/dev/null | grep -q . || break
    sleep 1
done
if flux submit --quantum-vendor mock -n4 sleep 1 >/dev/null 2>&1; then
    echo "  a new pair is admitted again"
else
    echo "FAIL the budget did not recover, cores are leaking on job.destroy"
    rc=1
fi

echo ""
echo "=== the total rebuilds itself when the plugin is reloaded ==="
# job.new is replayed for active jobs, which is how the budget survives a
# restart. Hold two pairs, reload, and check the budget is still spent.
held=""
for i in 1 2; do
    id=$(flux submit --quantum-vendor mock -n8 sleep 300 2>&1 | tail -1) && held="$held $id"
done
flux jobtap remove quantum 2>/dev/null
flux jobtap load "$HERE/flux_quantum/jobtap/quantum.so" \
    vendors="mock" total_cores=20 reserve_cores=0
if flux submit --quantum-vendor mock -n8 sleep 300 >/dev/null 2>&1; then
    echo "FAIL after reload the budget was forgotten, so a third pair got in"
    rc=1
else
    echo "  still full after reload, so job.new replay rebuilt the total"
fi
# shellcheck disable=SC2086
for id in $held; do flux cancel "$id" 2>/dev/null; done

echo ""
echo "=== both halves of a pair are marked protected, by the plugin ==="
err=$(mktemp)
scout=$(flux submit --quantum-vendor mock -n2 sleep 60 2>"$err")
main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
rm -f "$err"
for half in "classical:$main" "scout:$scout"; do
    name=${half%%:*}; id=${half#*:}
    [ -z "$id" ] && { echo "FAIL no $name id"; rc=1; continue; }
    if flux job info "$id" jobspec 2>/dev/null | grep -q '"protected"'; then
        echo "  $name is protected"
    else
        echo "FAIL the $name half was not marked protected, so it is preemptible"
        rc=1
    fi
done
flux cancel "$main" "$scout" 2>/dev/null

echo ""
echo "=== a submitter cannot mark their own job protected ==="
if err=$(flux submit --setattr=system.protected=quantum -n1 true 2>&1); then
    echo "FAIL a user set the protection flag and was allowed to"
    rc=1
else
    if echo "$err" | grep -q "set by the scheduler"; then
        echo "  rejected, and the message says why"
    else
        echo "FAIL rejected for the wrong reason: $err"
        rc=1
    fi
fi

echo ""
echo "=== an ordinary job is left preemptible ==="
id=$(flux submit -n1 sleep 30)
if flux job info "$id" jobspec 2>/dev/null | grep -q '"protected"'; then
    echo "FAIL an ordinary job was marked protected"
    rc=1
else
    echo "  not protected, so it can be preempted"
fi
flux cancel "$id" 2>/dev/null

echo ""
echo "=== vendor policy still applies ==="
if flux submit --quantum-vendor rigetti -n1 true >/dev/null 2>&1; then
    echo "FAIL an unconfigured vendor was accepted"; rc=1
else
    echo "  unconfigured vendor rejected"
fi

flux jobtap remove quantum 2>/dev/null
echo ""
echo "=== admission test rc=$rc ==="
exit "$rc"
