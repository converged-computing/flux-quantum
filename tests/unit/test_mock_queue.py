"""The simulated vendor queue.

This is the experiment instrument, so the wait has to be a controlled function
of depth rather than something approximate. No real vendor lets you set queue
depth, which is why the measurements come from here.
"""

import pytest


@pytest.fixture
def mock(monkeypatch):
    monkeypatch.setenv("FLUX_QUANTUM_MOCK", "1")
    import importlib
    from flux_quantum.backends import mock as mockmod

    importlib.reload(mockmod)
    return mockmod.MockBackend()


class Args:
    mock_session = None
    mock_latency = None
    mock_queue_depth = None
    mock_service_time = None
    mock_base_overhead = None
    mock_jitter = None
    mock_seed = None


def test_defaults_match_what_ibm_measured(mock):
    """ibm_marrakesh at depth 0 took 10 to 12 seconds to dequeue, so the fixed
    overhead is not invented."""
    opts = mock.scout_options(Args())
    assert opts["base_overhead"] == 10.0
    assert opts["queue_depth"] == 0
    assert opts["service_time"] == 0.1
    assert opts["jitter"] == 0.0


def test_wait_is_linear_in_depth(mock):
    base = {"base_overhead": 10.0, "service_time": 0.5, "jitter": 0.0}
    assert mock.queue_wait(dict(base, queue_depth=0)) == 10.0
    assert mock.queue_wait(dict(base, queue_depth=10)) == 15.0
    assert mock.queue_wait(dict(base, queue_depth=100)) == 60.0


def test_deterministic_without_jitter(mock):
    opts = {"queue_depth": 7, "service_time": 1.0, "base_overhead": 2.0}
    assert len({mock.queue_wait(opts) for _ in range(5)}) == 1


def test_jitter_is_repeatable_with_a_seed(mock):
    a = {
        "queue_depth": 10,
        "service_time": 1.0,
        "base_overhead": 0.0,
        "jitter": 0.5,
        "seed": 7,
    }
    b = dict(a, seed=8)
    assert mock.queue_wait(a) == mock.queue_wait(a)
    assert mock.queue_wait(a) != mock.queue_wait(b)


def test_priority_wait_sleeps_the_computed_time(mock):
    """The scout blocks here while the classical sits held, which is the whole
    point, so the total slept has to be the queue wait."""
    slept = []
    clock = [0.0]

    def fake_sleep(s):
        slept.append(s)
        clock[0] += s

    ok, why = mock.wait_for_priority(
        {"queue_depth": 5, "service_time": 2.0, "base_overhead": 0.0},
        sleep=fake_sleep,
        now=lambda: clock[0],
    )
    assert ok
    assert sum(slept) == pytest.approx(10.0)
    assert "depth 5" in why


def test_depth_zero_still_waits_the_overhead(mock):
    slept = []
    clock = [0.0]

    def fake_sleep(s):
        slept.append(s)
        clock[0] += s

    ok, why = mock.wait_for_priority(
        {"queue_depth": 0, "base_overhead": 10.0},
        sleep=fake_sleep,
        now=lambda: clock[0],
    )
    assert ok and sum(slept) == pytest.approx(10.0)


def test_position_is_reported_while_draining(mock, capsys):
    """The scout output carries the same position series a real vendor gives,
    so the experiment reads one format either way."""
    mock.wait_for_priority(
        {"queue_depth": 3, "service_time": 0.0, "base_overhead": 0.0},
        sleep=lambda s: None,
        now=lambda: 0.0,
    )
    out = capsys.readouterr().out
    assert "position 3" in out and "position 1" in out
