# flux-quantum with QRMI

This container builds a single node Flux instance you can start, and test a pipeline for Flux with QRMI first without a token (mock) and then run one cheap coscheduled job against an IBM QPU through QRMI. Feel free to tweak that and run a more expensive one, if you please.

| component     | version                                  | why?                                    |
|---------------|------------------------------------------|-----------------------------------------|
| ubuntu        | 24.04, python 3.12                       | qrmi needs python 3.11 or newer         |
| flux-security | 0.15.0                                   | release tarball                         |
| flux-core     | 0.87.0                                   | release tarball, prefix /usr            |
| flux-sched    | vsoch/flux-sched branch `hold`           | git, cmake, sysconfdir /etc             |
| flux-quantum  | branch `braket-hybrid`, or this checkout | git clone like the cluster, editable    |
| qrmi          | latest `qrmi[ibm]` on PyPI, with qiskit  | pip                                     |
| ibmcloud cli  | latest                                   | clis.cloud.ibm.com                      |

Since this is in a container, we have no systemd or munge. Keep in mind these would be part of a production setup, and are part of the setup where we are developing. The instance is started with `flux start`, run as the `ubuntu` user, and the scheduler is configured through `--config-path` with the same two config tables the cluster writes to `/etc/flux/system/conf.d`. Everything the scout and the classical job do is the same.

## 1. Build

From the repository root: 

```bash
uri=ghcr.io/converged-computing/flux-quantum:qrmi-demo-10-8-2026
docker build -f examples/qrmi/container/Dockerfile -t $uri .
```

flux-quantum is cloned from the `braket-hybrid` branch, the same clone the
cluster's start-script makes, so the image runs what is pushed. To put this
checkout in instead, uncommitted changes and all, pass an empty ref. The
build context is the repository root either way, and `Dockerfile.dockerignore`
keeps the build trees out of it.

```bash
docker build -f examples/qrmi/container/Dockerfile -t flux-quantum-qrmi \
    --build-arg FLUX_QUANTUM_REF= .
```

A cloned branch has to carry this directory already, since the scripts are
installed from it. Until it is pushed, build from the checkout.

To build another flux-sched branch or flux-core, or to pin qrmi:

```bash
docker build -f examples/qrmi/container/Dockerfile -t flux-quantum-qrmi \
    --build-arg FLUX_SCHED_BRANCH=add-hold \
    --build-arg FLUX_CORE_VERSION=0.87.0 \
    --build-arg QRMI_SPEC='qrmi[ibm]==0.26.0' .
```

## 2. Design

Let's talk about the coscheduling design. We add a flux quantum client plugin, which is an interface to customize flux commands for submit, run, batch, etc. With flux-quantum, we can add flags however we like that will say "I want to run a quantum job." Our strategy is simple. We take a request for hybrid quantum work (classical and quantum resources) and split it into a hybrid pair.

- A classical job that is "held" until specifically told to release (a form of reservation)
- A scout job or probe that will create a quantum session, monitor it, and release the classical resources when it is ready.

With this approach, no classical resources are running until the quantum is ready. Smaller jobs can backfill into the space with lower priority that can be pre-empted. The design uses priority and ensures that we do not accept more hybrid jobs than could be supported given the size of the cluster resources, and contender pre-emptible jobs. Full experiments to demonstrate the approach are pending - we have only mocked the setup due to funding limitations.

## 3. Run the container

The pair runs as the user (you) and for this case you are the Ubuntu user in the container. QRMI reads the credentials from your environment, so the credentials come in as environment variables. For the token free
mock, none are needed.

```bash
docker run -it --rm $uri
```

You will be in a Flux instance. Take a look.

```bash
flux resource list
```
```console
     STATE NNODES NCORES NGPUS NODELIST
      free      1      8     0 eda311ea73f7
 allocated      0      0     0 
      down      0      0     0 
```

Take a look to see that fluxion is handling scheduling.

```bash
flux module list
```

Take a look that we have the quantum plugin loaded.

```bash
flux jobtap list
quantum.so
```
```bash
$ flux jobtap query quantum.so | jq
{
  "name": "quantum.so",
  "path": "/etc/flux/system/jobtap/quantum.so",
  "total_cores": 16,
  "reserve_cores": 0,
  "preempt_after": 0.0,
  "vendors": "ibm,mock,braket,ionq",
  "protect_types": "qpu",
  "free_cores": 16,
  "preemptible_cores": 0,
  "promised_cores": 0,
  "tracked_jobs": 0
}
```

The plugin above is for validation, ensuring that we can support a vendor, that the request is feasible given that size of the cluster and the plugin will also be able to restore state on reload of Flux.  Note that when you log in, the entrypoint is a `flux start --config-path=... flux-quantum-init` and note the config path. The config is what loads the jobtap plugin, and configures the job manager.

```toml
[sched-fluxion-qmanager]
queue-policy = "coschedule"

[job-manager]
plugins = [
  { load = "/etc/flux/system/jobtap/quantum.so",
    conf = { vendors = "ibm,mock,braket,ionq", protect_types = "qpu",
             total_cores = 8, reserve_cores = 0 } }
]
```

We use a `coschedule` queue policy that lets a held job reserve.


## 4. Run a Mock Example

Let's start with a mock example, and check the setup before anything else:

```bash
flux-quantum-check            # fluxion, policy, jobtap, cli plugin, graph, qrmi
flux-quantum-check --ibm      # also the credentials, by name
```

Here is a full demo without you having to think (mocked)

```bash
flux-quantum-demo mock
```

Some notes on the output:

- **the submit** returns the scout id on stdout and says `held classical job <id>` on stderr. One submit, two jobs.
- **flux jobs** shows the classical in `SCHED` while the scout runs. It is held, with its cores reserved, until the scout releases it.
- **the scout's output** says it opened the session, waited for priority, released the classical, then waited for the classical to finish before closing the session. The qpu vertex is allocated to the scout the whole time.
- **the classical eventlog** carries a `memo` with `quantum_session` before its `alloc`. That memo is the handoff. No shared filesystem is involved, which is why the two halves can land on different nodes.
- **the classical's output** prints the session it was given.

And here is doing it by hand:

```bash
flux submit --quantum-vendor mock -n1 -- flux python /opt/flux-quantum/examples/qrmi/container/workload.py

# Look at the jobs to see the scout and classical
flux jobs -a
flux job attach <scout-id>
flux job eventlog <classical-id> | grep memo
flux job attach <classical-id>
```

`FLUX_QUANTUM_MOCK_QUEUE=30 flux submit ...` gives the mock a queue so the scout visibly waits, and `flux-quantum-start -s 2` gives the pair two ranks to land on.

## 5. IBM through QRMI

### Credentials

QRMI reads its configuration from the environment. The easiest thing is just to export them to the environment in the container. Exporting them in the shell inside the running instance is enough. flux copies your environment into both halves of the pair, and the ingest validator checks, by name, that the variables the chosen resource needs are in the job's environment. It never reads the values and has no credentials of its own. Note that you usually need to login to ibmcloud. This does not usually work for me and I will copy paste the quick credentials from the IBM Cloud interface.

```bash
export IBM_CLOUD_TOKEN=...
ibmcloud login --apikey "$IBM_CLOUD_TOKEN"
export IBM_CLOUD_CRN=$(ibmcloud resource service-instances --service-name quantum-computing --output json | jq -r '.[0].crn')
export RESOURCE=ibm_kingston
```

You can check credentials are set:

### Choose a backend

Then choose a backend:

```bash
flux python -c "
from qiskit_ibm_runtime import QiskitRuntimeService
import os
svc = QiskitRuntimeService(channel='ibm_quantum_platform',
                           token=os.environ['IBM_CLOUD_TOKEN'], instance=os.environ['IBM_CLOUD_CRN'])
for b in svc.backends(operational=True, simulator=False):
    print(b.name, b.status().pending_jobs)"
```

Export given your choice:

```bash
export ibm_kingston_QRMI_IBM_QRS_ENDPOINT=https://quantum.cloud.ibm.com/api/v1
export ibm_kingston_QRMI_IBM_QRS_IAM_ENDPOINT=https://iam.cloud.ibm.com
export ibm_kingston_QRMI_IBM_QRS_IAM_APIKEY=$IBM_CLOUD_TOKEN
export ibm_kingston_QRMI_IBM_QRS_SERVICE_CRN=$IBM_CLOUD_CRN
```

```bash
flux-quantum-check --ibm
```

Your plan has to allow sessions. The `qiskit-runtime-service` type opens a real session, which IBM permits on plans that support them. On a plan that does not, IBM answers 403, the scout explains, and cancels the held
classical job. 


### Do a test run

`ibm-run` submits a classical job that prints the session and exits. The billed window is the one shot warm-up task plus a few seconds, and it ends with what IBM says it charged.

```bash
RESOURCE=ibm_kingston ibm-run
SKIP_WARMUP=1 ibm-run           # free: opens and closes a session, no task
```

### Do a session run

`flux-quantum-demo ibm` is the same pair with this directory's `workload.py` as the classical half. The workload joins the session the scout opened, transpiles a two qubit Bell circuit for the device, runs it
through the QRMI SamplerV2 with 100 shots, and prints the counts. It asks before submitting.

```bash
RESOURCE=ibm_kingston flux-quantum-demo ibm
SHOTS=50 WALLTIME=3m flux-quantum-demo ibm -y
```

The above shows a lot of how it works and metadata. If you need to debug further, in another terminal you can shell into the container to check the queue:

```bash
docker exec -it sweet_mclean bash

# This will be at a similar path
flux proxy local:///tmp/flux-2Ge9tP/local-0 bash
flux resource list
```
```console
STATE NNODES NCORES NGPUS NODELIST
      free      1      7     0 1dcdc3d5f315
 allocated      1      1     0 1dcdc3d5f315
      down      0      0     0 
```
Export your access key again:

```bash
export IBM_CLOUD_TOKEN=...
export IBM_CLOUD_CRN=$(ibmcloud resource service-instances --service-name quantum-computing --output json | jq -r '.[0].crn')
```

Then look at the queue:

```bash
$ flux python -c "                                                   
import os
from qiskit_ibm_runtime import QiskitRuntimeService
svc = QiskitRuntimeService(channel='ibm_quantum_platform', token=os.environ['IBM_CLOUD_TOKEN'], instance=os.environ['IBM_CLOUD_CRN'])
print('pending on ibm_kingston:', svc.backend('ibm_kingston').status().pending_jobs)
for j in svc.jobs(limit=3): print(j.job_id(), j.status())"
```

For my testing, I saw previous jobs, and the queue depth.

```bash
qiskit_runtime_service._discover_account:WARNING:2026-10-08 21:11:12,194: Loading account with the given token. A saved account will not be used.
pending on ibm_kingston: 13
db40assvf2bc73ctt390 QUEUED
d9uj04535hes73fk0950 DONE
d9uip7343mgs73et1g4g CANCELLED
```

To show the command here that the script prints:

```bash
flux submit -t 5m --quantum-vendor ibm --quantum-device ibm_kingston --quantum-wait 1800 \
    -n1 -- flux python examples/qrmi/container/workload.py --shots 100
flux jobs -a
flux job attach <scout-id>               # a 403 or a credential problem shows up here
flux job eventlog <classical-id> | grep memo
flux job attach <classical-id>           # metadata, circuit, status, counts
```

`--quantum-wait` is how long the scout waits for the one shot warm-up task to leave the queue. An IBM session goes active when its first task reaches the head of the queue.  If it times out the scout closes the session and cancels the classical.
