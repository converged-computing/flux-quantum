#!/bin/bash
# One cheap end to end run against IBM, and a report of what it cost.
#
#   export IBM_CLOUD_TOKEN=<ibm cloud api key>
#   ibmcloud login --apikey "$IBM_CLOUD_TOKEN"
#   ibm-run
#
# Override anything with an env var, for example
#   RESOURCE=ibm_kingston NTASKS=4 ibm-run
#   SKIP_WARMUP=1 ibm-run          free, opens and closes a session, no task
#   SLEEP=5 ibm-run                let the warmup task finish instead of being
#                                  cancelled when the session closes

set -u

NTASKS="${NTASKS:-1}"      # flux -n is tasks, not nodes. One is enough here.
# How long the classical job stays alive once it has the session. Zero means
# print and exit, which is cheapest, but the session then closes while the
# warmup task is still running so the warmup ends up CANCELLED. Around 5
# seconds lets it finish instead. Every second of this is billed.
SLEEP="${SLEEP:-0}"
WALLTIME="${WALLTIME:-1m}"

# waiting is free, so be generous
WARMUP_TIMEOUT="${WARMUP_TIMEOUT:-1800}"   
SKIP_WARMUP="${SKIP_WARMUP:-0}"
OUT="${OUT:-$HOME/ibm-run-$(date +%Y%m%d-%H%M%S)}"

say() { echo "== $* =="; }
die() { echo "$*" >&2; exit 1; }

# 1. credentials and CRN
[ -n "${IBM_CLOUD_TOKEN:-}" ] || die "set IBM_CLOUD_TOKEN to your ibm cloud api key"

if [ -z "${IBM_CLOUD_CRN:-}" ]; then
    command -v ibmcloud >/dev/null || die "need the ibmcloud cli, or set IBM_CLOUD_CRN"
    command -v jq >/dev/null || die "need jq"
    say "looking up the quantum instance"
    crns=$(ibmcloud resource service-instances --service-name quantum-computing \
               --output json 2>/dev/null | jq -r '.[].crn')
    n=$(printf '%s\n' "$crns" | grep -c .)
    [ "$n" -eq 0 ] && die "no quantum instances. logged in?  ibmcloud login --apikey \"\$IBM_CLOUD_TOKEN\""
    if [ "$n" -gt 1 ]; then
        # one per line, so picking blindly would export a multiline value
        echo "found $n instances, set IBM_CLOUD_CRN to the one you want:" >&2
        ibmcloud resource service-instances --service-name quantum-computing \
            --output json | jq -r '.[] | "  \(.name)  \(.crn)"' >&2
        exit 1
    fi
    IBM_CLOUD_CRN="$crns"
fi

export QISKIT_IBM_TOKEN="$IBM_CLOUD_TOKEN"
export QISKIT_IBM_INSTANCE="$IBM_CLOUD_CRN"
unset FLUX_QUANTUM_MOCK          
# mock would win --quantum-select on queue depth

# 2. pick the least busy QPU, unless told which
if [ -z "${RESOURCE:-}" ]; then
    say "picking the least busy QPU"
    RESOURCE=$(flux python -c "
from qiskit_ibm_runtime import QiskitRuntimeService
rows = []
for b in QiskitRuntimeService().backends(operational=True, simulator=False):
    try:
        rows.append((b.status().pending_jobs, b.name))
    except Exception:
        pass
for q, name in sorted(rows):
    print(name, q)
" 2>/dev/null | tee /dev/stderr | head -1 | awk '{print $1}')
fi
[ -n "$RESOURCE" ] || die "could not find a QPU. is the plan active?"

# QRMI prefixes every variable with the resource id
export ${RESOURCE}_QRMI_IBM_QRS_ENDPOINT="https://quantum.cloud.ibm.com/api/v1"
export ${RESOURCE}_QRMI_IBM_QRS_IAM_ENDPOINT="https://iam.cloud.ibm.com"
export ${RESOURCE}_QRMI_IBM_QRS_IAM_APIKEY="$IBM_CLOUD_TOKEN"
export ${RESOURCE}_QRMI_IBM_QRS_SERVICE_CRN="$IBM_CLOUD_CRN"
# session, not batch. Batch is cheaper but gives no exclusive access, and
# exclusive access is the thing being tested.
export ${RESOURCE}_QRMI_IBM_QRS_SESSION_MODE="dedicated"

mkdir -p "$OUT"
echo "resource $RESOURCE" | tee "$OUT/run.txt"
echo "crn      $IBM_CLOUD_CRN" >> "$OUT/run.txt"

say "credentials"
flux python -c "
from flux_quantum.backends import get_backend
ok, msg = get_backend('ibm').credentials_present()
print(msg)
raise SystemExit(0 if ok else 1)" | tee -a "$OUT/run.txt" || die "credentials incomplete"

# 3. submit. The classical job prints and exits, so the billed window is warmup plus a few seconds.
warmup_args="--quantum-ibm-warmup-timeout $WARMUP_TIMEOUT"
[ "$SKIP_WARMUP" = 1 ] && warmup_args="--quantum-ibm-skip-warmup"

say "submitting"
t0=$(date +%s)
err=$(mktemp)
scout=$(flux submit -t "$WALLTIME" --quantum-vendor ibm \
            --quantum-ibm-resource "$RESOURCE" $warmup_args -n"$NTASKS" \
            -- sh -c "echo classical got \$QUANTUM_SESSION_ID; sleep $SLEEP" 2>"$err")
tee -a "$OUT/run.txt" < "$err"
main=$(grep -oE 'held classical job [0-9]+' "$err" | awk '{print $NF}')
rm -f "$err"
[ -n "$main" ] || die "no held classical job, see above"
echo "scout=$scout classical=$main" | tee -a "$OUT/run.txt"

say "waiting. the queue wait is free, so this can take a while"
flux job wait-event -t "$((WARMUP_TIMEOUT + 600))" "$main" clean >/dev/null 2>&1
flux job wait-event -t 300 "$scout" clean >/dev/null 2>&1
t1=$(date +%s)

# 4. collect
say "results"
{
    echo "--- wall clock ---"
    echo "submit to both jobs done: $((t1 - t0))s"
    echo "--- scout ---"
    flux job attach "$scout" 2>&1
    echo "--- classical ---"
    flux job attach "$main" 2>&1
    echo "--- classical eventlog ---"
    flux job eventlog "$main" 2>&1
    echo "--- scout eventlog ---"
    flux job eventlog "$scout" 2>&1
} | tee -a "$OUT/run.txt"

say "what IBM billed"
# qpu_charge_time_seconds is what you are charged. usage_estimation is the
# finer grained figure. A CANCELLED warmup is normal when SLEEP is 0, it means
# the classical finished and we closed the session while the one shot task was
# still in flight. It had already done its job by reaching the head of the queue.
flux python -c "
import os
from qiskit_ibm_runtime import QiskitRuntimeService
svc = QiskitRuntimeService(channel='ibm_quantum_platform',
                           token=os.environ['QISKIT_IBM_TOKEN'],
                           instance=os.environ['QISKIT_IBM_INSTANCE'])
jobs = list(svc.jobs(limit=5))
if not jobs:
    print('no jobs, which is expected with SKIP_WARMUP=1')
total = 0
for j in jobs:
    m = {}
    try:
        m = j.metrics() or {}
    except Exception as e:
        print('metrics unavailable', e)
    charged = m.get('usage', {}).get('qpu_charge_time_seconds')
    est = None
    try:
        est = (j.usage_estimation or {}).get('quantum_seconds')
    except Exception:
        pass
    if charged:
        total += charged
    print(j.job_id(), j.status(), 'backend=%s' % j.backend().name,
          'session=%s' % getattr(j, 'session_id', None),
          'charged_s=%s' % charged, 'estimate_s=%s' % est)
if total:
    print()
    print('charged %ss, about \$%.2f at 1.60 a second' % (total, total * 1.60))
" 2>&1 | tee -a "$OUT/run.txt"

echo
echo "saved to $OUT/run.txt"
