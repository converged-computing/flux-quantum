"""The session handoff: scout posts a memo on the classical job's eventlog,
wrap reads it back. No shared filesystem, no polling."""

import pytest


def test_scout_posts_session_as_a_memo_on_the_classical_job():
    from flux_quantum.scout import post_session, SESSION_KEY

    sent = {}

    class _Resp:
        def get(self):
            return {}

    def fake_rpc(topic, payload):
        sent["topic"] = topic
        sent["payload"] = payload
        return _Resp()

    post_session(handle=None, jobid=4021041664, session="sess-abc", rpc=fake_rpc)
    # job-manager.memo is FLUX_ROLE_USER and authorized against the job's owner
    # uid -- the same authorization the release RPC needs.
    assert sent["topic"] == "job-manager.memo"
    assert sent["payload"]["id"] == 4021041664
    assert sent["payload"]["memo"][SESSION_KEY] == "sess-abc"


class _Event:
    def __init__(self, name, context):
        self.name = name
        self.context = context


def test_wrap_reads_the_session_from_its_own_eventlog():
    from flux_quantum.wrap import read_session
    from flux_quantum.scout import SESSION_KEY

    def watcher(handle, jobid):
        # a realistic replay: the memo is already present before the job starts,
        # because the scout posts it BEFORE the release.
        yield _Event("submit", {})
        yield _Event("memo", {SESSION_KEY: "sess-xyz"})
        yield _Event("alloc", {})

    assert read_session(None, 123, watcher=watcher) == "sess-xyz"


def test_wrap_ignores_unrelated_memos():
    from flux_quantum.wrap import read_session
    from flux_quantum.scout import SESSION_KEY

    def watcher(handle, jobid):
        yield _Event("memo", {"note": "something else"})
        yield _Event("memo", {SESSION_KEY: "the-right-one"})

    assert read_session(None, 123, watcher=watcher) == "the-right-one"


def test_wrap_errors_clearly_when_no_memo_is_ever_posted():
    from flux_quantum.wrap import read_session

    def watcher(handle, jobid):
        yield _Event("submit", {})
        yield _Event("clean", {})

    with pytest.raises(RuntimeError, match="without a session memo"):
        read_session(None, 123, watcher=watcher)


def test_wrap_times_out_rather_than_hanging_forever():
    """A scout that dies after release must not leave the job blocked forever."""
    import time
    from flux_quantum.wrap import read_session

    def watcher(handle, jobid):
        yield _Event("submit", {})
        time.sleep(5)  # eventlog follows a live job and never yields the memo
        yield _Event("memo", {"quantum_session": "too-late"})

    start = time.time()
    with pytest.raises(RuntimeError, match="timed out"):
        read_session(None, 123, timeout=0.5, watcher=watcher)
    assert time.time() - start < 3  # the alarm fired, we did not wait out the sleep


# ---------------------------------------------------------------------------
# session lifecycle: the scout holds the qpu allocation (and the vendor
# session) for exactly as long as the classical job runs.
# ---------------------------------------------------------------------------


def test_backend_close_session_defaults_to_noop():
    """Vendors with nothing to release (e.g. Braket on-demand) must not have to
    implement close_session."""
    from flux_quantum.backends.base import Backend

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
    """wait_for_job must block on the classical's `clean` event, and must not
    raise when the classical failed -- the session still has to be closed."""
    from flux_quantum.scout import wait_for_job

    seen = {}

    def fake_waiter(handle, jobid, name, raiseJobException=True):
        seen["jobid"] = jobid
        seen["name"] = name
        seen["raise"] = raiseJobException

    wait_for_job(None, 4021041664, waiter=fake_waiter)
    assert seen["jobid"] == 4021041664
    assert seen["name"] == "clean"
    # a failed classical still reaches clean; we must not raise past it
    assert seen["raise"] is False


def test_signal_handlers_unwind_so_the_session_is_closed():
    """A cancelled scout must still run its finally block."""
    import os
    import signal
    from flux_quantum.scout import _install_signal_handlers

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
