"""The IonQ backend, without a key.

Most of these drive the backend with a scripted client. The last few run it
against the fake server over real HTTP, so the request shapes are checked
end to end: the header, the JSON, the paths.
"""

import time

import pytest


class Scripted:
    """A client that records every call and answers from a script.

    answers maps (method, path) to a response or a list of responses that
    are handed out in order, the last one repeating.
    """

    url = "https://fake"

    def __init__(self, answers):
        self.answers = {
            k: (v if isinstance(v, list) else [v]) for k, v in answers.items()
        }
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        key = (method, path)
        if key not in self.answers:
            raise AssertionError("unexpected call {} {}".format(method, path))
        queue = self.answers[key]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body if body is not None else {})

    def put(self, path, body=None):
        return self.request("PUT", path, body)


def _common(**kw):
    """The common options as the CLI hands them over, dry run applied."""
    from flux_quantum.backends.ionq import IonQBackend

    common = {
        "device": None,
        "hold": "session",
        "hold_max": 900.0,
        "wait": 0.0,
        "dry_run": False,
    }
    common.update(kw)
    if common["dry_run"]:
        common = IonQBackend(client=Scripted({})).dry_run(common)
    return common


def test_no_key_is_reported_by_name(monkeypatch):
    from flux_quantum.backends import BackendError
    from flux_quantum.backends.ionq import IonQBackend

    monkeypatch.delenv("IONQ_API_KEY", raising=False)
    monkeypatch.delenv("IONQ_API_TOKEN", raising=False)
    with pytest.raises(BackendError) as e:
        IonQBackend()
    assert "IONQ_API_KEY" in str(e.value)


def test_the_key_is_never_in_the_note(monkeypatch):
    from flux_quantum.backends.ionq import IonQBackend

    monkeypatch.setenv("IONQ_API_KEY", "supersecret")
    b = IonQBackend()
    assert "supersecret" not in b.credential_note
    assert "IONQ_API_KEY" in b.credential_note


def test_ionq_is_a_registered_vendor(backends_real):
    assert "ionq" in backends_real.known_vendors()


def test_defaults_target_the_qpu_with_a_session():
    from flux_quantum.backends.ionq import IonQBackend

    opts = IonQBackend(client=Scripted({})).scout_options(_common())
    assert opts["target"] == "qpu.forte-1"
    assert opts["hold"] == "session"
    assert opts["noise"] is None and not opts["dry_run"]
    # 900 seconds is 15 minutes, the session's own unit
    assert opts["max_minutes"] == 15 and opts["shots"] == 100
    assert opts["timeout"] == 0


def test_dry_run_goes_to_the_simulator_with_the_qpu_noise():
    """The simulator is free. Keeping the noise model means the circuit still
    compiles the way the hardware would compile it."""
    from flux_quantum.backends.ionq import IonQBackend

    b = IonQBackend(client=Scripted({}))
    common = b.dry_run({"device": "qpu.forte-enterprise-1"})
    assert common["device"] == "simulator" and common["noise"] == "forte-enterprise-1"
    opts = b.scout_options(common)
    assert opts["target"] == "simulator"
    assert opts["noise"] == "forte-enterprise-1"


def test_the_hold_limit_rounds_up_to_whole_minutes():
    from flux_quantum.backends.ionq import IonQBackend

    b = IonQBackend(client=Scripted({}))
    assert b.scout_options(_common(hold_max=61))["max_minutes"] == 2
    assert b.scout_options(_common(hold_max=1))["max_minutes"] == 1


def test_shots_are_tuning(monkeypatch):
    from flux_quantum.backends.ionq import IonQBackend

    monkeypatch.setenv("FLUX_QUANTUM_IONQ_SHOTS", "250")
    assert IonQBackend(client=Scripted({})).scout_options(_common())["shots"] == 250


def test_the_simulator_itself_gets_no_noise_model():
    from flux_quantum.backends.ionq import IonQBackend

    b = IonQBackend(client=Scripted({}))
    common = b.dry_run({"device": "simulator"})
    assert common["device"] == "simulator" and common["noise"] is None


def test_the_classical_job_learns_the_target_not_the_key():
    from flux_quantum.backends.ionq import IonQBackend

    b = IonQBackend(client=Scripted({}))
    env = b.job_environment({"target": "simulator", "noise": "forte-1"})
    assert env["IONQ_BACKEND"] == "simulator"
    assert env["IONQ_NOISE_MODEL"] == "forte-1"
    assert env["IONQ_API_URL"] == "https://fake"
    assert not any("KEY" in k for k in env)


def test_a_session_is_opened_then_warmed_up_inside_it():
    from flux_quantum.backends.ionq import IonQBackend

    c = Scripted(
        {
            ("POST", "/sessions"): {
                "id": "sess-1",
                "status": "pending",
                "active": False,
            },
            ("POST", "/jobs"): {
                "id": "job-1",
                "status": "submitted",
                "session_id": "sess-1",
            },
        }
    )
    b = IonQBackend(client=c)
    opened = b.open_session(
        {"target": "qpu.forte-1", "hold": "session", "max_minutes": 7, "shots": 100}
    )
    assert opened == "sess-1"
    assert b.session_id(opened) == "sess-1"
    _, _, session = c.calls[0]
    assert session == {"backend": "qpu.forte-1", "settings": {"duration_limit_min": 7}}
    _, _, job = c.calls[1]
    assert job["session_id"] == "sess-1"
    assert job["backend"] == "qpu.forte-1"
    assert job["shots"] == 100
    assert job["input"]["qubits"] == 1
    assert "noise" not in job


def test_a_dry_run_warmup_carries_the_noise_model():
    from flux_quantum.backends.ionq import IonQBackend

    c = Scripted({("POST", "/jobs"): {"id": "job-1", "status": "submitted"}})
    IonQBackend(client=c).open_session(
        {"target": "simulator", "noise": "forte-1", "hold": "probe", "shots": 10}
    )
    assert c.calls[0][2]["noise"] == {"model": "forte-1"}
    assert c.calls[0][2]["backend"] == "simulator"


def test_an_account_without_sessions_is_told_what_to_do():
    from flux_quantum.backends import BackendError
    from flux_quantum.backends.ionq import APIError, IonQBackend

    c = Scripted(
        {("POST", "/sessions"): APIError(403, "POST", "/sessions", b"forbidden")}
    )
    with pytest.raises(BackendError) as e:
        IonQBackend(client=c).open_session({"target": "qpu.forte-1", "hold": "session"})
    assert "beta" in str(e.value) and "--quantum-hold probe" in str(e.value)
    assert len(c.calls) == 1


def test_a_dry_run_falls_back_to_a_probe_and_says_so(capsys):
    """The simulator may not take sessions. A dry run is there to exercise
    the plumbing, so it carries on with a probe rather than stopping."""
    from flux_quantum.backends.ionq import APIError, IonQBackend

    c = Scripted(
        {
            ("POST", "/sessions"): APIError(404, "POST", "/sessions", b"nope"),
            ("POST", "/jobs"): {"id": "job-1", "status": "submitted"},
        }
    )
    b = IonQBackend(client=c)
    opened = b.open_session({"target": "simulator", "hold": "session", "dry_run": True})
    assert opened == "job-1"
    assert "session_id" not in c.calls[1][2]
    assert "probe job instead" in capsys.readouterr().out


def test_a_server_error_on_sessions_is_not_swallowed():
    from flux_quantum.backends.ionq import APIError, IonQBackend

    c = Scripted({("POST", "/sessions"): APIError(500, "POST", "/sessions", b"boom")})
    with pytest.raises(APIError):
        IonQBackend(client=c).open_session({"target": "qpu.forte-1", "dry_run": True})


def test_ready_once_the_session_is_active(capsys):
    from flux_quantum.backends.ionq import IonQBackend

    c = Scripted(
        {
            ("POST", "/sessions"): {"id": "sess-1"},
            ("POST", "/jobs"): {"id": "job-1"},
            ("GET", "/jobs/job-1"): [{"status": "submitted"}, {"status": "ready"}],
            ("GET", "/sessions/sess-1"): [
                {"status": "pending", "active": False},
                {"status": "active", "active": True, "started_at": "t"},
            ],
        }
    )
    b = IonQBackend(client=c)
    b.open_session({"target": "qpu.forte-1"})
    ok, why = b.wait_for_priority({}, interval=0, sleep=lambda _: None)
    assert ok and "session active" in why
    assert "session pending, warm-up job submitted" in capsys.readouterr().out


def test_a_started_warmup_counts_as_active_even_if_the_session_lags():
    from flux_quantum.backends.ionq import IonQBackend

    c = Scripted(
        {
            ("POST", "/sessions"): {"id": "sess-1"},
            ("POST", "/jobs"): {"id": "job-1"},
            ("GET", "/jobs/job-1"): {"status": "started"},
            ("GET", "/sessions/sess-1"): {"status": "pending", "active": False},
        }
    )
    b = IonQBackend(client=c)
    b.open_session({"target": "qpu.forte-1"})
    ok, _ = b.wait_for_priority({}, interval=0, sleep=lambda _: None)
    assert ok


def test_a_failed_warmup_is_a_failure_not_readiness():
    from flux_quantum.backends.ionq import IonQBackend

    c = Scripted(
        {
            ("POST", "/jobs"): {"id": "job-1"},
            ("GET", "/jobs/job-1"): {
                "status": "failed",
                "failure": {"error": "circuit too wide"},
            },
        }
    )
    b = IonQBackend(client=c)
    b.open_session({"target": "qpu.forte-1", "hold": "probe"})
    ok, why = b.wait_for_priority({}, interval=0, sleep=lambda _: None)
    assert not ok and "failed" in why and "circuit too wide" in why


def test_a_session_that_ends_first_is_a_failure():
    from flux_quantum.backends.ionq import IonQBackend

    c = Scripted(
        {
            ("POST", "/sessions"): {"id": "sess-1"},
            ("POST", "/jobs"): {"id": "job-1"},
            ("GET", "/jobs/job-1"): {"status": "submitted"},
            ("GET", "/sessions/sess-1"): {"status": "expired", "ended_at": "t"},
        }
    )
    b = IonQBackend(client=c)
    b.open_session({"target": "qpu.forte-1"})
    ok, why = b.wait_for_priority({}, interval=0, sleep=lambda _: None)
    assert not ok and "expired" in why


def test_the_wait_gives_up_at_the_timeout():
    import time

    from flux_quantum.backends.ionq import IonQBackend

    c = Scripted(
        {
            ("POST", "/jobs"): {"id": "job-1"},
            ("GET", "/jobs/job-1"): {"status": "submitted"},
        }
    )
    b = IonQBackend(client=c)
    b.open_session({"target": "qpu.forte-1", "hold": "probe"})
    b._hold_started = time.time() - 100
    ok, why = b.wait_for_priority({"timeout": 60}, interval=1, sleep=lambda _: None)
    assert not ok and "gave up" in why


def test_probe_mode_is_ready_when_the_job_starts():
    from flux_quantum.backends.ionq import IonQBackend

    c = Scripted(
        {
            ("POST", "/jobs"): {"id": "job-1"},
            ("GET", "/jobs/job-1"): [{"status": "submitted"}, {"status": "started"}],
        }
    )
    b = IonQBackend(client=c)
    assert b.open_session({"target": "qpu.forte-1", "hold": "probe"}) == "job-1"
    # a job id is not something the classical job can submit into, and the
    # handover says so
    assert b.session_id("job-1") == "job:job-1"
    ok, why = b.wait_for_priority({}, interval=0, sleep=lambda _: None)
    assert ok and "started" in why
    assert not any(path.startswith("/sessions") for _, path, _ in c.calls)


def test_closing_ends_the_session_once():
    from flux_quantum.backends.ionq import IonQBackend

    c = Scripted(
        {
            ("POST", "/sessions"): {"id": "sess-1"},
            ("POST", "/jobs"): {"id": "job-1"},
            ("POST", "/sessions/sess-1/end"): {"status": "ended"},
        }
    )
    b = IonQBackend(client=c)
    b.open_session({"target": "qpu.forte-1"})
    b.close_session()
    b.close_session()
    ends = [p for m, p, _ in c.calls if p.endswith("/end")]
    assert ends == ["/sessions/sess-1/end"]


def test_closing_a_probe_cancels_only_an_unfinished_job():
    from flux_quantum.backends.ionq import IonQBackend

    c = Scripted(
        {
            ("POST", "/jobs"): {"id": "job-1"},
            ("GET", "/jobs/job-1"): {"status": "ready"},
            ("PUT", "/jobs/job-1/status/cancel"): {"status": "canceled"},
        }
    )
    b = IonQBackend(client=c)
    b.open_session({"target": "qpu.forte-1", "hold": "probe"})
    b.close_session()
    assert ("PUT", "/jobs/job-1/status/cancel", None) in c.calls

    c = Scripted(
        {
            ("POST", "/jobs"): {"id": "job-2"},
            ("GET", "/jobs/job-2"): {"status": "completed"},
        }
    )
    b = IonQBackend(client=c)
    b.open_session({"target": "qpu.forte-1", "hold": "probe"})
    b.close_session()
    assert not any(m == "PUT" for m, _, _ in c.calls)


def test_a_failed_close_is_reported_not_raised(capsys):
    from flux_quantum.backends.ionq import APIError, IonQBackend

    c = Scripted(
        {
            ("POST", "/sessions"): {"id": "sess-1"},
            ("POST", "/jobs"): {"id": "job-1"},
            ("POST", "/sessions/sess-1/end"): APIError(500, "POST", "/x", b"down"),
        }
    )
    b = IonQBackend(client=c)
    b.open_session({"target": "qpu.forte-1"})
    b.close_session()
    assert "could not end session sess-1" in capsys.readouterr().out


def test_probe_reads_the_backend(monkeypatch):
    from flux_quantum.backends.ionq import IonQBackend

    monkeypatch.delenv("FLUX_QUANTUM_MOCK", raising=False)
    monkeypatch.delenv("IONQ_BACKEND", raising=False)
    c = Scripted(
        {
            ("GET", "/backends/qpu.forte-1"): {
                "status": "available",
                "degraded": True,
                "qubits": 36,
                "average_queue_time": 1200,
            }
        }
    )
    sig = IonQBackend(client=c).probe()
    assert sig.available and sig.queue_depth == 1200
    assert sig.detail["degraded"] is True and sig.detail["qubits"] == 36


def test_probe_follows_mock_to_the_simulator(monkeypatch):
    from flux_quantum.backends.ionq import IonQBackend

    monkeypatch.setenv("FLUX_QUANTUM_MOCK", "1")
    c = Scripted({("GET", "/backends/simulator"): {"status": "available"}})
    assert IonQBackend(client=c).probe().available


def test_an_unreachable_service_is_unavailable_not_an_exception():
    from flux_quantum.backends import BackendError
    from flux_quantum.backends.ionq import IonQBackend

    c = Scripted({("GET", "/backends/qpu.forte-1"): BackendError("ionq: cannot reach")})
    sig = IonQBackend(client=c).probe()
    assert not sig.available and "cannot reach" in sig.detail["error"]


def test_noise_model_names():
    from flux_quantum.backends.ionq import noise_model

    assert noise_model("qpu.forte-1") == "forte-1"
    assert noise_model("qpu.aria-2") == "aria-2"
    assert noise_model("simulator") is None


# ---------------------------------------------------------------------------
# over real HTTP, against the fake server


@pytest.fixture
def fake_ionq():
    from flux_quantum.backends.ionq import fake

    server, state = fake.serve(0)
    yield "http://127.0.0.1:%d" % server.server_address[1], state
    server.shutdown()


def test_the_client_sends_the_key_and_json(fake_ionq, monkeypatch):
    from flux_quantum.backends.ionq import Client

    url, state = fake_ionq
    c = Client("k-123", url)
    b = c.get("/backends/simulator")
    assert b["status"] == "available"
    made = c.post("/jobs", {"backend": "simulator", "shots": 5})
    assert made["status"] == "submitted"
    assert state.requests[-1] == ("POST", "/jobs", {"backend": "simulator", "shots": 5})


def test_a_bad_key_is_an_api_error_with_the_status(fake_ionq):
    from flux_quantum.backends.ionq import APIError, Client

    url, _ = fake_ionq
    with pytest.raises(APIError) as e:
        Client("", url).get("/backends/simulator")
    assert e.value.status == 401


def test_the_whole_scout_lifecycle_against_the_fake_server(
    fake_ionq, monkeypatch, capsys
):
    """open, wait, hand over, close. What the scout does, over HTTP."""
    from flux_quantum.backends.ionq import IonQBackend

    url, state = fake_ionq
    monkeypatch.setenv("IONQ_API_KEY", "k")
    monkeypatch.setenv("IONQ_API_URL", url)
    monkeypatch.setenv("FLUX_QUANTUM_MOCK", "1")
    b = IonQBackend()
    opts = b.scout_options(b.dry_run({"device": "qpu.forte-1", "hold_max": 900}))
    assert opts["target"] == "simulator" and opts["noise"] == "forte-1"

    opened = b.open_session(opts)
    assert opened in state.sessions
    ok, why = b.wait_for_priority(opts, interval=0, sleep=lambda _: None)
    assert ok, why
    assert state.sessions[opened]["active"]
    session = b.session_id(opened)
    assert session == opened

    # the warm-up went to the simulator, in the session, with the noise model
    (job,) = state.jobs.values()
    assert job["backend"] == "simulator"
    assert job["session_id"] == opened
    assert job["noise"] == {"model": "forte-1"}

    b.close_session(session)
    assert state.sessions[opened]["status"] == "ended"


def test_the_lifecycle_without_sessions_falls_back_in_a_dry_run(fake_ionq, monkeypatch):
    from flux_quantum.backends.ionq import IonQBackend

    url, state = fake_ionq
    state.no_sessions = True
    monkeypatch.setenv("IONQ_API_KEY", "k")
    monkeypatch.setenv("IONQ_API_URL", url)
    monkeypatch.setenv("FLUX_QUANTUM_MOCK", "1")
    b = IonQBackend()
    opts = b.scout_options(b.dry_run({}))
    opened = b.open_session(opts)
    assert opened in state.jobs
    ok, why = b.wait_for_priority(opts, interval=0, sleep=lambda _: None)
    assert ok and "warm-up job" in why
    assert b.session_id(opened).startswith("job:")
    b.close_session(opened)
    assert state.sessions == {}


# ---------------------------------------------------------------------------
# the fake's device queue


def test_the_fake_queue_holds_a_job_in_submitted_until_its_wait_is_up():
    from flux_quantum.backends.ionq import fake

    state = fake.State(queue="0.3")
    _, made = state.create_job({"backend": "simulator"})
    for _ in range(3):
        assert state.get_job(made["id"])[1]["status"] == "submitted"
    time.sleep(0.35)
    assert state.get_job(made["id"])[1]["status"] == "ready"


def test_a_started_sessions_jobs_skip_the_fake_queue():
    """The session's first job queues like anyone's. Once it starts the
    device is the session's, and the next job is served on arrival."""
    from flux_quantum.backends.ionq import fake

    state = fake.State(queue="0.3")
    _, s = state.create_session({"backend": "qpu.forte-1"})
    _, first = state.create_job({"backend": "qpu.forte-1", "session_id": s["id"]})
    assert state.get_job(first["id"])[1]["status"] == "submitted"
    time.sleep(0.35)
    state.get_job(first["id"])
    state.get_job(first["id"])
    assert state.sessions[s["id"]]["active"]
    _, second = state.create_job({"backend": "qpu.forte-1", "session_id": s["id"]})
    assert state.get_job(second["id"])[1]["status"] == "ready"


def test_the_fake_queue_is_a_range_and_backends_reports_its_mean(fake_ionq):
    from flux_quantum.backends.ionq import Client, fake

    assert fake.parse_queue("30-90") == (30.0, 90.0)
    assert fake.parse_queue("30") == (30.0, 30.0)
    assert fake.parse_queue("") == (0.0, 0.0)
    state = fake.State(queue="30-90")
    assert 30 <= state.queue_wait() <= 90
    assert state.backend("qpu.forte-1")[1]["average_queue_time"] == 60

    url, state = fake_ionq
    c = Client("k", url)
    assert c.get("/fake/config")["queue"] == "0-0"
    assert c.post("/fake/config", {"queue": "5-10"})["queue"] == "5-10"
    assert state.queue == (5.0, 10.0)
    assert c.get("/backends/simulator")["average_queue_time"] == 7.5
    made = c.post("/jobs", {"backend": "simulator"})
    assert "queue" not in c.get("/jobs/%s" % made["id"])


def test_a_cost_limit_from_the_environment_goes_on_the_session(fake_ionq, monkeypatch):
    from flux_quantum.backends.ionq import IonQBackend

    url, state = fake_ionq
    monkeypatch.setenv("IONQ_API_KEY", "k")
    monkeypatch.setenv("IONQ_API_URL", url)
    monkeypatch.setenv("FLUX_QUANTUM_IONQ_COST_LIMIT_USD", "25")
    b = IonQBackend()
    b.open_session(b.scout_options({"device": "qpu.forte-1"}))
    posted = [
        body for m, path, body in state.requests if (m, path) == ("POST", "/sessions")
    ]
    assert posted[0]["settings"]["cost_limit"] == {"unit": "usd", "value": 25.0}
    assert posted[0]["settings"]["duration_limit_min"] == 15
