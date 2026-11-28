import sys
import types

import pytest

IBM_CREDS = {
    "ibm_kingston_QRMI_IBM_QRS_ENDPOINT": "supersecret",
    "ibm_kingston_QRMI_IBM_QRS_IAM_ENDPOINT": "supersecret",
    "ibm_kingston_QRMI_IBM_QRS_IAM_APIKEY": "supersecret",
    "ibm_kingston_QRMI_IBM_QRS_SERVICE_CRN": "supersecret",
}


def test_real_backends_registered(backends_real):
    assert backends_real.known_vendors() == {"ibm", "braket", "ionq"}


def test_mock_gated_by_env(backends_real, backends_mock):
    # fixtures import fresh, so check each one independently below
    pass


def test_mock_hidden_without_env(backends_real):
    assert "mock" not in backends_real.known_vendors()


def test_mock_present_with_env(backends_mock):
    assert {"mock", "mock_busy"} <= backends_mock.known_vendors()


def _fake_qrmi(monkeypatch):
    """The backend binds qrmi when its module is imported, so the stub has to
    be in sys.modules before the package is imported fresh."""
    mod = types.ModuleType("qrmi")
    mod.QuantumResource = object
    mod.ResourceType = type("ResourceType", (), {"IBMQiskitRuntimeService": "x"})
    mod.Payload = mod.TaskStatus = object
    monkeypatch.setitem(sys.modules, "qrmi", mod)


def test_missing_creds_fail_at_construction(backends_real):
    """Constructing the backend is the check. The message names the variables
    it wants, never a value."""
    with pytest.raises(backends_real.BackendError) as e:
        backends_real.get_backend("ibm")
    assert "_QRMI_IBM_QRS_IAM_APIKEY" in str(e.value)


def test_creds_present_when_env_set(fresh, monkeypatch):
    _fake_qrmi(monkeypatch)
    b = fresh(mock=False, env=IBM_CREDS)
    assert "ibm_kingston" in b.get_backend("ibm").credential_note


def test_missing_sdk_is_reported_after_credentials(fresh):
    """With credentials in place but no qrmi, say what to install."""
    b = fresh(mock=False, env=IBM_CREDS)
    with pytest.raises(b.BackendError, match="qrmi is not installed"):
        b.get_backend("ibm")


def test_partial_creds_name_only_the_missing_variable(fresh):
    partial = dict(IBM_CREDS)
    del partial["ibm_kingston_QRMI_IBM_QRS_SERVICE_CRN"]
    b = fresh(mock=False, env=partial)
    with pytest.raises(b.BackendError) as e:
        b.get_backend("ibm")
    msg = str(e.value)
    assert "ibm_kingston_QRMI_IBM_QRS_SERVICE_CRN" in msg
    # the api key value must never be echoed back
    assert "supersecret" not in msg


def test_mock_needs_no_creds(backends_mock):
    assert backends_mock.get_backend("mock") is not None


def test_scout_options_map_the_common_device_to_the_resource(fresh, monkeypatch):
    """A vendor backend takes the common options and speaks its own terms."""
    _fake_qrmi(monkeypatch)
    b = fresh(mock=False, env=IBM_CREDS)

    opts = b.get_backend("ibm").scout_options({"device": "ibm_kingston"})
    assert opts["resource"] == "ibm_kingston"
    assert opts["type"] == "qiskit-runtime-service"


def test_scout_options_infer_the_resource_from_the_environment(fresh, monkeypatch):
    """One resource configured is unambiguous, so the id need not be repeated."""
    _fake_qrmi(monkeypatch)
    b = fresh(mock=False, env=IBM_CREDS)

    assert b.get_backend("ibm").scout_options({})["resource"] == "ibm_kingston"


def test_job_environment_follows_the_qrmi_convention(fresh, monkeypatch):
    """The classical job gets the same variables Slurm and LSF set, so user
    code can call get_job_qpu_resources_and_types unchanged."""
    _fake_qrmi(monkeypatch)
    b = fresh(mock=False, env=IBM_CREDS)
    env = b.get_backend("ibm").job_environment(
        {"resource": "ibm_kingston", "type": "qiskit-runtime-service"}
    )
    assert env["QRMI_JOB_QPU_RESOURCES"] == "ibm_kingston"
    assert env["QRMI_JOB_QPU_TYPES"] == "qiskit-runtime-service"


def test_mock_open_session_uses_options(monkeypatch):
    monkeypatch.setenv("FLUX_QUANTUM_MOCK", "1")
    import importlib
    from flux_quantum.backends import mock as mockmod

    importlib.reload(mockmod)
    b = mockmod.MockBackend()
    # forced session id honored
    assert b.open_session({"session": "FORCED123"}) == "FORCED123"
    # otherwise a generated mock id
    assert b.open_session({}).startswith("mock-session-")


def test_session_mode_is_batch_unless_asked(fresh, monkeypatch):
    """A dedicated session bills wall clock, idle gaps included, and it is
    QRMI's own default. The backend sets batch unless the operator or the
    environment says dedicated."""
    import os

    from flux_quantum.backends import qrmi as q

    name = "ibm_kingston_QRMI_IBM_QRS_SESSION_MODE"
    monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FLUX_QUANTUM_IBM_SESSION_MODE", raising=False)
    assert q.session_mode("ibm_kingston", "qiskit-runtime-service") == "batch"
    assert os.environ[name] == "batch"

    monkeypatch.setenv("FLUX_QUANTUM_IBM_SESSION_MODE", "dedicated")
    monkeypatch.delenv(name)
    assert q.session_mode("ibm_kingston", "qiskit-runtime-service") == "dedicated"

    # a value already on the resource wins over the operator setting
    monkeypatch.setenv(name, "batch")
    assert q.session_mode("ibm_kingston", "qiskit-runtime-service") == "batch"

    monkeypatch.setenv(name, "nonsense")
    with pytest.raises(ValueError, match="batch or dedicated"):
        q.session_mode("ibm_kingston", "qiskit-runtime-service")

    # direct access has no session modes
    assert q.session_mode("ibm_kingston", "ibm-quantum-system") is None


def test_open_session_without_a_resource_is_explicit(fresh, monkeypatch):
    _fake_qrmi(monkeypatch)
    b = fresh(mock=False, env=IBM_CREDS)
    with pytest.raises(ValueError, match="no QRMI resource id"):
        b.get_backend("ibm").open_session({})


def test_open_session_names_the_missing_variable(fresh, monkeypatch):
    """Two resources, one complete. Construction passes on the complete one,
    and asking for the other names what it lacks."""
    _fake_qrmi(monkeypatch)
    env = dict(IBM_CREDS)
    env["ibm_torino_QRMI_IBM_QRS_ENDPOINT"] = "supersecret"
    b = fresh(mock=False, env=env)
    with pytest.raises(ValueError) as e:
        b.get_backend("ibm").open_session(
            {"resource": "ibm_torino", "type": "qiskit-runtime-service"}
        )
    assert "ibm_torino_QRMI_IBM_QRS_IAM_APIKEY" in str(e.value)
    assert "supersecret" not in str(e.value)


# --------------------------------------------------- braket hybrid-job quota
# The account quota on ACTIVE hybrid jobs refuses CreateJob outright instead
# of queueing (C4 attempt 3 lost its fourth pair to it). The scout waits a
# quota refusal out, bounded, and raises everything else untouched.


class _QuotaError(Exception):
    """Shaped like the botocore ClientError AWS actually raises."""

    def __init__(self):
        super().__init__(
            "An error occurred (ServiceQuotaExceededException) when calling "
            "the CreateJob operation: You have 3 active hybrid jobs."
        )
        self.response = {"Error": {"Code": "ServiceQuotaExceededException"}}


def _quota_clock(start=100.0):
    state = {"now": start, "naps": []}

    def clock():
        return state["now"]

    def sleep(seconds):
        state["naps"].append(seconds)
        state["now"] += seconds

    return state, clock, sleep


def test_quota_refusals_are_waited_out():
    from flux_quantum.backends import braket

    calls = {"n": 0}

    def create():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _QuotaError()
        return "the-job"

    state, clock, sleep = _quota_clock()
    got = braket._wait_out_quota(
        create, started=100.0, budget=600, clock=clock, sleep=sleep
    )
    assert got == "the-job"
    assert calls["n"] == 3
    assert len(state["naps"]) == 2


def test_quota_wait_gives_up_at_its_budget():
    from flux_quantum.backends import braket

    state, clock, sleep = _quota_clock()

    def create():
        raise _QuotaError()

    with pytest.raises(Exception, match="ServiceQuotaExceededException"):
        braket._wait_out_quota(
            create, started=100.0, budget=60, interval=20.0, clock=clock, sleep=sleep
        )
    # it slept toward the budget but never past it
    assert state["now"] - 100.0 <= 60


def test_non_quota_errors_raise_immediately():
    from flux_quantum.backends import braket

    calls = {"n": 0}

    def create():
        calls["n"] += 1
        raise RuntimeError("ValidationException: bad instance type")

    state, clock, sleep = _quota_clock()
    with pytest.raises(RuntimeError, match="ValidationException"):
        braket._wait_out_quota(
            create, started=100.0, budget=600, clock=clock, sleep=sleep
        )
    assert calls["n"] == 1
    assert state["naps"] == []


def test_wrapped_quota_error_is_detected_by_its_message():
    """The SDK sometimes rewraps the ClientError; the string still names it."""
    from flux_quantum.backends import braket

    wrapped = RuntimeError(
        "create failed: An error occurred (ServiceQuotaExceededException) "
        "when calling the CreateJob operation"
    )
    assert braket._quota_exceeded(wrapped)
    assert not braket._quota_exceeded(RuntimeError("AccessDeniedException"))
