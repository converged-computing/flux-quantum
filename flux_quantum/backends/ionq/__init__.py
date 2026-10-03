"""IonQ backend, over the v0.4 REST API with nothing but the standard library.

Credentials come from IONQ_API_KEY, or IONQ_API_TOKEN. IONQ_API_URL overrides
the endpoint, which is how the tests point it at the fake service in fake.py.

Two ways to hold the device, set with --quantum-hold.

session is the default. It creates a session on the backend and submits a one
qubit warm-up job inside it. The scout is ready once IonQ reports the session
active, which is when jobs carrying the session id are being served. The
session id is what the classical job is handed. Sessions are in beta and only
some accounts have them, and a refusal says so plainly.

probe submits the warm-up job alone and is ready once it has started. That
holds nothing, like the Braket probe, but it does say the device is serving us.

Dry run. --quantum-dry-run, or FLUX_QUANTUM_MOCK in the environment, sends
every job to the simulator, which IonQ does not charge for, with the noise
model of the backend asked for, so the circuit compiles the way it would on
hardware. The simulator has no queue, so a dry run exercises the plumbing and
not the hold.

Tuning, from the environment:

    FLUX_QUANTUM_IONQ_SHOTS           shots for the warm-up job, 100
    FLUX_QUANTUM_IONQ_COST_LIMIT_USD  a cost limit on every session opened,
                                      none by default. IonQ ends the session
                                      when its jobs reach it, so a session
                                      nobody is watching cannot bill past it
"""

import math
import os
import time

from ..base import Backend, BackendError, Signals, register, truthy, tuning
from .client import API_URL, APIError, Client

SIMULATOR = "simulator"
DEFAULT_BACKEND = "qpu.forte-1"

# v0.4 says started where v0.3 said running, and a job that has already
# finished was served too
STARTED = ("started", "running", "completed")
TERMINAL = ("completed", "canceled", "failed")


def noise_model(backend):
    """The simulator noise model for a QPU name, or None for the simulator
    itself. qpu.forte-1 is modelled as forte-1."""
    if backend.startswith("qpu."):
        return backend[len("qpu.") :]
    return None


# one qubit, one gate. The smallest job the service accepts.
WARMUP = {"qubits": 1, "gateset": "qis", "circuit": [{"gate": "h", "target": 0}]}


@register
class IonQBackend(Backend):
    name = "ionq"

    def __init__(self, client=None):
        if client is None:
            key = os.environ.get("IONQ_API_KEY") or os.environ.get("IONQ_API_TOKEN")
            if not key:
                raise BackendError(
                    "ionq: no IONQ_API_KEY in the environment. Create one at "
                    "cloud.ionq.com and export it"
                )
            client = Client(key, os.environ.get("IONQ_API_URL") or API_URL)
            self.credential_note = "ionq: credentials present via IONQ_API_KEY"
        else:
            self.credential_note = "ionq: client supplied"
        self.client = client
        self._session = None
        self._job = None
        self._hold_started = None
        self._hold_ready_after = None

    simulator = SIMULATOR
    holds = ("session", "probe")

    def dry_run(self, common):
        """The simulator, with the noise model of the backend that was asked
        for, so the circuit still compiles the way the hardware would."""
        out = super().dry_run(common)
        out["noise"] = noise_model(common.get("device") or DEFAULT_BACKEND)
        return out

    def scout_options(self, common):
        hold = self.check_hold(common.get("hold"))
        return {
            # where jobs are sent, and the noise that stands in for the
            # hardware when it is the simulator
            "target": common.get("device") or DEFAULT_BACKEND,
            "noise": common.get("noise"),
            "dry_run": bool(common.get("dry_run")),
            "hold": hold,
            # the session's own limit is in minutes
            "max_minutes": max(
                1, int(math.ceil(float(common.get("hold_max") or 900) / 60))
            ),
            "shots": int(tuning("ionq_shots", 100)),
            "timeout": float(common.get("wait") or 0),
        }

    def job_environment(self, options):
        """What the classical job needs to submit into the hold: the target
        and, on the simulator, the noise model. The key stays the user's."""
        env = {
            "IONQ_BACKEND": options.get("target") or DEFAULT_BACKEND,
            "IONQ_API_URL": self.client.url,
        }
        if options.get("noise"):
            env["IONQ_NOISE_MODEL"] = options["noise"]
        return env

    def _warmup(self, options, session=None):
        """Submit the one qubit job and return its id."""
        body = {
            "type": "ionq.circuit.v1",
            "name": "flux-quantum-warmup",
            "metadata": {"flux_quantum": "warmup"},
            "shots": int(options.get("shots") or 100),
            "backend": options.get("target") or DEFAULT_BACKEND,
            "input": WARMUP,
        }
        if session:
            body["session_id"] = session
        if options.get("noise"):
            body["noise"] = {"model": options["noise"]}
        return self.client.post("/jobs", body)["id"]

    def open_session(self, options):
        """Take the device. session returns the session id, probe the job id.

        A session the account cannot create is refused with the fix named.
        In a dry run the simulator may refuse one too, and since the run is
        there to exercise the plumbing, it falls back to a probe and says so.
        """
        self._hold_started = time.time()
        if (options.get("hold") or "session") == "session":
            body = {
                "backend": options.get("target") or DEFAULT_BACKEND,
                "settings": {
                    "duration_limit_min": int(options.get("max_minutes") or 15)
                },
            }
            limit = tuning("ionq_cost_limit_usd")
            if limit:
                body["settings"]["cost_limit"] = {"unit": "usd", "value": float(limit)}
            try:
                self._session = self.client.post("/sessions", body)["id"]
            except APIError as e:
                if e.status not in (400, 401, 403, 404):
                    raise
                if not options.get("dry_run"):
                    raise BackendError(
                        "ionq: this account cannot create a session on {} "
                        "(HTTP {}). Sessions are in beta. Ask IonQ, or hold "
                        "with --quantum-hold probe".format(body["backend"], e.status)
                    )
                print(
                    "ionq: {} refused a session (HTTP {}), holding with a "
                    "probe job instead".format(body["backend"], e.status),
                    flush=True,
                )
            else:
                self._job = self._warmup(options, session=self._session)
                return self._session
        self._job = self._warmup(options)
        return self._job

    def session_id(self, opened):
        """A session id goes over as is, since jobs are submitted into it. A
        probe job's id is prefixed, so the classical job can tell it holds
        nothing it can submit into."""
        if self._session and opened == self._session:
            return opened
        return "job:{}".format(opened)

    def wait_for_priority(self, options=None, interval=5.0, sleep=time.sleep):
        """Poll until the hold is real.

        With a session, that is IonQ saying it is active, or the warm-up job
        inside it having started, whichever is reported first. With a probe,
        the warm-up job having started. A warm-up that fails or is cancelled
        is a failure, not readiness.
        """
        options = options or {}
        timeout = float(options.get("timeout") or 0)
        started = self._hold_started or time.time()
        deadline = started + timeout if timeout > 0 else None
        while True:
            job = self.client.get("/jobs/{}".format(self._job))
            status = job.get("status")
            waited = time.time() - started
            if status in ("failed", "canceled"):
                why = (job.get("failure") or {}).get("error") or status
                return False, "warm-up job {} after {:.0f}s: {}".format(
                    status, waited, why
                )
            if self._session:
                s = self.client.get("/sessions/{}".format(self._session))
                if s.get("ended_at") or s.get("status") in ("ended", "expired"):
                    return False, "session {} before it became active".format(
                        s.get("status") or "ended"
                    )
                if s.get("active") or s.get("started_at") or status in STARTED:
                    self._hold_ready_after = waited
                    return True, "session active after {:.0f}s".format(waited)
                said = "session {}, warm-up job {}".format(
                    s.get("status") or "pending", status
                )
            else:
                if status in STARTED:
                    self._hold_ready_after = waited
                    return True, "warm-up job {} after {:.0f}s".format(status, waited)
                said = "warm-up job {}".format(status)
            print("ionq: {} ({:.0f}s)".format(said, waited), flush=True)
            if deadline is not None and time.time() + interval >= deadline:
                return False, "gave up after {:.0f}s, still {}".format(waited, said)
            sleep(interval)

    def close_session(self, session=None):
        """End the session, which cancels anything still queued in it, or
        cancel a probe job that has not run. Safe to call twice."""
        sess, job, self._session, self._job = self._session, self._job, None, None
        if sess:
            try:
                self.client.post("/sessions/{}/end".format(sess))
            except Exception as e:
                print("ionq: could not end session {}: {}".format(sess, e))
            return
        if job:
            try:
                if (
                    self.client.get("/jobs/{}".format(job)).get("status")
                    not in TERMINAL
                ):
                    self.client.put("/jobs/{}/status/cancel".format(job))
            except Exception as e:
                print("ionq: could not cancel job {}: {}".format(job, e))

    def probe(self):
        """The backend's status and queue, from GET /backends.

        IonQ reports an average queue time rather than a count. It goes in
        queue_depth so --quantum-select queue can rank it, lower being
        better either way, and the unit is noted in detail.
        """
        backend = DEFAULT_BACKEND
        if truthy(os.environ.get("FLUX_QUANTUM_MOCK")):
            backend = SIMULATOR
        try:
            b = self.client.get("/backends/{}".format(backend))
        except Exception as e:
            return Signals(
                available=False, detail={"backend": backend, "error": str(e)}
            )
        return Signals(
            available=b.get("status") == "available",
            queue_depth=b.get("average_queue_time"),
            detail={
                "backend": backend,
                "status": b.get("status"),
                "degraded": b.get("degraded"),
                "qubits": b.get("qubits"),
                "queue_unit": "average_queue_time",
            },
        )
