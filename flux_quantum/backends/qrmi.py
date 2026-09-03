"""QRMI backed vendor sessions.

QRMI is itself vendor agnostic, so one implementation covers every resource
type it supports. acquire maps onto open_session and release onto
close_session, which is the scout lifecycle exactly.

QRMI reads its own configuration from the environment, and every variable is
prefixed with the resource id, so the api key for ibm_kingston is

    ibm_kingston_QRMI_IBM_QRS_IAM_APIKEY

Credentials stay in the user environment. We only ever check that a variable is
set and report its name, never its value.

qrmi is an optional dependency and needs python 3.11 or newer, so it is
imported inside the methods that use it.
"""

import json
import os
import time

from .base import Backend, Signals

# type strings QRMI itself uses in qrmi_config.json and in QRMI_JOB_QPU_TYPES,
# mapped to the ResourceType attribute
RESOURCE_TYPES = {
    "qiskit-runtime-service": "IBMQiskitRuntimeService",
    "ibm-quantum-system": "IBMQuantumSystem",
    "pasqal-cloud": "PasqalCloud",
    "pasqal-local": "PasqalLocal",
    "iqm-server": "IQMServer",
    "alice-bob-felis": "AliceBobFelis",
}

# suffixes QRMI requires per type, discovered by constructing a QuantumResource
# with an empty environment and reading back what it asked for. Each one is
# looked up as <resource_id><suffix>.
REQUIRED_ENV = {
    "qiskit-runtime-service": (
        "_QRMI_IBM_QRS_ENDPOINT",
        "_QRMI_IBM_QRS_IAM_ENDPOINT",
        "_QRMI_IBM_QRS_IAM_APIKEY",
        "_QRMI_IBM_QRS_SERVICE_CRN",
    ),
    "ibm-quantum-system": (
        "_QRMI_IBM_QS_ENDPOINT",
        "_QRMI_IBM_QS_IAM_ENDPOINT",
        "_QRMI_IBM_QS_IAM_APIKEY",
        "_QRMI_IBM_QS_SERVICE_CRN",
    ),
    "iqm-server": (
        "_QRMI_IQM_ISA_ENDPOINT",
        "_QRMI_IQM_ISA_TOKEN",
    ),
    "pasqal-cloud": (),
    "pasqal-local": (),
    "alice-bob-felis": (),
}


def resource_type(name):
    """Return the QRMI ResourceType for a type string like ibm-quantum-system."""
    from qrmi import ResourceType

    attr = RESOURCE_TYPES.get(name, name)
    if not hasattr(ResourceType, attr):
        known = ", ".join(sorted(RESOURCE_TYPES))
        raise ValueError(
            "unknown QRMI resource type {}, expected one of {}".format(name, known)
        )
    return getattr(ResourceType, attr)


def resources_in_environment(rtype):
    """Return the resource ids that have credentials set for this type.

    QRMI prefixes every variable with the resource id, so the ids can be read
    back out of the environment rather than being configured twice.
    """
    suffixes = REQUIRED_ENV.get(rtype, ())
    if not suffixes:
        return []
    suffix = suffixes[0]
    return sorted(k[: -len(suffix)] for k in os.environ if k.endswith(suffix))


def missing_env(resource, rtype):
    """Return the variables this resource still needs."""
    return [s for s in REQUIRED_ENV.get(rtype, ()) if not os.environ.get(resource + s)]


# QRMI passes the acquisition token to the job in this variable, prefixed with
# the resource id. The Slurm and LSF plugins set it, so we set it too and a
# workload written for either runs here unchanged.
ACQUISITION_TOKEN = "_QRMI_JOB_ACQUISITION_TOKEN"


def warmup_payload(shots=1):
    """Smallest valid sampler input, one qubit measured once.

    Shape copied from the QRMI sampler in qrmi/primitives/base_sampler.py, and
    qiskit generates the qasm3 so we are not hand writing a circuit. A measure
    only circuit needs no basis gate translation, so it is ISA valid anywhere.
    """
    try:
        from qiskit import QuantumCircuit, qasm3
    except ImportError:
        raise RuntimeError(
            "qiskit is needed to build the warmup task. Install it with "
            "pip install 'qrmi[ibm]', or pass --quantum-ibm-skip-warmup to "
            "release without confirming priority"
        )

    qc = QuantumCircuit(1, 1)
    qc.measure(0, 0)
    qasm = qasm3.dumps(qc, disable_constants=True, allow_aliasing=True)
    return {
        "pubs": [[qasm, None, shots]],
        "shots": shots,
        "options": {},
        "version": 2,
        "support_qiskit": True,
    }


def explain_acquire_failure(error, resource, rtype):
    """Turn a QRMI acquire error into something a user can act on.

    QRMI raises with the HTTP status in the message, so the status is what we
    match on.
    """
    text = str(error)
    if "403" in text or "Forbidden" in text:
        return (
            "{r}: not allowed to open a session on {t}. IBM only permits "
            "sessions on plans that support them, such as Premium. On an Open "
            "or Pay As You Go plan you can submit tasks but not hold a "
            "session, so there is nothing for the scout to co-allocate.\n"
            "  use --quantum-ibm-type ibm-quantum-system if you have direct "
            "access, or --quantum-vendor mock to test the pipeline.\n"
            "  QRMI said: {e}".format(r=resource, t=rtype, e=text)
        )
    if "401" in text or "Unauthorized" in text or "Token renewal failed" in text:
        return (
            "{r}: credentials rejected. Check {r}_QRMI_IBM_QRS_IAM_APIKEY and "
            "{r}_QRMI_IBM_QRS_SERVICE_CRN.\n  QRMI said: {e}".format(r=resource, e=text)
        )
    if "Invalid session mode" in text:
        return "{r}: bad {r}_QRMI_IBM_QRS_SESSION_MODE. QRMI said: {e}".format(
            r=resource, e=text
        )
    if "404" in text:
        return "{r}: no such resource on {t}. QRMI said: {e}".format(
            r=resource, t=rtype, e=text
        )
    return "{r}: could not acquire on {t}: {e}".format(r=resource, t=rtype, e=text)


class QRMIBackend(Backend):
    """Shared implementation for any vendor reachable through QRMI.

    A vendor subclass sets name and default_type. Everything else, including
    the option names, comes from name so the options stay namespaced.
    """

    # QRMI type string used when the user does not pass one
    default_type = None

    def __init__(self):
        self._resource = None
        self._lock = None

    @classmethod
    def add_options(cls, add_option):
        add_option(
            "--{}-resource".format(cls.name),
            metavar="ID",
            default=None,
            help="{}: QRMI resource id, for example ibm_kingston".format(cls.name),
        )
        add_option(
            "--{}-ready-timeout".format(cls.name),
            metavar="SECONDS",
            default=None,
            help="{}: wait this long after acquiring for the resource to report "
            "itself accessible, 0 to skip. Default 120".format(cls.name),
        )
        add_option(
            "--{}-skip-warmup".format(cls.name),
            action="store_true",
            help="{}: release the classical job as soon as the session opens, "
            "without waiting for a warmup task to reach the head of the "
            "queue. Faster, but priority is not confirmed".format(cls.name),
        )
        add_option(
            "--{}-warmup-timeout".format(cls.name),
            metavar="SECONDS",
            default=None,
            help="{}: give up if the warmup task is still queued after this "
            "long. Default 0, meaning wait for as long as the scout job "
            "is allowed to run".format(cls.name),
        )
        add_option(
            "--{}-type".format(cls.name),
            metavar="TYPE",
            default=None,
            help="{}: QRMI resource type, default {}".format(
                cls.name, cls.default_type
            ),
        )

    def scout_options(self, args):
        rtype = getattr(args, "{}_type".format(self.name), None) or self.default_type
        resource = getattr(args, "{}_resource".format(self.name), None)
        if not resource:
            # only one resource configured in the environment is unambiguous
            found = resources_in_environment(rtype)
            if len(found) == 1:
                resource = found[0]
        ready = getattr(args, "{}_ready_timeout".format(self.name), None)
        warmup = getattr(args, "{}_warmup_timeout".format(self.name), None)
        return {
            "resource": resource,
            "type": rtype,
            "ready_timeout": 120 if ready is None else float(ready),
            "skip_warmup": bool(
                getattr(args, "{}_skip_warmup".format(self.name), False)
            ),
            "warmup_timeout": 0 if warmup is None else float(warmup),
        }

    def job_environment(self, options):
        """Tell the classical job which QPU it has, the same way the Slurm and
        LSF plugins do, so user code can call get_job_qpu_resources_and_types
        without knowing which workload manager it is running under."""
        if not options.get("resource"):
            return {}
        return {
            "QRMI_JOB_QPU_RESOURCES": options["resource"],
            "QRMI_JOB_QPU_TYPES": options.get("type") or self.default_type,
        }

    @staticmethod
    def acquisition_token_env(resource, session):
        """The variable QRMI expects the acquisition token in."""
        return {resource + ACQUISITION_TOKEN: session}

    def open_session(self, options):
        from qrmi import QuantumResource

        resource = options.get("resource")
        rtype = options.get("type") or self.default_type
        if not resource:
            raise ValueError(
                "{}: no QRMI resource id. Pass --quantum-{}-resource or export "
                "the credentials for exactly one resource".format(self.name, self.name)
            )
        missing = missing_env(resource, rtype)
        if missing:
            raise ValueError(
                "{}: {} is missing {}".format(
                    self.name, resource, ", ".join(resource + s for s in missing)
                )
            )
        self._resource = QuantumResource(resource, resource_type(rtype))
        try:
            self._lock = self._resource.acquire()
        except Exception as e:
            self._resource = None
            raise RuntimeError(explain_acquire_failure(e, resource, rtype))

        # Hold nothing we cannot use. If the resource never becomes usable,
        # give the session back rather than letting the classical job start.
        ready, why = self.wait_until_ready(float(options.get("ready_timeout") or 0))
        if not ready:
            self.close_session()
            raise RuntimeError(
                "{}: acquired a session but {} did not become usable: {}".format(
                    self.name, resource, why
                )
            )

        return str(self._lock)

    def wait_until_ready(self, timeout=0.0, interval=5.0, sleep=time.sleep):
        """Poll until the resource reports itself accessible.

        Returns (ready, reason). timeout of 0 skips the wait.

        This is as far as QRMI lets us check. is_accessible reads the backend
        status, so it tells us the backend is online and taking work. QRMI does
        not expose session state or queue position to python, so it is not proof
        that our session has been activated.
        """
        if timeout <= 0 or self._resource is None:
            return True, "not checked"
        deadline = time.time() + timeout
        last = "no response"
        while True:
            try:
                if self._resource.is_accessible():
                    return True, "accessible"
                last = "not accepting work"
            except Exception as e:
                last = str(e)
            if time.time() + interval >= deadline:
                return False, last
            sleep(interval)

    def wait_for_priority(self, options=None, interval=5.0, sleep=time.sleep, shots=1):
        """Submit a one shot task and wait for it to leave the queue.

        An IBM session activates when its first task reaches the head of the
        queue, and later tasks in the session inherit that priority, so a task
        that has started running is the signal that we actually hold the QPU.
        This is the only check available that means priority rather than just
        reachability.

        Returns (ok, reason). A timeout of 0 waits as long as the scout job is
        allowed to live.
        """
        from qrmi import Payload, TaskStatus

        options = options or {}
        if options.get("skip_warmup"):
            return True, "warmup skipped, priority unconfirmed"
        timeout = float(options.get("warmup_timeout") or 0)
        payload = Payload.QiskitPrimitive(
            input=json.dumps(warmup_payload(shots)), program_id="sampler"
        )
        task = self._resource.task_start(payload)
        deadline = time.time() + timeout if timeout > 0 else None
        while True:
            status = self._resource.task_status(task)
            if status in (TaskStatus.Running, TaskStatus.Completed):
                return True, "warmup task {} is {}".format(task, status)
            if status in (TaskStatus.Failed, TaskStatus.Cancelled):
                return False, "warmup task {} is {}".format(task, status)
            if deadline is not None and time.time() + interval >= deadline:
                try:
                    self._resource.task_stop(task)
                except Exception:
                    pass
                return False, "still queued after {}s".format(timeout)
            sleep(interval)

    def close_session(self, session=None):
        if self._resource is not None and self._lock is not None:
            self._resource.release(self._lock)
        self._resource = None
        self._lock = None

    def credentials_present(self):
        found = resources_in_environment(self.default_type)
        if not found:
            wanted = ", ".join(
                "<resource>" + s for s in REQUIRED_ENV.get(self.default_type, ())
            )
            return False, "{}: no QRMI credentials in the environment, set {}".format(
                self.name, wanted
            )
        incomplete = {r: missing_env(r, self.default_type) for r in found}
        usable = [r for r, m in incomplete.items() if not m]
        if not usable:
            r = found[0]
            return False, "{}: {} is missing {}".format(
                self.name, r, ", ".join(r + s for s in incomplete[r])
            )
        return True, "{}: credentials present for {}".format(
            self.name, " ".join(usable)
        )

    def probe(self):
        """Ask QRMI whether the resource is reachable.

        Only possible when exactly one resource is configured, since the
        selector runs before the submit options are parsed. Otherwise report
        available and let the scout find out for real.
        """
        found = [
            r
            for r in resources_in_environment(self.default_type)
            if not missing_env(r, self.default_type)
        ]
        if len(found) != 1:
            return Signals(available=bool(found), detail={"resources": found})
        try:
            from qrmi import QuantumResource

            qr = QuantumResource(found[0], resource_type(self.default_type))
            return Signals(
                available=bool(qr.is_accessible()), detail={"resource": found[0]}
            )
        except Exception as e:
            return Signals(available=False, detail={"error": str(e)})
