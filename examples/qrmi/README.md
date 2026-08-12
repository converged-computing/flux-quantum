# Running against IBM through QRMI

This is the demo for using qrmi with flux-quantum.

## Install

QRMI needs python 3.11 or newer. It is not a dependency of flux-quantum, so

    pip install "qrmi[ibm]"

Install it into whatever python `flux python` uses, on every node that could run
the scout. qiskit comes along with it and we need that for the warmup task.

## Credentials

QRMI reads its config out of the environment. Every variable is prefixed with the
resource id, so for ibm_kingston the api key lives in
`ibm_kingston_QRMI_IBM_QRS_IAM_APIKEY`. Four of them are required.

    cp env.example .env
    chmod 600 .env
    $EDITOR .env
    set -a && . ./.env && set +a

Check before you submit anything.

    flux python -c "
    from flux_quantum.backends import get_backend
    print(get_backend('ibm').credentials_present())"

That names any variable you are missing. It never prints a value.

## Submit

    flux submit -t 30m --quantum-vendor ibm --quantum-ibm-resource ibm_kingston \
        -n4 flux python examples/qrmi/workload.py

stdout is the scout id. The classical job id comes back on stderr. If only one
resource has credentials in the environment you can drop
`--quantum-ibm-resource` and we work it out.

    flux jobs -a
    flux job eventlog <classical-id> | grep memo
    flux job attach <classical-id>

## You need an account that can open sessions

This is the thing that will bite you first. The qiskit-runtime-service type opens
a session, and IBM only allows that on plans that support sessions, like Premium.
On Open or Pay As You Go you can submit tasks but you cannot hold a session, so
there is nothing for the scout to co-allocate.

You get told that in those words rather than a 403. The held classical job is
cancelled, so nothing is left sitting in the queue.

If you have direct access instead, use `--quantum-ibm-type ibm-quantum-system`.

## A session is not the same as having the QPU

An IBM session goes active when its first task reaches the head of the queue.
After that, tasks in the session keep that priority. So opening a session tells
you very little on its own.

After acquiring, the scout submits a warmup task, one qubit measured once, and
waits for it to leave the queue. Once that task is running we are at the head,
the session is live, and only then do we release the classical job.

That is the whole point. The classical job sits held with its nodes reserved
while the scout waits in the QPU queue. It starts the moment the QPU is really
ours, so neither side pays to wait for the other.

If the warmup never runs we release the session and fail the submit. The
classical job never starts against a QPU we do not have.

    --quantum-ibm-warmup-timeout SECONDS   give up after this long. Default 0,
                                           which means wait as long as the
                                           scout job is allowed to live
    --quantum-ibm-skip-warmup              release as soon as the session opens,
                                           no priority guarantee
    --quantum-ibm-ready-timeout SECONDS    default 120, how long to wait for the
                                           backend to report itself up

The warmup costs one shot.

## The scout holds the session for as long as the classical runs

That is deliberate. The fluxion allocation and the vendor session start and end
together. It does mean you are billed for the length of the classical job and not
the length of the quantum work, so set `-t`.

A killed scout still releases, the release is in a finally and SIGTERM is
handled. If the node dies you are down to the vendor timeout.

## Credentials, and where they end up

The plugin runs in your process at submit time and reads the variables out of
your environment. The scout uses them, and it runs as you.

Flux copies the submitting environment into the jobspec, so they are in the job
record and the instance owner can read them. Same as any other job environment.
What does not happen is a credential owned by the system user, or one shared
between users. If that is not good enough for you, do not export the key in the
shell you submit from.

Only the session id travels to the classical job, and it goes over the job
eventlog.

## What the classical job sees

    QUANTUM_SESSION_ID                       the open session
    <resource>_QRMI_JOB_ACQUISITION_TOKEN    same value, where QRMI looks
    QRMI_JOB_QPU_RESOURCES                   the resource id
    QRMI_JOB_QPU_TYPES                       the resource type

All but the first are what the Slurm and LSF QRMI plugins set, so
`get_job_qpu_resources_and_types()` works and a workload written for Slurm runs
here untouched. See workload.py.

## Other QRMI settings

QRMI reads these itself, prefixed with the resource id. Put them in `.env` if you
need them.

    _QRMI_IBM_QRS_SESSION_MODE      batch or dedicated
    _QRMI_IBM_QRS_SESSION_MAX_TTL   session lifetime
    _QRMI_IBM_QRS_TIMEOUT_SECONDS   request timeout
    _QRMI_IBM_QRS_SESSION_ID        join a session that already exists

## When it breaks

    flux job attach <scout-id>

The scout says why it gave up. Credential problems name the variable. Set
`RUST_LOG=debug` before submitting if you want QRMI's own logs, including the
HTTP calls.
