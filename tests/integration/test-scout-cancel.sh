#!/bin/bash
# A cancelled scout must get to its finally and close the session. The wait
# on the classical job runs in the reactor, where a SIGTERM used to be
# ignored until flux escalated to SIGKILL. This drives the wait directly,
# so it needs only a flux instance, no scheduler or plugin.
#
#     flux start -s1 bash tests/integration/test-scout-cancel.sh
set -u
HERE=$(cd "$(dirname "$0")/../.." && pwd)
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
rc=0
LOG=$(mktemp)
trap 'rm -f "$LOG"' EXIT

waiter() {
    exec flux python - "$1" <<'PY'
import sys, flux
from flux.job import JobID
from flux_quantum.scout import wait_for_job
try:
    wait_for_job(flux.Flux(), int(JobID(sys.argv[1])))
except SystemExit as e:
    print("unwound: %s" % e, flush=True)
    raise
print("returned", flush=True)
PY
}

echo "=== 1. SIGTERM while waiting unwinds, promptly ==="
job=$(flux submit sleep 120)
waiter "$job" >"$LOG" 2>&1 &
pid=$!
sleep 3  # the bindings take a moment to import, the watcher is not up before
t0=$(date +%s%N)
kill -TERM "$pid"
wait "$pid"; wrc=$?
dt=$(( ($(date +%s%N) - t0) / 1000000 ))
sed 's/^/    /' "$LOG"
if grep -q "unwound: quantum-scout: received signal 15" "$LOG"; then
    echo "  ok    unwound on SIGTERM"
else
    echo "  FAIL  did not unwind on SIGTERM (rc=$wrc)"; rc=1
fi
if [ "$dt" -lt 3000 ]; then
    echo "  ok    within ${dt}ms"
else
    echo "  FAIL  took ${dt}ms"; rc=1
fi
flux cancel "$job" 2>/dev/null

echo "=== 2. the wait returns when the job reaches clean ==="
job=$(flux submit true)
if timeout 30 bash -c "$(declare -f waiter); waiter $job" >"$LOG" 2>&1; then
    if grep -q "^returned" "$LOG"; then
        echo "  ok    returned after clean"
    else
        echo "  FAIL  no return"; cat "$LOG"; rc=1
    fi
else
    echo "  FAIL  the wait did not return"; cat "$LOG"; rc=1
fi

echo "=== 3. a job that already finished does not block ==="
flux job wait-event -t 10 "$job" clean >/dev/null 2>&1
if timeout 10 bash -c "$(declare -f waiter); waiter $job" >"$LOG" 2>&1 && grep -q "^returned" "$LOG"; then
    echo "  ok    returned on a finished job"
else
    echo "  FAIL  blocked or failed on a finished job"; cat "$LOG"; rc=1
fi
if [ "$rc" = 0 ]; then echo "=== PASS ==="; else echo "=== FAIL ==="; fi
exit "$rc"
