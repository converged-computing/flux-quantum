#!/bin/bash
# Same pipeline as test-mock-e2e.sh, but against a running system instance with
# fluxion already loaded, where the scout and the classical can land on
# different nodes.
#
# Requires fluxion loaded with match-format=rv1, FLUX_CLI_PLUGINPATH set, and
# >=2 cores so the scout has a foothold while the classical holds its
# reservation.
set -u

: "${FLUX_QUANTUM_MOCK:=1}"; export FLUX_QUANTUM_MOCK
rc=0

# longer than the 60s guard in wrap.py, so its error surfaces first
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

echo "=== 1. instance is up ==="
flux resource list || exit 1

echo ""
echo "=== 2. fluxion is the scheduler (not sched-simple) ==="
flux module list | grep -E "sched-fluxion-(resource|qmanager)" || {
    echo "FAIL: fluxion modules not loaded"; exit 1; }

echo ""
echo "=== 3. the quantum CLI plugin is discovered ==="
flux submit --help 2>&1 | grep -q -- --quantum-vendor || {
    echo "FAIL: no --quantum options. Is FLUX_CLI_PLUGINPATH set?"; exit 1; }
flux submit --help 2>&1 | grep -- --quantum- | sed 's/^/    /'

# Remember how much log there is before we start. This is a long lived
# instance, so everything since boot is still in the ring buffer and an
# unrelated error from an hour ago is not our problem. Counting lines rather
# than parsing timestamps keeps this working whatever the log format is.
DMESG_MARK=$(flux dmesg 2>/dev/null | wc -l)

echo ""
echo "=== 4. one quantum submit -> held classical + scout ==="
SID="systest-$(date +%s)"
ERR=$(mktemp)
scout=$(flux submit --quantum-vendor mock --quantum-mock-session "$SID" -n1 \
        -- sh -c 'echo QUANTUM_SESSION=$QUANTUM_SESSION_ID' 2>"$ERR")
sed 's/^/    /' "$ERR"
main=$(grep -oE 'held classical job [0-9]+' "$ERR" | awk '{print $NF}')
rm -f "$ERR"
echo "    scout=$scout  classical=$main"
if [ -z "$main" ] || [ -z "$scout" ]; then
    echo "FAIL: the plugin did not produce a classical+scout pair"; exit 1
fi

echo ""
echo "=== 5. did the pair land on different nodes? (the interesting case) ==="
scout_host=$(flux job info "$scout" R 2>/dev/null | jq -r '.execution.nodelist[0] // "?"')
echo "    scout ran on: $scout_host"

echo ""
echo "=== 6. the scout opens the session, memos it, and releases the classical ==="
if flux job wait-event -t "$WAIT_TIMEOUT" "$main" clean </dev/null >/dev/null 2>&1; then
    out=$(flux job attach "$main" </dev/null 2>&1)
    main_host=$(flux job info "$main" R 2>/dev/null | jq -r '.execution.nodelist[0] // "?"')
    echo "    classical ran on: $main_host"
    if [ "$scout_host" != "$main_host" ] && [ "$scout_host" != "?" ]; then
        echo "    (cross-node handoff exercised -- no shared filesystem involved)"
    fi
    if echo "$out" | grep -q "QUANTUM_SESSION=$SID"; then
        echo "PASS: classical ran with the vendor-supplied session ($SID)"
    else
        echo "FAIL: classical ran but the session was wrong or missing:"
        echo "$out" | sed 's/^/    /'
        rc=1
    fi
else
    # a held job sits in SCHED, a released one reaches RUN
    st=$(flux jobs -no "{state}" "$main" 2>/dev/null)
    echo "FAIL: classical did not complete (state=$st)"
    if [ "$st" = "SCHED" ]; then
        echo "  -> still HELD: the scout never released it. Scout output:"
        flux job attach "$scout" </dev/null 2>&1 | sed 's/^/     /'
    else
        echo "  -> released but did not finish: the session handoff or the"
        echo "     wrapped program is the problem. Classical output:"
        flux job attach "$main" </dev/null 2>&1 | tail -20 | sed 's/^/     /'
    fi
    rc=1
fi

echo ""
echo "=== 7. the session on the classical job's eventlog ==="
# the memo is the handoff, so show it
if flux job eventlog "$main" 2>/dev/null | grep -q '"quantum_session"'; then
    flux job eventlog "$main" | grep memo | sed 's/^/    /'
else
    echo "    no session memo found on the eventlog"
    [ "$rc" -eq 0 ] || echo "    -> the scout failed before posting it; see its output above"
    rc=1
fi

echo ""
echo "=== 8. instance log errors during the run ==="
# only what was logged after the mark, and rexec complaints are not ours. They
# come from flux exec, which the scout and the classical never use.
errs=$(flux dmesg 2>/dev/null | tail -n +$((DMESG_MARK + 1)) \
    | grep -iE "\.err\[[0-9]+\]|: error:|fatal" \
    | grep -v "rexec" || true)
if [ -n "$errs" ]; then
    echo "FAIL: errors logged:"; echo "$errs" | sed 's/^/    /'; rc=1
fi

echo ""
echo "=== system-instance test rc=$rc ==="
exit "$rc"
