# flux-quantum

Quantum and classical coscheduling for Flux. One `flux submit` produces a pair.
The user's work is submitted held, and a small scout job that requests the vendor
device opens the vendor session, hands the session id to the held job over its
eventlog, and releases it. Developed against flux-core 0.87.0 and the `add-hold`
branch of https://github.com/vsoch/flux-sched, and also loads against flux-core 0.89.0.

## Components

- **flux_quantum/cli.py** submit plugin. Runs as the user, picks a vendor, submits the held classical job and turns the submit itself into the scout.
- **flux_quantum/backends/** one module or package per vendor, IBM through QRMI, AWS Braket, IonQ over its REST API, and a mock. The common submit options are mapped to each vendor's terms there, so a user changes nothing but the vendor and the device. They probe queue depth and cost and open and close the session. They only ever run as the user.
- **flux_quantum/selector.py** discovers vendors from the graph and ranks them by policy.
- **flux_quantum/scout.py** and **wrap.py** the scout, and the wrapper that exports `QUANTUM_SESSION_ID` before the user's command runs.
- **flux_quantum/graph.py**, **qresource.py**, **populate.py** add `qdevice_<vendor> -> qpu` to the fluxion graph.
- **flux_quantum/jobtap/quantum.c** owner side policy in the job manager. Allowed vendors, admission against reachable cores, protection of both halves, and optional preemption. Holds no credentials.

## Install

The CLI plugin is found through `{confdir}/cli/plugins`, `{libexecdir}/cli/plugins`, or a directory on `FLUX_CLI_PLUGINPATH`.

```bash
flux python -m pip install -e .              # into the python flux uses
export FLUX_CLI_PLUGINPATH=$PWD/cli-plugins  # holds only the discovery shim
flux submit --help | grep quantum
```

`cli-plugins/` contains only `quantum.py`, a shim that imports the installed package. Do not point `FLUX_CLI_PLUGINPATH` at `flux_quantum/` itself, or flux tries to import every module as a plugin. For a system install copy the shim into `{confdir}/cli/plugins`.

Populate the graph once, before any job runs. Growing it while jobs hold resources corrupts fluxion.

```bash
flux python -m flux_quantum.populate ibm braket ionq
```

Build and load the jobtap plugin. `flux jobtap query quantum.so` shows the settings in force and the capacity numbers.

```bash
make -C flux_quantum/jobtap
flux jobtap load $PWD/flux_quantum/jobtap/quantum.so vendors=ibm,braket,ionq \
    total_cores=128 reserve_cores=32 protect_types=qpu preempt_after=30
```

## Submitting

The options are the same whichever vendor it is.

```
--quantum-vendor VENDOR   ibm, braket, ionq, or mock under FLUX_QUANTUM_MOCK
--quantum-select POLICY   pick the vendor instead: any, queue or cost
--quantum-device NAME     the vendor's name for the device: a Braket ARN, an IonQ
                          backend, a QRMI resource id. Each vendor has a default
--quantum-hold MODE       session takes the vendor's real hold, a hybrid job on
                          Braket, a session on IonQ and IBM. probe submits a front
                          of queue job and holds nothing. Default session
--quantum-hold-max SECS   give the hold up after this long. Default the job's
                          duration plus 300, or 900 without one
--quantum-wait SECS       give up and cancel the held job if the hold is not ours
                          after this long. Default 0, as long as the scout may run
--quantum-dry-run         run on the vendor's simulator. FLUX_QUANTUM_MOCK implies it
```

```bash
flux submit --quantum-vendor ionq --quantum-dry-run -n1 -- flux python examples/ionq/workload.py
flux submit --quantum-vendor braket --quantum-device arn:aws:braket:us-east-1::device/qpu/ionq/Forte-Enterprise-1 -t 30m -n4 -- ./work
flux submit --quantum-vendor ibm --quantum-device ibm_kingston --quantum-wait 600 -n4 -- flux python examples/qrmi/workload.py
```

Settings that tune an installation rather than describe a job are environment variables with the `FLUX_QUANTUM_` prefix, read at submit time. Each backend's docstring lists its own. The ones that matter most:

```
FLUX_QUANTUM_BRAKET_HOLD_INSTANCE   instance for the hybrid job, ml.m5.large
FLUX_QUANTUM_BRAKET_SHOTS           shots for the probe task, 1
FLUX_QUANTUM_IONQ_SHOTS             shots for the warm-up job, 100
FLUX_QUANTUM_IBM_TYPE               QRMI resource type, qiskit-runtime-service
FLUX_QUANTUM_IBM_SKIP_WARMUP        release as soon as the session opens
FLUX_QUANTUM_MOCK_SESSION           force the mock's session id
FLUX_QUANTUM_MOCK_QUEUE             the mock's simulated queue depth
```

## Testing

`FLUX_QUANTUM_MOCK=1` registers the `mock` and `mock_busy` backends, so the whole pipeline runs without a vendor token. Export it before `flux start`, since the ingest validator is a separate process. The unit tests need no broker.

```bash
pytest tests/unit
flux start bash tests/integration/test-mock-e2e.sh              # one submit, held classical and scout, handoff
flux start -s2 bash tests/integration/test-mock-e2e.sh          # the two halves on different ranks
FLUX_QUANTUM_MOCK=1 flux start bash tests/integration/test-admission.sh    # jobtap admission
FLUX_QUANTUM_MOCK=1 flux start bash tests/integration/test-preemption.sh   # jobtap preemption
FLUX_QUANTUM_MOCK=1 flux start bash tests/integration/test-reload.sh       # jobtap unload and reload
flux start bash tests/integration/test-select-discovery.sh      # vendors discovered from the graph
flux start bash tests/integration/test-ionq-e2e.sh              # the IonQ backend against a fake api.ionq.co
bash tests/integration/test-system-instance.sh                  # against a running instance
```

The e2e and discovery scripts load fluxion themselves. The jobtap and system instance scripts expect fluxion to be the scheduler already, as it is in the CI image. `tests/integration/setup-mock-registry.sh` adds `qdevice_*` vertices to a live graph so the selector has something to discover.

## IonQ

`flux_quantum/backends/ionq/` talks to the IonQ v0.4 REST API with the standard library and reads the key from `IONQ_API_KEY`. The session hold opens a session on the backend, warms it up with a one qubit job, and releases the classical job once IonQ reports the session active. The session id is what the classical job is handed, and jobs it submits with that id run inside the session. Sessions are in beta and not every account has them, and an account without them is told to use `--quantum-hold probe`, which submits the warm-up job alone and releases when it starts.

A dry run sends every job to the simulator, which IonQ does not charge for, with the noise model of the backend asked for, so `--quantum-device qpu.forte-1 --quantum-dry-run` runs on the simulator with Forte's noise. The simulator has no queue, so a dry run exercises the plumbing and not the hold.

```bash
export IONQ_API_KEY=...
flux submit --quantum-vendor ionq --quantum-dry-run -n1 -- flux python examples/ionq/workload.py
flux submit --quantum-vendor ionq --quantum-device qpu.forte-1 -n1 -- flux python examples/ionq/workload.py
```

`python -m flux_quantum.backends.ionq.fake` runs a stand-in for the service, so the backend and the whole pipeline run with no key at all. `IONQ_API_URL` points the backend at it. It listens on the loopback, so on an instance of more than one node start it with `--bind 0.0.0.0` and use the URL it prints, which the other nodes can reach. `FAKE_IONQ_QUEUE=30-90` gives it a device queue, seconds a job waits before it is served, which a started session's jobs skip, and `POST /fake/config` changes it while running.

## Braket priority probe

`tests/probe_hold.py` measures the thing this branch depends on: whether a task submitted from outside the hybrid job, carrying its token, is served ahead of the device queue. It opens the hold, submits a control task without the token, then the token task after it, and watches both until the token task finishes. Braket only reports a task's queue, Normal or Priority, and its position when `GetQuantumTask` is asked for `QueueInfo`, so the probe asks, and it reads the finishing order from the service timestamps. Both come from the tasks themselves. The device-wide counters lag a submit by minutes and are not used for the verdict.

```bash
python3 tests/probe_hold.py --survey                    # which QPUs are open and have a queue
python3 tests/probe_hold.py                             # SV1, shows only that the token associates
AWS_DEFAULT_REGION=us-east-1 python3 tests/probe_hold.py \
    --device arn:aws:braket:us-east-1::device/qpu/ionq/Forte-Enterprise-1 \
    --shots 100 --max-seconds 1800
```

`--max-seconds` is the hold, and the token task has to finish inside it, so give a device where tasks take minutes half an hour. The hold is released as soon as the token task finishes, and the control is then followed to its end unless `--cancel-control` is passed. Each run appends a JSON line to `probe-runs.jsonl` with both tasks' queue, position, timestamps and a timeline, and everything printed, the backend's progress included, is also appended to `probe-hold.log` (`--log PATH` to change it, `--log ''` to disable). It costs two tasks at the device rate plus the hold instance, and prints the estimate before creating anything. QuEra Aquila takes an analog program rather than a circuit, and the probe sends one when the ARN is QuEra's. `--check-program` runs the program on the local simulator first, free, and the analog simulator applies Aquila's own limits.

## License

flux-quantum is distributed under the terms of the MIT license.
All new contributions must be made under this license.
See [LICENSE](LICENSE), [COPYRIGHT](COPYRIGHT), and [NOTICE](NOTICE) for details.
SPDX-License-Identifier: (MIT)
LLNL-CODE- 842614
