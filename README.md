# flux-quantum

Quantum and classical coscheduling for Flux. One `flux submit` produces a pair.
The user's work is submitted held, and a small scout job that requests the vendor
device opens the vendor session, hands the session id to the held job over its
eventlog, and releases it. Developed against flux-core 0.87.0 and the `add-hold`
branch of https://github.com/vsoch/flux-sched, and also loads against flux-core 0.89.0.

## Components

- **flux_quantum/cli.py** submit plugin. Runs as the user, picks a vendor, submits the held classical job and turns the submit itself into the scout.
- **flux_quantum/backends/** one module per vendor, IBM through QRMI, AWS Braket, and a mock. They probe queue depth and cost and open and close the session. They only ever run as the user.
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
flux python -m flux_quantum.populate ibm braket
```

Build and load the jobtap plugin. `flux jobtap query quantum.so` shows the settings in force and the capacity numbers.

```bash
make -C flux_quantum/jobtap
flux jobtap load $PWD/flux_quantum/jobtap/quantum.so vendors=ibm,braket \
    total_cores=128 reserve_cores=32 protect_types=qpu preempt_after=30
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
bash tests/integration/test-system-instance.sh                  # against a running instance
```

The e2e and discovery scripts load fluxion themselves. The jobtap and system instance scripts expect fluxion to be the scheduler already, as it is in the CI image. `tests/integration/setup-mock-registry.sh` adds `qdevice_*` vertices to a live graph so the selector has something to discover.

## License

flux-quantum is distributed under the terms of the MIT license.
All new contributions must be made under this license.
See [LICENSE](LICENSE), [COPYRIGHT](COPYRIGHT), and [NOTICE](NOTICE) for details.
SPDX-License-Identifier: (MIT)
LLNL-CODE- 842614
