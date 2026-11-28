#!/usr/bin/env python3
"""Example classical workload for IonQ.

Reads the session the scout opened and the target it was given, submits a
Bell circuit into the session, waits for it and prints the histogram. This
is what the classical half of a pair does. The key comes from the user's
environment, the rest from the scout.

    flux submit --quantum-vendor ionq --quantum-dry-run -n1 \\
        -- flux python examples/ionq/workload.py
"""

import json
import os
import sys
import time
import urllib.request

BELL = {
    "qubits": 2,
    "gateset": "qis",
    "circuit": [
        {"gate": "h", "target": 0},
        {"gate": "cnot", "control": 0, "target": 1},
    ],
}


def call(method, path, body=None):
    url = (os.environ.get("IONQ_API_URL") or "https://api.ionq.co/v0.4").rstrip("/")
    req = urllib.request.Request(
        url + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
    )
    req.add_header("Authorization", "apiKey " + os.environ["IONQ_API_KEY"])
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def main():
    session = os.environ.get("QUANTUM_SESSION_ID")
    if not session:
        sys.exit("no QUANTUM_SESSION_ID, this job was not started by the scout")
    if not os.environ.get("IONQ_API_KEY"):
        sys.exit("no IONQ_API_KEY in the environment")
    target = os.environ.get("IONQ_BACKEND", "simulator")
    print("session:", session)
    print("target: ", target)

    body = {
        "type": "ionq.circuit.v1",
        "name": "flux-quantum-bell",
        "shots": 100,
        "backend": target,
        "input": BELL,
    }
    # a probe hold hands over a job id, not a session, and a job id is not
    # something another job can be submitted into
    if not session.startswith("job:"):
        body["session_id"] = session
    noise = os.environ.get("IONQ_NOISE_MODEL")
    if noise:
        body["noise"] = {"model": noise}

    job = call("POST", "/jobs", body)
    print("job:", job["id"], job.get("status"))
    while True:
        state = call("GET", "/jobs/{}".format(job["id"]))
        if state.get("status") in ("completed", "failed", "canceled"):
            break
        time.sleep(2)
    print("status:", state.get("status"))
    if state.get("status") == "completed":
        print("results:", call("GET", "/jobs/{}/results".format(job["id"])))


if __name__ == "__main__":
    main()
