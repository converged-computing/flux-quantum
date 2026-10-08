#!/usr/bin/env python3
"""Example classical workload.

Reads the session the scout opened and the QPU it was given, then would run the
quantum part. Nothing here is flux specific. The same script works under the
Slurm and LSF QRMI plugins, because they set the same two variables.
"""

import os
import sys


def main():
    session = os.environ.get("QUANTUM_SESSION_ID")
    if not session:
        sys.exit("no QUANTUM_SESSION_ID, this job was not started by the scout")
    print("session:", session)

    try:
        from qrmi import get_job_qpu_resources_and_types
    except ImportError:
        sys.exit("qrmi is not installed, pip install 'qrmi[ibm]'")

    resources, types = get_job_qpu_resources_and_types()
    print("qpu resources:", resources)
    print("qpu types:", types)

    # From here the real work would build a payload and call task_start on the
    # resource. The session is already open and the classical side already has
    # its nodes, so nothing waits on anything.
    print("both halves are allocated, ready to submit quantum tasks")


if __name__ == "__main__":
    main()
