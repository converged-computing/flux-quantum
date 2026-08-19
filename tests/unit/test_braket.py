"""Braket backend, driven against a fake SDK.

Braket has no session to hold, so the scout submits a no-op task and waits for
it to reach the front of the queue. The interesting behaviour is what counts as
ready and what counts as a failure.
"""

import sys
import types

import pytest

ARN = "arn:aws:braket:us-west-1:123:quantum-task/abc"


class FakeTask:
    def __init__(self, states, positions):
        self.id = ARN
        self._states = list(states)
        self._positions = list(positions)
        self.state_calls = 0

    def state(self):
        self.state_calls += 1
        return self._states.pop(0) if self._states else "QUEUED"

    def queue_position(self):
        pos = self._positions.pop(0) if self._positions else None
        return types.SimpleNamespace(queue_position=pos)


@pytest.fixture
def fake_braket(monkeypatch):
    """Minimal braket.aws and braket.circuits."""
    submitted = {}

    class FakeDevice:
        status = "ONLINE"

        def __init__(self, arn):
            submitted["device"] = arn

        def run(self, circuit, shots=1):
            submitted["shots"] = shots
            return submitted["task"]

    aws = types.ModuleType("braket.aws")
    aws.AwsDevice = FakeDevice
    circuits = types.ModuleType("braket.circuits")

    class FakeCircuit:
        def i(self, q):
            submitted["qubit"] = q
            return self

    circuits.Circuit = FakeCircuit
    pkg = types.ModuleType("braket")
    monkeypatch.setitem(sys.modules, "braket", pkg)
    monkeypatch.setitem(sys.modules, "braket.aws", aws)
    monkeypatch.setitem(sys.modules, "braket.circuits", circuits)
    return submitted


def _backend():
    from flux_quantum.backends import get_backend
    import flux_quantum.backends.braket  # noqa

    return get_backend("braket")


def test_defaults_to_the_sv1_simulator():
    """SV1 is cents per task, so it is the sane default for a probe."""
    from flux_quantum.backends.braket import SV1

    class Args:
        braket_device = None
        braket_region = None
        braket_shots = None
        braket_queue_timeout = None

    opts = _backend().scout_options(Args())
    assert opts["device"] == SV1
    assert opts["shots"] == 1


def test_region_comes_from_the_device_arn(monkeypatch):
    """A QPU searched in the wrong region simply never appears."""
    from flux_quantum.backends.braket import region_for

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    assert region_for("arn:aws:braket:eu-north-1::device/qpu/iqm/x") == "eu-north-1"
    # SV1 has an empty region field, so fall back
    assert (
        region_for("arn:aws:braket:::device/quantum-simulator/amazon/sv1")
        == "us-east-1"
    )
    # an explicit override always wins
    assert region_for("arn:aws:braket:eu-north-1::device/x", "us-west-2") == "us-west-2"


def test_ready_at_queue_position_one(fake_braket):
    b = _backend()
    b._task = FakeTask(states=["QUEUED", "QUEUED"], positions=["3", "1"])
    ok, why = b.wait_for_priority(interval=0, sleep=lambda s: None)
    assert ok and "position 1" in why


def test_ready_when_the_task_already_left_the_queue(fake_braket):
    """A task that is RUNNING reports no position, so waiting for 1 would hang."""
    b = _backend()
    b._task = FakeTask(states=["RUNNING"], positions=[None])
    ok, why = b.wait_for_priority(interval=0, sleep=lambda s: None)
    assert ok and why == "RUNNING"


def test_completed_is_also_ready(fake_braket):
    """On SV1 a trivial task can finish before we ever observe position 1."""
    b = _backend()
    b._task = FakeTask(states=["COMPLETED"], positions=[None])
    ok, why = b.wait_for_priority(interval=0, sleep=lambda s: None)
    assert ok and why == "COMPLETED"


def test_failed_is_not_ready(fake_braket):
    """The classical must be cancelled, not started against a task with no result."""
    b = _backend()
    b._task = FakeTask(states=["FAILED"], positions=[None])
    ok, why = b.wait_for_priority(interval=0, sleep=lambda s: None)
    assert not ok and why == "FAILED"


def test_cancelled_is_not_ready(fake_braket):
    b = _backend()
    b._task = FakeTask(states=["CANCELLED"], positions=[None])
    ok, why = b.wait_for_priority(interval=0, sleep=lambda s: None)
    assert not ok


def test_deep_queue_position_is_compared_as_a_string(fake_braket):
    """Braket reports anything over 2000 as the string >2000."""
    b = _backend()
    b._task = FakeTask(states=["QUEUED", "QUEUED"], positions=[">2000", "1"])
    ok, why = b.wait_for_priority(interval=0, sleep=lambda s: None)
    assert ok


def test_timeout_reports_the_position(fake_braket):
    b = _backend()
    b._task = FakeTask(states=["QUEUED"] * 5, positions=["7"] * 5)
    ok, why = b.wait_for_priority(timeout=1, interval=1, sleep=lambda s: None)
    assert not ok and "still queued at position 7" in why


def test_open_session_submits_a_one_qubit_no_op(fake_braket):
    from flux_quantum.backends.braket import SV1

    fake_braket["task"] = FakeTask(states=["QUEUED"], positions=["1"])
    b = _backend()
    arn = b.open_session({"device": SV1, "shots": 1, "queue_timeout": 0})
    assert arn == ARN
    assert fake_braket["device"] == SV1
    assert fake_braket["qubit"] == 0  # identity on qubit 0, a real no-op
    assert fake_braket["shots"] == 1


def test_open_session_raises_when_the_task_fails(fake_braket):
    """So the scout cancels the held classical instead of releasing it."""
    fake_braket["task"] = FakeTask(states=["FAILED"], positions=[None])
    with pytest.raises(RuntimeError, match="never reached the front of the queue"):
        _backend().open_session({"queue_timeout": 0})


def test_close_session_is_a_no_op(fake_braket):
    assert _backend().close_session(ARN) is None


def test_job_environment_carries_device_and_region(fake_braket):
    env = _backend().job_environment({"device": "arn:x", "region": "eu-north-1"})
    assert env["BRAKET_DEVICE"] == "arn:x"
    assert env["AWS_DEFAULT_REGION"] == "eu-north-1"
