"""Session handoff. The scout puts the session on the eventlog of the
classical job and wrap reads it back."""

import os
import signal
import time

import pytest

from flux_quantum.backends.base import Backend
from flux_quantum.scout import SESSION_KEY, post_session, wait_for_job
from flux_quantum.scout import _install_signal_handlers
from flux_quantum.wrap import read_session


def test_scout_posts_session_as_a_memo_on_the_classical_job():

    sent = {}

    class _Resp:
        def get(self):
            return {}

    def fake_rpc(topic, payload):
        sent["topic"] = topic
        sent["payload"] = payload
        return _Resp()

    post_session(handle=None, jobid=4021041664, session="sess-abc", rpc=fake_rpc)
    assert sent["topic"] == "job-manager.memo"
    assert sent["payload"]["id"] == 4021041664
    assert sent["payload"]["memo"][SESSION_KEY] == "sess-abc"


class _Event:
    def __init__(self, name, context):
        self.name = name
        self.context = context


def test_wrap_reads_the_session_from_its_own_eventlog():

    def watcher(handle, jobid):
        # the memo is already there when the job starts
        yield _Event("submit", {})
        yield _Event("memo", {SESSION_KEY: "sess-xyz"})
        yield _Event("alloc", {})

    assert read_session(None, 123, watcher=watcher) == "sess-xyz"


def test_wrap_ignores_unrelated_memos():

    def watcher(handle, jobid):
        yield _Event("memo", {"note": "something else"})
        yield _Event("memo", {SESSION_KEY: "the-right-one"})

    assert read_session(None, 123, watcher=watcher) == "the-right-one"


def test_wrap_errors_clearly_when_no_memo_is_ever_posted():

    def watcher(handle, jobid):
        yield _Event("submit", {})
        yield _Event("clean", {})

    with pytest.raises(RuntimeError, match="without a session memo"):
        read_session(None, 123, watcher=watcher)


def test_wrap_times_out_rather_than_hanging_forever():
    """A dead scout must not block the job forever."""

    def watcher(handle, jobid):
        yield _Event("submit", {})
        time.sleep(5)  # eventlog follows a live job and never yields the memo
        yield _Event("memo", {"quantum_session": "too-late"})

    start = time.time()
    with pytest.raises(RuntimeError, match="timed out"):
        read_session(None, 123, timeout=0.5, watcher=watcher)
    assert time.time() - start < 3  # the alarm fired, we did not wait out the sleep


# the scout holds the qpu and the vendor session for as long as the job runs


def test_backend_close_session_defaults_to_noop():
    """Vendors with nothing to release need not implement close_session."""

    class _Bare(Backend):
        name = "bare"

        def credentials_present(self):
            return True, "ok"

        def probe(self):
            return None

    assert _Bare().close_session("anything") is None


def test_mock_backend_closes_the_session(monkeypatch):
    monkeypatch.setenv("FLUX_QUANTUM_MOCK", "1")
    import importlib
    from flux_quantum.backends import mock as mockmod

    importlib.reload(mockmod)
    b = mockmod.MockBackend()
    sid = b.open_session({"session": "S1"})
    b.close_session(sid)
    assert b._closed == "S1"


def test_scout_waits_for_the_classical_before_closing():
    """Wait on clean, and do not raise when the job failed."""

    seen = {}

    def fake_waiter(handle, jobid, name, raiseJobException=True):
        seen["jobid"] = jobid
        seen["name"] = name
        seen["raise"] = raiseJobException

    wait_for_job(None, 4021041664, waiter=fake_waiter)
    assert seen["jobid"] == 4021041664
    assert seen["name"] == "clean"
    assert seen["raise"] is False


def test_signal_handlers_unwind_so_the_session_is_closed():
    """A cancelled scout still runs its finally block."""

    previous = signal.getsignal(signal.SIGTERM)
    try:
        _install_signal_handlers()
        closed = []
        try:
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            finally:
                closed.append("closed")
        except SystemExit:
            pass
        assert closed == ["closed"]
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_abort_cancels_the_held_job():
    """A failure before the release must not leave the classical held forever."""
    from flux_quantum.scout import abort_held

    cancelled = {}

    def fake_cancel(handle, jobid, why):
        cancelled["jobid"] = jobid
        cancelled["why"] = why

    with pytest.raises(SystemExit):
        abort_held(None, 4021041664, "opening ibm session failed", cancel=fake_cancel)
    assert cancelled["jobid"] == 4021041664
    assert "opening ibm session failed" in cancelled["why"]


def test_abort_still_exits_when_the_cancel_fails():
    """A broker we cannot reach must not turn into a hang."""
    from flux_quantum.scout import abort_held

    def boom(handle, jobid, why):
        raise RuntimeError("no broker")

    with pytest.raises(SystemExit):
        abort_held(None, 1, "whatever", cancel=boom)


def test_memo_carries_a_durable_release_marker():
    """A release lives only in scheduler memory, so a qmanager restart would
    park the job again. The eventlog survives, and fluxion reads it there."""
    from flux_quantum.scout import post_session, SESSION_KEY

    sent = {}

    class Fut:
        def get(self):
            return None

    def rpc(topic, payload):
        sent["topic"] = topic
        sent["payload"] = payload
        return Fut()

    post_session(None, 4021041664, "sess-1", rpc=rpc)
    memo = sent["payload"]["memo"]
    assert memo[SESSION_KEY] == "sess-1"
    assert memo["released"] == 1
    assert sent["payload"]["id"] == 4021041664
