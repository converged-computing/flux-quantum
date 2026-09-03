"""QRMI backed sessions, driven against a fake qrmi module.

qrmi needs python 3.11 or newer and real credentials, so the tests stub it and
check the calls we make rather than talking to a vendor.
"""

import sys
import types

import pytest

RESOURCE = "ibm_kingston"
SUFFIXES = (
    "_QRMI_IBM_QRS_ENDPOINT",
    "_QRMI_IBM_QRS_IAM_ENDPOINT",
    "_QRMI_IBM_QRS_IAM_APIKEY",
    "_QRMI_IBM_QRS_SERVICE_CRN",
)
CREDS = {RESOURCE + s: "supersecret" for s in SUFFIXES}


class FakeStatus:
    Queued = "Queued"
    Running = "Running"
    Completed = "Completed"
    Failed = "Failed"
    Cancelled = "Cancelled"


class FakeResource:
    calls = []
    # statuses handed back by task_status, in order
    statuses = ["Running"]

    def __init__(self, rid, rtype):
        self.rid = rid
        FakeResource.calls.append(("ctor", rid, str(rtype)))

    def acquire(self):
        FakeResource.calls.append(("acquire", self.rid))
        return "LOCK-123"

    def release(self, id):
        FakeResource.calls.append(("release", self.rid, id))

    def is_accessible(self):
        return True

    def task_start(self, payload):
        FakeResource.calls.append(("task_start", self.rid))
        return "TASK-1"

    def task_status(self, task):
        return FakeResource.statuses.pop(0)

    def task_stop(self, task):
        FakeResource.calls.append(("task_stop", task))


@pytest.fixture
def fake_qrmi(monkeypatch):
    FakeResource.calls = []
    FakeResource.statuses = ["Running"]
    mod = types.ModuleType("qrmi")
    mod.QuantumResource = FakeResource
    mod.ResourceType = type(
        "ResourceType", (), {"IBMQiskitRuntimeService": "IBMQiskitRuntimeService"}
    )
    mod.TaskStatus = FakeStatus
    mod.Payload = type(
        "Payload", (), {"QiskitPrimitive": staticmethod(lambda input, program_id: {})}
    )
    monkeypatch.setitem(sys.modules, "qrmi", mod)
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    return FakeResource


def _opts(**kw):
    base = {"resource": RESOURCE, "type": "qiskit-runtime-service"}
    base.update(kw)
    return base


def _ibm():
    from flux_quantum.backends import get_backend
    import flux_quantum.backends.ibm  # noqa

    return get_backend("ibm")


def test_acquire_and_release_round_trip(fake_qrmi):
    """open_session acquires and close_session releases the same lock."""
    b = _ibm()
    session = b.open_session(_opts(skip_warmup=True))
    assert session == "LOCK-123"
    b.close_session(session)
    assert ("acquire", RESOURCE) in fake_qrmi.calls
    assert ("release", RESOURCE, "LOCK-123") in fake_qrmi.calls


def test_close_without_open_is_harmless(fake_qrmi):
    """The scout always calls close in a finally, even if open never ran."""
    _ibm().close_session("anything")
    assert fake_qrmi.calls == []


def test_resource_is_inferred_when_only_one_is_configured(fake_qrmi):
    class Args:
        ibm_resource = None
        ibm_type = None

    assert _ibm().scout_options(Args())["resource"] == RESOURCE


def test_explicit_resource_wins(fake_qrmi):
    class Args:
        ibm_resource = "ibm_fez"
        ibm_type = None

    assert _ibm().scout_options(Args())["resource"] == "ibm_fez"


def test_job_environment_matches_the_qrmi_convention(fake_qrmi):
    """Slurm and LSF set these two, so a workload moves between them unchanged."""
    env = _ibm().job_environment(
        {"resource": RESOURCE, "type": "qiskit-runtime-service"}
    )
    assert env == {
        "QRMI_JOB_QPU_RESOURCES": RESOURCE,
        "QRMI_JOB_QPU_TYPES": "qiskit-runtime-service",
    }


def test_unknown_resource_type_is_rejected(fake_qrmi):
    from flux_quantum.backends.qrmi import resource_type

    with pytest.raises(ValueError, match="unknown QRMI resource type"):
        resource_type("not-a-real-type")


def test_secrets_never_appear_in_messages(fake_qrmi, monkeypatch):
    """Credential checks report variable names and never values."""
    monkeypatch.delenv(RESOURCE + "_QRMI_IBM_QRS_IAM_APIKEY")
    ok, msg = _ibm().credentials_present()
    assert not ok
    assert RESOURCE + "_QRMI_IBM_QRS_IAM_APIKEY" in msg
    assert "supersecret" not in msg


def test_premium_message_when_sessions_are_not_allowed():
    """A 403 on acquire means the plan cannot hold a session. Say so plainly
    instead of surfacing an HTTP status."""
    from flux_quantum.backends.qrmi import explain_acquire_failure

    msg = explain_acquire_failure(
        RuntimeError("status code error in create_session: 403"),
        "ibm_kingston",
        "qiskit-runtime-service",
    )
    assert "Premium" in msg
    assert "submit tasks but not hold a session" in msg
    assert "--quantum-vendor mock" in msg
    assert "403" in msg  # the original is still there for debugging


def test_bad_credentials_point_at_the_variables():
    from flux_quantum.backends.qrmi import explain_acquire_failure

    msg = explain_acquire_failure(
        RuntimeError("Token renewal failed: 401"), "ibm_fez", "qiskit-runtime-service"
    )
    assert "ibm_fez_QRMI_IBM_QRS_IAM_APIKEY" in msg


def test_acquire_failure_is_translated(fake_qrmi, monkeypatch):
    def boom(self):
        raise RuntimeError("status code error in create_session: 403 Forbidden")

    monkeypatch.setattr(fake_qrmi, "acquire", boom)
    b = _ibm()
    with pytest.raises(RuntimeError, match="Premium"):
        b.open_session(_opts(skip_warmup=True))


def test_session_is_released_if_the_resource_never_becomes_usable(
    fake_qrmi, monkeypatch
):
    """Never hand the classical job a session we could not confirm."""
    monkeypatch.setattr(fake_qrmi, "is_accessible", lambda self: False)
    b = _ibm()
    with pytest.raises(RuntimeError, match="did not become usable"):
        b.open_session(_opts(ready_timeout=1, skip_warmup=True))
    # the acquired session must not be left dangling
    assert ("release", RESOURCE, "LOCK-123") in fake_qrmi.calls


def test_ready_wait_polls_until_accessible(fake_qrmi):
    b = _ibm()
    b.open_session(_opts(skip_warmup=True))
    states = [False, False, True]
    fake_qrmi.is_accessible = lambda self: states.pop(0)
    slept = []
    ready, why = b.wait_until_ready(timeout=60, interval=1, sleep=slept.append)
    assert ready and why == "accessible"
    assert len(slept) == 2  # two failures, so two waits


def test_wrap_exports_the_qrmi_acquisition_token():
    """The token goes into the variable QRMI reads, not only ours."""
    from flux_quantum.wrap import session_environment

    env = session_environment("SESS-1", resources="ibm_kingston,ibm_fez")
    assert env["QUANTUM_SESSION_ID"] == "SESS-1"
    assert env["ibm_kingston_QRMI_JOB_ACQUISITION_TOKEN"] == "SESS-1"
    assert env["ibm_fez_QRMI_JOB_ACQUISITION_TOKEN"] == "SESS-1"


def test_wrap_without_qrmi_resources_sets_only_our_variable():
    from flux_quantum.wrap import session_environment

    assert session_environment("SESS-2", resources="") == {
        "QUANTUM_SESSION_ID": "SESS-2"
    }


# ---------------------------------------------------------------------------
# priority. An IBM session activates when its first task reaches the head of
# the queue, so a task that is running is the signal that we hold the QPU.
# ---------------------------------------------------------------------------


def _acquired(fake, statuses):
    """A backend with a session already open, so wait_for_priority can run."""
    b = _ibm()
    b.open_session(_opts(ready_timeout=0, skip_warmup=True))
    fake.statuses = list(statuses)
    fake.calls = []
    return b


def test_warmup_waits_until_the_task_leaves_the_queue(fake_qrmi, monkeypatch):
    monkeypatch.setattr(
        "flux_quantum.backends.qrmi.warmup_payload", lambda shots=1: {"pubs": []}
    )
    b = _acquired(fake_qrmi, ["Queued", "Queued", "Running"])
    slept = []
    ok, reason = b.wait_for_priority({}, interval=1, sleep=slept.append)
    assert ok and "Running" in reason
    assert len(slept) == 2


def test_warmup_failure_means_no_priority(fake_qrmi, monkeypatch):
    monkeypatch.setattr(
        "flux_quantum.backends.qrmi.warmup_payload", lambda shots=1: {"pubs": []}
    )
    b = _acquired(fake_qrmi, ["Queued", "Failed"])
    ok, reason = b.wait_for_priority({}, interval=0, sleep=lambda s: None)
    assert not ok and "Failed" in reason


def test_warmup_timeout_stops_the_task(fake_qrmi, monkeypatch):
    monkeypatch.setattr(
        "flux_quantum.backends.qrmi.warmup_payload", lambda shots=1: {"pubs": []}
    )
    b = _acquired(fake_qrmi, ["Queued"] * 10)
    ok, reason = b.wait_for_priority(
        {"warmup_timeout": 1}, interval=1, sleep=lambda s: None
    )
    assert not ok and "still queued" in reason
    assert ("task_stop", "TASK-1") in fake_qrmi.calls


def test_priority_not_reached_is_reported(fake_qrmi, monkeypatch):
    """The scout cancels the held job on a False here, so never say ok."""
    monkeypatch.setattr(
        "flux_quantum.backends.qrmi.warmup_payload", lambda shots=1: {"pubs": []}
    )
    b = _acquired(fake_qrmi, ["Failed"])
    ok, reason = b.wait_for_priority({}, interval=0, sleep=lambda s: None)
    assert not ok and "Failed" in reason


def test_skip_warmup_submits_nothing(fake_qrmi):
    b = _acquired(fake_qrmi, [])
    ok, reason = b.wait_for_priority({"skip_warmup": True})
    assert ok and "skipped" in reason
    assert not any(c[0] == "task_start" for c in fake_qrmi.calls)


def test_warmup_payload_is_a_single_shot_measure(fake_qrmi):
    """Cheapest thing that still occupies the QPU, so priority costs one shot."""
    pytest.importorskip("qiskit")
    from flux_quantum.backends.qrmi import warmup_payload

    p = warmup_payload()
    assert p["shots"] == 1 and p["version"] == 2 and p["support_qiskit"] is True
    qasm = p["pubs"][0][0]
    assert "measure" in qasm


def test_missing_qiskit_says_what_to_install(fake_qrmi, monkeypatch):
    """The warmup needs qiskit. Say so, and offer the way out."""
    import builtins

    real = builtins.__import__

    def no_qiskit(name, *a, **k):
        if name == "qiskit":
            raise ImportError("no qiskit")
        return real(name, *a, **k)

    b = _acquired(fake_qrmi, ["Queued"])
    monkeypatch.setattr(builtins, "__import__", no_qiskit)
    with pytest.raises(RuntimeError) as e:
        b.wait_for_priority({})
    assert "qrmi[ibm]" in str(e.value)
    assert "skip-warmup" in str(e.value)
