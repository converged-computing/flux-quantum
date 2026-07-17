# flux-quantum
Quantum + classical coscheduling for Flux, including jobtap, validation, and client plugins.
I am developing this against flux-core **flux-core 0.87.0** and my custom branch of flux
sched with support for hold. The quantum backend is external (e,g., IBM/QRMI, AWS Braket)
and informs the work for the flux-sched reservation being held for classical resources.
## Components
- **flux_quantum/cli.py** the CLI plugin that runs in user space and (if needed) can use credentials to look at queue depth or cost. 
- **flux_quantum/backends/** the vendor libraries. E.g., handling braket will be different than IBM or qrmi, and how we validate will vary. These also run only in user-space.
- **flux_quantum/selector.py** is generic discover and rank over backends.
- **flux_quantum/validator.py** is going to check that we have the right envars, etc. per a backend choice.
- **flux_quantum/jobtap/quantum.c** the owner-side policy gate (runs in the job manager, holds no credentials). At `job.validate` it enforces vendor policy, rejecting a vendor that isn't permitted/available. The scout and the vendor *selection* live in user space (the CLI plugin), since those need the user's credentials -- jobtap only mutates/enforces. (Selecting a vendor *type* in the graph is a future owned-hardware step; the external-vendor model has no qpu resource to select.)
## Install / discovery (flux-core 0.87.0)
The CLI plugin is found by flux via `{confdir}/cli/plugins`, `{libexecdir}/cli/plugins`, or any directory on `FLUX_CLI_PLUGINPATH`. To install:
```bash
flux python -m pip install -e .            # install into the python flux uses
export FLUX_CLI_PLUGINPATH=$PWD/cli-plugins # dir holds ONLY the discovery shim
flux submit --help | grep quantum          # options should appear
```
The plugin search dir (`cli-plugins/`) contains only `quantum.py`, a shim that
imports `QuantumCLIPlugin` from the installed package -- do NOT point
`FLUX_CLI_PLUGINPATH` at `flux_quantum/` itself, or flux will try to import the
package's internal modules as standalone plugins and fail. For a system install,
copy `cli-plugins/quantum.py` into `{confdir}/cli/plugins`.
For the jobtap plugin:  
```bash
make -C flux_quantum/jobtap  &&  flux jobtap load .../quantum.so
```
Still a WIP I need to test all this with flux-sched! And we need a cute logo. Very important.
## Testing (token-free)
No real vendor token needed. `FLUX_QUANTUM_MOCK=1` registers `mock`/`mock_busy`
backends (credentials always "present", no API calls) and the scout session is
mocked, so the whole pipeline runs without credentials.
- `testing/setup-mock-registry.sh` derives the real graph and injects `qvendor_*`
  markers so the selector has vendors to discover (markers are plain graph vertices
  added with `flux inject` -- no token).
- `testing/test-mock-e2e.sh` runs it end to end: plugin discovery -> registry ->
  `flux submit --quantum-vendor mock` (held) -> scout (mock session) -> handoff -> run.

## License
DevTools is distributed under the terms of the MIT license.
All new contributions must be made under this license.
See [LICENSE](https://github.com/converged-computing/cloud-select/blob/main/LICENSE),
[COPYRIGHT](https://github.com/converged-computing/cloud-select/blob/main/COPYRIGHT), and
[NOTICE](https://github.com/converged-computing/cloud-select/blob/main/NOTICE) for details.
SPDX-License-Identifier: (MIT)
LLNL-CODE- 842614
