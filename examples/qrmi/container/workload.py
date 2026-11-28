#!/usr/bin/env python3
"""The classical half of a pair, talking to the QPU through QRMI.

The scout has already opened the session and the wrapper has put it in our
environment, so there is nothing to wait for. This reads the session and the
resource the way a Slurm or LSF QRMI workload does, joins the session,
transpiles a Bell circuit for the device and runs it through the QRMI
SamplerV2, then prints the counts.

    flux submit -t 5m --quantum-vendor ibm --quantum-device ibm_kingston \\
        -n1 -- flux python examples/qrmi/container/workload.py --shots 100

Under the mock vendor there is no QPU and the script just reports the
session, so the same submit is the token free rehearsal.

What QRMI reads from the environment, all set before this runs:

    QUANTUM_SESSION_ID                       set by the wrapper
    <resource>_QRMI_JOB_ACQUISITION_TOKEN    the same value, QRMI uses it as
                                             the session id for task_start
    QRMI_JOB_QPU_RESOURCES, QRMI_JOB_QPU_TYPES   set at submit by the backend
    <resource>_QRMI_IBM_QRS_*                the credentials, from your shell
"""

import argparse
import os
import sys
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--shots", type=int, default=100)
    ap.add_argument(
        "--qubits", type=int, default=2, help="width of the GHZ/Bell circuit, 2"
    )
    args = ap.parse_args()

    session = os.environ.get("QUANTUM_SESSION_ID")
    if not session:
        sys.exit("no QUANTUM_SESSION_ID, this job was not started by the scout")
    print("session: ", session)
    print("job:     ", os.environ.get("FLUX_JOB_ID", "?"), "on", os.uname().nodename)

    if not os.environ.get("QRMI_JOB_QPU_RESOURCES"):
        # the mock vendor hands over a session and no resource. The handoff
        # worked, and that is all a rehearsal can show.
        print("resource: none, a rehearsal with no QPU. Nothing submitted.")
        return

    try:
        from qrmi import QuantumResource, get_job_qpu_resources_and_types
        from qrmi.primitives.ibm import SamplerV2, get_target
    except ImportError as e:
        sys.exit(
            "qrmi is not installed in flux python ({}), pip install 'qrmi[ibm]'".format(
                e
            )
        )
    try:
        from qiskit import QuantumCircuit
        from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
    except ImportError as e:
        sys.exit("qiskit is not installed ({}), it comes with qrmi[ibm]".format(e))

    from flux_quantum.backends.qrmi import resource_type

    resources, types = get_job_qpu_resources_and_types()
    resource, rtype = resources[0], types[0]
    print("resource:", resource, "type", rtype)
    token = os.environ.get(resource + "_QRMI_JOB_ACQUISITION_TOKEN")
    print("token:   ", token, "(matches session)" if token == session else "(DIFFERS)")

    # QRMI reads <resource>_QRMI_JOB_ACQUISITION_TOKEN as a preset session id,
    # so this resource submits into the scout's session without acquiring
    # one of its own. No acquire, no release, the scout owns the session.
    t0 = time.time()
    qr = QuantumResource(resource, resource_type(rtype))
    meta = qr.metadata()
    print(
        "metadata:",
        {
            k: meta[k]
            for k in sorted(meta)
            if k in ("session_id", "backend_name", "backend_status")
        },
    )
    if meta.get("session_id") not in (None, session):
        print("WARNING QRMI thinks the session is", meta.get("session_id"))

    # a Bell state, or a GHZ state if wider. The circuit has to be in the
    # device's basis before IBM accepts it, so transpile against the target
    # QRMI reads back from the backend.
    qc = QuantumCircuit(args.qubits)
    qc.h(0)
    for q in range(1, args.qubits):
        qc.cx(q - 1, q)
    qc.measure_all()
    target = get_target(qr)
    isa = generate_preset_pass_manager(optimization_level=1, target=target).run(qc)
    print("circuit: ", dict(isa.count_ops()), "after {:.1f}s".format(time.time() - t0))

    sampler = SamplerV2(qr, options={"default_shots": args.shots})
    job = sampler.run([isa])
    print("task:    ", job.job_id(), "submitted into the session")
    t1 = time.time()
    last = None
    while not job.in_final_state():
        status = job.status()
        if status != last:
            print("status:  ", status, "at {:.0f}s".format(time.time() - t1))
            last = status
        time.sleep(2)
    print("status:  ", job.status(), "after {:.0f}s".format(time.time() - t1))

    if job.errored():
        print("logs:")
        print(job.logs())
        sys.exit("the task failed")

    counts = job.result()[0].data.meas.get_counts()
    print("counts:  ", dict(sorted(counts.items(), key=lambda kv: -kv[1])))
    ideal = {"0" * args.qubits, "1" * args.qubits}
    good = sum(v for k, v in counts.items() if k in ideal)
    print("fidelity:", "{}/{} shots in {}".format(good, args.shots, sorted(ideal)))


if __name__ == "__main__":
    main()
