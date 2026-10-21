#!/bin/bash
# The static scheduling graph, end to end in a throwaway instance. Run me as
#
#   source tests/integration/static-graph.sh
#   dir=$(make_static_config 2 "mock" 2)
#   FLUX_QUANTUM_MOCK=1 flux start -s2 -o,--config-path=$dir/conf \
#       bash tests/integration/test-static-graph.sh
#
# This is the free local proof for the two things the design rests on:
#   1. fluxion's JGF reader accepts a graph we generate whose ranks match the
#      broker's R, qdevice vertices included, arriving via resource.acquire.
#   2. jobs allocate AND free against it, repeatedly, with no planner errors.
# Plus the payoffs: feasibility bootstraps itself from notify (qdevices
# included, no add_subgraph), and a mock pair runs end to end.
set -e
cd "$(dirname "$0")/../.."
source tests/integration/fluxion.sh
export FLUX_CLI_PLUGINPATH=${FLUX_CLI_PLUGINPATH:-$PWD/cli-plugins}
export PYTHONPATH=$PWD${PYTHONPATH:+:$PYTHONPATH}  # the CLI plugin imports flux_quantum
export FLUX_QUANTUM_MOCK=1

fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

step "1. fluxion loads and receives the configured scheduling graph"
ensure_fluxion || fail "fluxion did not load"
flux dmesg | grep -i "sched-fluxion-resource.*err" && fail "fluxion logged errors on load" || true

step "2. the live graph is the static graph: nodes, cores and qdevices, all reachable"
flux python - <<'EOF'
import flux
from flux_quantum.graph import get_live_graph, orphaned_vendors, vendors_present

g = get_live_graph(flux.Flux())
types = {n["metadata"]["type"] for n in g["nodes"]}
assert "node" in types and "core" in types, types
assert "qdevice_mock" in types and "qpu" in types, types
assert not orphaned_vendors(g), orphaned_vendors(g)
print("    vendors in the graph:", sorted(vendors_present(g)))
ranks = {n["metadata"].get("rank") for n in g["nodes"] if n["metadata"]["type"] == "node"}
print("    node ranks:", sorted(ranks))
EOF

step "3. plain jobs allocate and free against it, repeatedly, on every rank"
NNODES=$(flux resource list -no "{nnodes}")
for i in 1 2 3; do
    flux run -N "$NNODES" -n "$NNODES" /bin/true || fail "spread job $i did not run"
done
for i in 1 2 3; do
    flux run -n1 /bin/true || fail "job $i did not run"
done
ALLOC=$(flux python -c "import flux; from flux_quantum.graph import allocated_cores; print(allocated_cores(flux.Flux()))")
[ "$ALLOC" = "0" ] || fail "cores still allocated after free: $ALLOC"

step "4. no planner errors after repeated allocate/free"
flux dmesg | grep -E "planner_multi_rem_span|run_sched_loop|cancel_vertex" \
    && fail "planner errors in dmesg" || echo "    dmesg clean"

step "5. a mock pair runs end to end through flux submit"
ERR=$(mktemp)
OUT=$(flux submit --quantum-vendor mock -n1 \
    sh -c 'echo session=$QUANTUM_SESSION_ID' 2>"$ERR") || { cat "$ERR"; fail "pair submit failed"; }
CLASSICAL=$(grep -o "held classical job [0-9]*" "$ERR" | awk '{print $4}')
[ -n "$CLASSICAL" ] || { cat "$ERR"; fail "no held classical id"; }
rm -f "$ERR"
flux job wait-event -t 120 "$CLASSICAL" clean >/dev/null || fail "classical never finished"
flux job attach "$CLASSICAL" | grep -q "session=." || fail "no session reached the classical"
echo "    pair OK (scout $OUT classical $CLASSICAL)"

step "6. feasibility bootstrapped itself from notify, qdevices included"
if flux module list | grep -q sched-fluxion-feasibility; then
    flux python - <<'EOF'
import errno, flux
from flux_quantum.launch import build_scout_jobspec
from flux_quantum.graph import get_live_graph
import json

h = flux.Flux()
live = get_live_graph(h)
ok = build_scout_jobspec("mock", 1, live_graph=live)
h.rpc("feasibility.check", {"jobspec": ok}).get()  # a json object, not a string
print("    mock scout: feasible")
bad = build_scout_jobspec("mock", 1, live_graph=live)
for r in bad["resources"]:
    if r["type"].startswith("qdevice_"):
        r["type"] = "qdevice_nosuchvendor"
try:
    h.rpc("feasibility.check", {"jobspec": bad}).get()
except OSError as exc:
    assert exc.errno in (errno.ENODEV, errno.EINVAL), exc
    print("    unknown vendor: refused ({})".format(errno.errorcode[exc.errno]))
else:
    raise SystemExit("    feasibility accepted a vendor that is not in the graph")
EOF
else
    echo "    SKIP: feasibility module not loaded"
fi

step "7. still no planner errors, graph still sound"
flux dmesg | grep -E "planner_multi_rem_span|run_sched_loop|cancel_vertex" \
    && fail "planner errors in dmesg" || echo "    dmesg clean"
flux python -c "
import flux
from flux_quantum.graph import get_live_graph, orphaned_vendors
assert not orphaned_vendors(get_live_graph(flux.Flux()))
print('    graph sound')
"

echo
echo "PASS: the static scheduling graph holds up end to end"
