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
- **flux_quantum/jobtap/quantum.c** This is the plugin that is going to actually define the scout job and select quantum for the graph based on the vendor, which is a type.

## Install / discovery (flux-core 0.87.0)

The CLI plugin is found by flux via `{confdir}/cli/plugins`, `{libexecdir}/cli/plugins`, or any directory on `FLUX_CLI_PLUGINPATH`. To install:

```bash
pip install -e .
export FLUX_CLI_PLUGINPATH=$PWD/flux_quantum
flux submit --help | grep quantum      # options should appear
```

For the jobtap plugin:  

```bash
make -C flux_quantum/jobtap  &&  flux jobtap load .../quantum.so
```

Still a WIP I need to test all this with flux-sched! And we need a cute logo. Very important.

## License

HPCIC DevTools is distributed under the terms of the MIT license.
All new contributions must be made under this license.

See [LICENSE](https://github.com/converged-computing/cloud-select/blob/main/LICENSE),
[COPYRIGHT](https://github.com/converged-computing/cloud-select/blob/main/COPYRIGHT), and
[NOTICE](https://github.com/converged-computing/cloud-select/blob/main/NOTICE) for details.

SPDX-License-Identifier: (MIT)

LLNL-CODE- 842614

