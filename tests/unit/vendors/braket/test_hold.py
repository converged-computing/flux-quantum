"""The hybrid job hold, tested without AWS.

A running hybrid job holds the priority queue for its device and publishes
AMZN_BRAKET_JOB_TOKEN. Work submitted anywhere with that token gets the job's
priority. Without it the docs say it gets none and bills standalone. So the
token, not the job ARN, is what the classical job has to be handed.
"""

import json


def test_entry_publishes_the_token_then_waits_for_release():
    from flux_quantum.backends.braket import hold as entry

    written = {}
    released = {"yet": False}

    class FakeS3:
        def put_object(self, Bucket, Key, Body):
            written[Key] = Body

        def head_object(self, Bucket, Key):
            if not released["yet"]:
                raise RuntimeError("not there")
            return {}

    def sleep(_):
        released["yet"] = True

    env = {
        "AMZN_BRAKET_OUT_S3_BUCKET": "bucket",
        "AMZN_BRAKET_JOB_TOKEN": "tok-123",
        "AMZN_BRAKET_DEVICE_ARN": "arn:aws:braket:::device/quantum-simulator/amazon/sv1",
        "AMZN_BRAKET_JOB_ARN": "arn:aws:braket:us-east-1:1:job/x",
        "FLUX_QUANTUM_PREFIX": "flux-quantum/x",
        "FLUX_QUANTUM_MAX_SECONDS": "60",
        "FLUX_QUANTUM_POLL": "0",
    }
    rc = entry.main(env=env, sleep=sleep, client=FakeS3())
    assert rc == 0
    body = json.loads(written["flux-quantum/x/token.json"].decode())
    assert body["token"] == "tok-123"
    assert body["device"].endswith("sv1")


def test_entry_gives_up_rather_than_billing_forever():
    """A scout that is never released has to stop. The instance bills by the
    minute for as long as it sits there."""
    from flux_quantum.backends.braket import hold as entry

    class FakeS3:
        def put_object(self, Bucket, Key, Body):
            pass

        def head_object(self, Bucket, Key):
            raise RuntimeError("never released")

    calls = {"n": 0}

    def sleep(_):
        calls["n"] += 1
        if calls["n"] > 50:
            raise AssertionError("did not respect the deadline")

    env = {
        "AMZN_BRAKET_OUT_S3_BUCKET": "bucket",
        "AMZN_BRAKET_JOB_TOKEN": "tok",
        "FLUX_QUANTUM_PREFIX": "p",
        "FLUX_QUANTUM_MAX_SECONDS": "0",
        "FLUX_QUANTUM_POLL": "0",
    }
    assert entry.main(env=env, sleep=sleep, client=FakeS3()) == 0


def _backend():
    """Skip __init__, which wants the SDK and credentials. Nothing here needs
    either."""
    from flux_quantum.backends.braket import BraketBackend

    b = BraketBackend.__new__(BraketBackend)
    b._session = None
    return b


def test_the_session_hold_is_the_hybrid_job():
    """The common session hold is Braket's hybrid job, and the hold limit
    arrives in seconds, which is what the hybrid job's stopping condition
    wants."""
    opts = _backend().scout_options({"hold": "session", "hold_max": 1200})
    assert opts["hold"] == "job"
    # the cheapest instance, since it does no work
    assert opts["hold_instance"] == "ml.m5.large"
    assert opts["hold_max_seconds"] == 1200


def test_session_is_the_default_and_probe_is_the_other_hold():
    assert _backend().scout_options({})["hold"] == "job"
    assert _backend().scout_options({"hold": "probe"})["hold"] == "probe"


def test_tuning_comes_from_the_environment(monkeypatch):
    """The instance and the probe shots are an operator's settings, not a
    submitter's, so they are not submit options."""
    monkeypatch.setenv("FLUX_QUANTUM_BRAKET_HOLD_INSTANCE", "ml.m5.xlarge")
    monkeypatch.setenv("FLUX_QUANTUM_BRAKET_SHOTS", "10")
    opts = _backend().scout_options({})
    assert opts["hold_instance"] == "ml.m5.xlarge" and opts["shots"] == 10


def test_a_dry_run_is_sv1():
    from flux_quantum.backends.braket import SV1

    b = _backend()
    common = b.dry_run({"device": "arn:aws:braket:us-east-1::device/qpu/ionq/Forte-1"})
    assert common["device"] == SV1 and common["dry_run"]
    assert b.scout_options(common)["device"] == SV1


def test_the_token_replaces_the_job_arn_as_the_session():
    """open_session returns the job ARN. The classical job needs the token,
    which only exists once the job runs."""
    b = _backend()
    assert b.session_id("arn:job") == "arn:job"
    b._session = "tok-abc"
    assert b.session_id("arn:job") == "tok-abc"


def test_base_session_id_is_a_passthrough():
    """The default hands back what open_session returned."""
    from flux_quantum.backends.base import Backend

    class Plain(Backend):
        name = "plain"

        def probe(self):
            return None

    assert Plain().session_id("whatever") == "whatever"


def _held(states, tokens):
    """A backend whose job walks through states and whose token shows up when
    tokens says so."""
    import types

    import flux_quantum.backends.braket as bk

    b = bk.BraketBackend.__new__(bk.BraketBackend)
    b._hold_prefix = "p"
    b._session = None
    b._hold_ready_after = None
    seq = list(states)
    b._hold_job = types.SimpleNamespace(state=lambda: seq.pop(0))
    b._output_bucket = lambda: "bucket"
    toks = list(tokens)
    b._read_token = lambda s3, bucket: toks.pop(0)
    return b, bk


def test_running_without_a_token_is_reported_as_starting(capsys):
    """Braket says RUNNING when the instance is up. The device is not ours
    until the container publishes its token, so that stretch reads as
    starting."""
    import types

    b, bk = _held(["RUNNING", "RUNNING"], [None, "tok"])
    b._hold_started = __import__("time").time()
    original = bk.boto3
    bk.boto3 = types.SimpleNamespace(client=lambda _: None)
    try:
        ok, why = b._wait_for_hold_job({}, 0, lambda _: None)
    finally:
        bk.boto3 = original

    assert ok
    assert "holding the device" in why
    assert "starting the container" in capsys.readouterr().out


def test_the_wait_is_timed():
    """How long from asking for the hold to having it. Minutes rather than
    milliseconds decides whether a scout can be started per pair."""
    import time
    import types

    b, bk = _held(["RUNNING"], ["tok"])
    b._hold_started = time.time() - 42
    original = bk.boto3
    bk.boto3 = types.SimpleNamespace(client=lambda _: None)
    try:
        ok, why = b._wait_for_hold_job({}, 0, lambda _: None)
    finally:
        bk.boto3 = original

    assert ok and "42s" in why
    assert 41 < b._hold_ready_after < 44


def test_a_job_that_died_says_so_and_for_how_long():
    import time
    import types

    b, bk = _held(["FAILED"], [None])
    b._hold_started = time.time() - 10
    original = bk.boto3
    bk.boto3 = types.SimpleNamespace(client=lambda _: None)
    try:
        ok, why = b._wait_for_hold_job({}, 0, lambda _: None)
    finally:
        bk.boto3 = original

    assert not ok and "FAILED" in why and "10s" in why


def test_verdict_calls_out_a_control_that_is_also_associated():
    """If the control is linked too, the comparison says nothing."""
    from probe_hold import verdict

    out = "\n".join(verdict({"job": "a"}, {"job": "a"}, "qpu"))
    assert "WARNING" in out


def test_verdict_says_when_the_token_did_not_travel():
    from probe_hold import SV1, verdict

    assert "NOT ASSOCIATED" in "\n".join(verdict({}, {}, SV1))


class _FakeWindow:
    def __init__(self, day, start, end):
        import datetime

        self.executionDay = type("D", (), {"value": day})()
        self.windowStartHour = datetime.time(*start)
        self.windowEndHour = datetime.time(*end)


ALWAYS = [_FakeWindow("Everyday", (0, 0), (23, 59))]


class _FakeDevice:
    """Enough of AwsDevice to exercise queue and window logic without AWS.

    fleet maps an arn to (normal, priority) or (normal, priority, windows).
    """

    fleet = {}

    def __init__(self, arn):
        self.arn = arn
        wins = self.fleet[arn][2] if len(self.fleet[arn]) > 2 else ALWAYS
        self.properties = type(
            "P", (), {"service": type("S", (), {"executionWindows": wins})()}
        )()

    def queue_depth(self):
        from braket.aws.queue_information import QueueDepthInfo, QueueType

        n, p = self.fleet[self.arn][:2]
        return QueueDepthInfo(
            quantum_tasks={QueueType.NORMAL: str(n), QueueType.PRIORITY: str(p)},
            jobs="0",
        )

    @classmethod
    def get_devices(cls, statuses=None):
        return [cls(a) for a in cls.fleet]


def test_queue_depth_splits_normal_from_priority(braket_sdk):
    from flux_quantum.backends.braket import queue_depth

    _FakeDevice.fleet = {"arn:aws:braket:::device/qpu/x/y": (7, 3)}
    q = queue_depth("arn:aws:braket:::device/qpu/x/y", cls=_FakeDevice)
    assert (q["normal"], q["priority"], q["jobs"]) == (7, 3, 0)


def test_only_qpus_with_work_waiting_are_offered(braket_sdk):
    """Priority means nothing on an idle device, and simulators never queue."""
    from flux_quantum.backends.braket import qpus_with_a_queue

    _FakeDevice.fleet = {
        "arn:aws:braket:::device/qpu/quera/Aquila": (59, 0),
        "arn:aws:braket:::device/qpu/iqm/Emerald": (1, 0),
        "arn:aws:braket:::device/qpu/iqm/Garnet": (0, 0),
        "arn:aws:braket:::device/quantum-simulator/amazon/sv1": (99, 0),
    }
    got = qpus_with_a_queue(cls=_FakeDevice)
    assert [d["arn"].split("/")[-1] for d in got] == ["Aquila", "Emerald"]


def test_a_device_that_cannot_be_read_is_skipped():
    from flux_quantum.backends.braket import qpus_with_a_queue

    class Broken(_FakeDevice):
        def queue_depth(self):
            raise RuntimeError("AccessDenied in this region")

    Broken.fleet = {"arn:aws:braket:::device/qpu/x/y": (5, 0)}
    assert qpus_with_a_queue(cls=Broken) == []


def test_a_maintenance_gap_counts_as_shut():
    """Garnet runs weekdays but stops between 15:30 and 17:15. From outside
    that looks the same as a long queue."""
    import datetime

    from flux_quantum.backends.braket import is_open

    wins = [
        _FakeWindow("Weekdays", (3, 15), (15, 29)),
        _FakeWindow("Weekdays", (17, 15), (23, 59)),
    ]
    _FakeDevice.fleet = {"arn:garnet": (7, 0, wins)}
    d = _FakeDevice("arn:garnet")
    monday = datetime.datetime(2026, 9, 28, tzinfo=datetime.timezone.utc)

    assert not is_open(d, monday.replace(hour=16, minute=23))
    assert is_open(d, monday.replace(hour=18))
    # Weekdays must not fire at the weekend
    assert not is_open(d, monday.replace(day=26, hour=18))


def test_everyday_windows_are_understood():
    import datetime

    from flux_quantum.backends.braket import is_open

    _FakeDevice.fleet = {"arn:x": (0, 0, ALWAYS)}
    assert is_open(
        _FakeDevice("arn:x"),
        datetime.datetime(2026, 9, 27, 3, tzinfo=datetime.timezone.utc),
    )


def test_survey_puts_open_devices_first_and_skips_shut_ones(braket_sdk):
    """The real case from a Monday afternoon. Aquila had 60 waiting and Garnet
    7, but both were shut, so only Forte was worth paying for."""
    import datetime

    from flux_quantum.backends.braket import qpus_with_a_queue, survey

    shut = [_FakeWindow("Friday", (4, 0), (11, 59))]
    _FakeDevice.fleet = {
        "arn:aws:braket:::device/qpu/quera/Aquila": (60, 0, shut),
        "arn:aws:braket:::device/qpu/iqm/Garnet": (7, 0, shut),
        "arn:aws:braket:::device/qpu/ionq/Forte": (1, 0, ALWAYS),
        "arn:aws:braket:::device/quantum-simulator/amazon/sv1": (99, 0, ALWAYS),
    }
    monday = datetime.datetime(2026, 9, 28, 16, tzinfo=datetime.timezone.utc)

    names = [r["arn"].split("/")[-1] for r in survey(cls=_FakeDevice, now=monday)]
    assert names[0] == "Forte", names
    assert "sv1" not in names
    assert [
        r["arn"].split("/")[-1] for r in qpus_with_a_queue(cls=_FakeDevice, now=monday)
    ] == ["Forte"]


def test_windows_are_reported_readably():
    from flux_quantum.backends.braket import windows

    _FakeDevice.fleet = {"arn:x": (0, 0, [_FakeWindow("Weekdays", (17, 15), (23, 59))])}
    assert windows(_FakeDevice("arn:x"), local=False) == [
        ("Weekdays", "17:15", "23:59")
    ]


def test_queue_depth_survives_the_note_braket_appends(braket_sdk):
    """The jobs field is a string and sometimes carries a message, as in
    "0 (1 prioritized hybrid job running)". int() on that raised mid run,
    after a task had already been submitted and paid for."""
    from braket.aws.queue_information import QueueDepthInfo, QueueType

    from flux_quantum.backends.braket import queue_depth

    class Chatty:
        def __init__(self, arn):
            pass

        def queue_depth(self):
            return QueueDepthInfo(
                quantum_tasks={QueueType.NORMAL: "7", QueueType.PRIORITY: "0"},
                jobs="0 (1 prioritized hybrid job running)",
            )

    q = queue_depth("arn:x", cls=Chatty)
    assert q["normal"] == 7
    assert q["jobs"] == 0
    assert "prioritized hybrid job running" in q["jobs_note"]


def _device(wins):
    return type(
        "D",
        (),
        {
            "properties": type(
                "P", (), {"service": type("S", (), {"executionWindows": wins})()}
            )()
        },
    )()


def test_windows_come_back_in_local_time():
    """Braket gives these in UTC. Reading them against your own clock is where
    the mistakes happen."""
    import datetime
    import os
    import time

    from flux_quantum.backends.braket import windows

    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    try:
        d = _device([_FakeWindow("Weekdays", (3, 15), (15, 29))])
        now = datetime.datetime(2026, 9, 28, tzinfo=datetime.timezone.utc)
        assert windows(d, now=now) == [("Weekdays", "05:15", "17:29")]
        assert windows(d, local=False, now=now) == [("Weekdays", "03:15", "15:29")]
    finally:
        del os.environ["TZ"]
        time.tzset()


def test_a_window_that_lands_on_another_day_says_so():
    """The day label is the UTC day, so a converted time can sit outside it."""
    import datetime
    import os
    import time

    from flux_quantum.backends.braket import windows

    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        d = _device([_FakeWindow("Weekdays", (3, 15), (15, 29))])
        now = datetime.datetime(2026, 9, 28, tzinfo=datetime.timezone.utc)
        assert windows(d, now=now) == [("Weekdays", "20:15 (prev day)", "08:29")]
    finally:
        del os.environ["TZ"]
        time.tzset()


def test_cleanup_undoes_a_run_that_failed_halfway():
    """The job was made but nothing else. Cleanup should still cancel it and
    clear the keys it would have used."""
    import types

    import flux_quantum.backends.braket as bk

    calls = []

    class S3:
        def put_object(self, Bucket, Key, Body):
            calls.append(("put", Key))

        def delete_object(self, Bucket, Key):
            calls.append(("delete", Key))

    b = bk.BraketBackend.__new__(bk.BraketBackend)
    b._made = {
        "job": types.SimpleNamespace(
            state=lambda: "RUNNING", cancel=lambda: calls.append(("cancel", None))
        ),
        "bucket": "amazon-braket-1",
        "prefix": "flux-quantum/j",
    }
    original = bk.boto3
    bk.boto3 = types.SimpleNamespace(client=lambda _: S3())
    try:
        b.close_session()
        # calling twice must not repeat the work
        b.close_session()
    finally:
        bk.boto3 = original

    assert calls == [
        ("put", "flux-quantum/j/release"),
        ("cancel", None),
        ("delete", "flux-quantum/j/token.json"),
        ("delete", "flux-quantum/j/release"),
    ], calls


def test_cleanup_continues_when_a_step_fails():
    """A cancel that fails must not stop the keys being deleted."""
    import types

    import flux_quantum.backends.braket as bk

    deleted = []

    class S3:
        def put_object(self, **kw):
            pass

        def delete_object(self, Bucket, Key):
            deleted.append(Key)

    def boom():
        raise RuntimeError("AccessDenied")

    b = bk.BraketBackend.__new__(bk.BraketBackend)
    b._made = {
        "job": types.SimpleNamespace(state=lambda: "RUNNING", cancel=boom),
        "bucket": "bucket",
        "prefix": "p",
    }
    original = bk.boto3
    bk.boto3 = types.SimpleNamespace(client=lambda _: S3())
    try:
        b.close_session()
    finally:
        bk.boto3 = original
    assert deleted == ["p/token.json", "p/release"]


def test_cleanup_with_nothing_made_is_a_noop():
    import flux_quantum.backends.braket as bk

    b = bk.BraketBackend.__new__(bk.BraketBackend)
    b._made = {"job": None, "bucket": None, "prefix": None}
    b.close_session()


def _holding(token_payload, device):
    """A backend whose hybrid job is running and has published a token."""
    import json
    import types

    import flux_quantum.backends.braket as bk

    b = bk.BraketBackend.__new__(bk.BraketBackend)
    b._hold_prefix = "p"
    b._session = None
    b._token_device = None
    b._hold_ready_after = None
    b._hold_started = __import__("time").time()
    b._hold_job = types.SimpleNamespace(state=lambda: "RUNNING")
    b._output_bucket = lambda: "bucket"

    class Body:
        @staticmethod
        def read():
            return json.dumps(token_payload).encode()

    class S3:
        def get_object(self, Bucket, Key):
            return {"Body": Body}

    return b, bk, S3(), {"device": device}


def test_a_token_for_another_device_is_refused():
    """Seb: the device ARN in AwsQuantumJob.create decides which device the
    token is valid for. Using it elsewhere silently gets no priority, so it
    has to fail loudly instead."""
    import types

    b, bk, s3, opts = _holding(
        {
            "token": "tok",
            "device": "arn:aws:braket:::device/quantum-simulator/amazon/sv1",
        },
        "arn:aws:braket:eu-north-1::device/qpu/iqm/Garnet",
    )
    original = bk.boto3
    bk.boto3 = types.SimpleNamespace(client=lambda _: s3)
    try:
        ok, why = b._wait_for_hold_job(opts, 0, lambda _: None)
    finally:
        bk.boto3 = original

    assert not ok
    assert "only valid for the device" in why
    assert b._session is None


def test_a_token_for_the_right_device_is_accepted():
    import types

    garnet = "arn:aws:braket:eu-north-1::device/qpu/iqm/Garnet"
    b, bk, s3, opts = _holding({"token": "tok", "device": garnet}, garnet)
    original = bk.boto3
    bk.boto3 = types.SimpleNamespace(client=lambda _: s3)
    try:
        ok, why = b._wait_for_hold_job(opts, 0, lambda _: None)
    finally:
        bk.boto3 = original

    assert ok and b._session == "tok"


def test_a_token_without_a_device_is_not_second_guessed():
    """An older hold published no device. Refusing on that would break a
    working setup for the sake of a check we cannot make."""
    import types

    b, bk, s3, opts = _holding({"token": "tok"}, "arn:whatever")
    original = bk.boto3
    bk.boto3 = types.SimpleNamespace(client=lambda _: s3)
    try:
        ok, _ = b._wait_for_hold_job(opts, 0, lambda _: None)
    finally:
        bk.boto3 = original
    assert ok and b._session == "tok"


def _priced(price, unit="shot"):
    return type(
        "D",
        (),
        {
            "properties": type(
                "P",
                (),
                {
                    "service": type(
                        "S",
                        (),
                        {
                            "deviceCost": type(
                                "C", (), {"price": price, "unit": unit}
                            )(),
                            "executionWindows": [],
                        },
                    )()
                },
            )()
        },
    )()


def test_cost_is_the_task_fee_plus_shots():
    """Rigetti is $0.00090 a shot on top of the flat $0.30 task fee."""
    from flux_quantum.backends.braket import cost

    assert round(cost(_priced(0.00090), shots=1), 5) == 0.3009
    assert round(cost(_priced(0.00090), shots=100), 4) == 0.39


def test_per_minute_devices_have_no_per_shot_price():
    """Simulators bill for time, so a shot count says nothing about cost."""
    from flux_quantum.backends.braket import cost

    assert cost(_priced(0.075, unit="minute")) is None


def test_a_device_with_no_price_does_not_break_the_survey():
    from flux_quantum.backends.braket import cost

    assert cost(type("D", (), {})()) is None


def _api_bound_client(seen):
    """A Braket client that rejects anything the real API would."""
    import botocore.session

    m = botocore.session.get_session().get_service_model("braket")
    create = m.operation_model("CreateQuantumTask").input_shape
    cancel = m.operation_model("CancelQuantumTask").input_shape

    class Client:
        def create_quantum_task(self, **kw):
            assert not set(create.required_members) - set(kw)
            assert not set(kw) - set(create.members)
            seen.setdefault("tokens", []).append(kw.get("jobToken"))
            seen.setdefault("shots", []).append(kw.get("shots"))
            return {"quantumTaskArn": "arn:task:%d" % len(seen["tokens"])}

        def cancel_quantum_task(self, **kw):
            assert not set(cancel.required_members) - set(kw)
            assert not set(kw) - set(cancel.members)
            seen.setdefault("cancelled", []).append(kw["quantumTaskArn"])

    return Client()


def test_filler_goes_in_without_a_token(braket_sdk):
    """It is there to sit in the Normal queue. A token would put it in
    Priority, which is the queue we are trying to jump."""
    from probe_hold import make_queue

    seen = {}
    arns = make_queue(_api_bound_client(seen), "arn:dev", "bucket", 3)
    assert len(arns) == 3
    assert seen["tokens"] == [None, None, None]


def test_filler_is_cancelled_with_a_client_token(braket_sdk):
    """CancelQuantumTask requires clientToken, unlike most calls. Without it
    the cleanup fails at the moment it matters."""
    from probe_hold import drain_queue, make_queue

    seen = {}
    client = _api_bound_client(seen)
    drain_queue(client, make_queue(client, "arn:dev", "bucket", 2))
    assert len(seen["cancelled"]) == 2


def test_a_cancel_that_fails_does_not_stop_the_rest():
    """A task that already started cannot be cancelled, and the others still
    need clearing."""
    from probe_hold import drain_queue

    done = []

    class Client:
        def cancel_quantum_task(self, quantumTaskArn, clientToken):
            if quantumTaskArn == "arn:b":
                raise RuntimeError("already RUNNING")
            done.append(quantumTaskArn)

    drain_queue(Client(), ["arn:a", "arn:b", "arn:c"])
    assert done == ["arn:a", "arn:c"]


def test_shots_range_comes_from_the_device():
    """Cepheus takes 10 to 50000 and refuses 1. Learning that from a
    ValidationException means the hold is already up and billing."""
    from flux_quantum.backends.braket import shots_range

    d = type(
        "D",
        (),
        {
            "properties": type(
                "P", (), {"service": type("S", (), {"shotsRange": (10, 50000)})()}
            )()
        },
    )()
    assert shots_range(d) == (10, 50000)


def test_a_device_that_does_not_say_is_not_guessed_at():
    from flux_quantum.backends.braket import shots_range

    assert shots_range(type("D", (), {})()) is None


def test_the_device_region_wins_over_the_environment(braket_sdk):
    """A stale AWS_DEFAULT_REGION pointed the session at the wrong region, so
    the bucket polled for the token was the wrong one and the hold looked like
    it never started."""
    import os
    import types

    import flux_quantum.backends.braket as bk

    os.environ["AWS_DEFAULT_REGION"] = "eu-north-1"
    seen = {}

    def fake_create(**kw):
        seen["region"] = os.environ["AWS_DEFAULT_REGION"]
        job = types.SimpleNamespace(arn="arn:job")
        return job

    import braket.aws as aws_mod

    original, original_session = aws_mod.AwsQuantumJob.create, aws_mod.AwsSession
    aws_mod.AwsQuantumJob.create = staticmethod(fake_create)
    aws_mod.AwsSession = lambda: types.SimpleNamespace(
        default_bucket=lambda: "amazon-braket-us-west-1-1"
    )
    try:
        b = bk.BraketBackend.__new__(bk.BraketBackend)
        b._hold_job = b._hold_prefix = b._session = None
        b._made = {"job": None, "bucket": None, "prefix": None}
        b._open_hold_job(
            {"device": "arn:aws:braket:us-west-1::device/qpu/rigetti/Cepheus-1-108Q"}
        )
    finally:
        aws_mod.AwsQuantumJob.create = original
        aws_mod.AwsSession = original_session
        del os.environ["AWS_DEFAULT_REGION"]

    assert seen["region"] == "us-west-1"


def test_a_completed_task_is_not_reported_as_a_cancel_failure():
    """On a fast device every filler task has already run, and four lines of
    ConflictException are noise."""
    import probe_hold

    said = []

    class Client:
        def cancel_quantum_task(self, quantumTaskArn, clientToken):
            raise RuntimeError(
                "ConflictException: cannot cancel a quantum task in the "
                "COMPLETED status"
            )

    import builtins

    real_print = builtins.print
    builtins.print = lambda *a, **k: said.append(" ".join(str(x) for x in a))
    try:
        probe_hold.drain_queue(Client(), ["arn:a"])
    finally:
        builtins.print = real_print
    assert said == []


def test_filler_shots_are_separate_from_probe_shots(braket_sdk):
    """They want opposite things. Filler needs shots to occupy the device,
    probe tasks want few so the run stays cheap. Ten shot filler finished
    before the queue could be read, twice."""
    from probe_hold import make_queue

    seen = {}
    client = _api_bound_client(seen)
    make_queue(client, "arn:dev", "bucket", 2, 500)
    assert seen["shots"] == [500, 500]


def test_calibration_summarises_rather_than_dumps():
    """What a result has to be read against is which edges were usable and
    when they were characterised, not a thousand lines of specs."""
    from probe_hold import calibration

    specs = {
        "instructions": [
            {
                "node_ids": [88, 89],
                "characteristics": [
                    {
                        "name": "fCZ",
                        "value": 0.9967,
                        "timestamp": "2026-09-28T15:21:58+00:00",
                    }
                ],
            },
            {
                "node_ids": [97, 106],
                "characteristics": [
                    {
                        "name": "fCZ",
                        "value": 0.8857,
                        "timestamp": "2026-09-28T15:41:47+00:00",
                    }
                ],
            },
            {
                "node_ids": [64, 73],
                "characteristics": [
                    {
                        "name": "fCZ",
                        "value": 0.5,
                        "timestamp": "2026-09-28T15:41:46+00:00",
                    }
                ],
            },
        ]
    }
    d = type(
        "D",
        (),
        {
            "properties": type(
                "P", (), {"provider": type("V", (), {"specs": specs})()}
            )()
        },
    )()
    c = calibration(d)
    assert c["live"] == 2 and c["dead"] == 1
    assert round(c["error_min"], 4) == 0.0033
    assert round(c["error_max"], 4) == 0.1143
    assert c["calibrated_from"].endswith("15:21:58+00:00")


def test_the_record_never_holds_the_token():
    """It grants priority and bills to the job. A log is not the place."""
    import json
    import tempfile

    from probe_hold import record

    path = tempfile.mktemp(suffix=".jsonl")
    row = {"device": "arn:dev", "hold_job": "arn:job", "verdict": ["ASSOCIATED"]}
    record(path, row)
    record(path, row)
    lines = open(path).read().strip().split("\n")
    assert len(lines) == 2
    assert "token" not in json.loads(lines[0])


def test_a_calibration_we_cannot_read_is_not_invented():
    from probe_hold import calibration

    assert calibration(type("D", (), {})()) is None


def test_used_qubits_does_not_block_on_an_unfinished_task():
    """result() polls until the task completes, five days by default. On a
    queued task that is a hang, and it hung a live run."""
    import probe_hold

    class Task:
        def __init__(self, arn):
            pass

        def state(self):
            return "QUEUED"

        def result(self):
            raise AssertionError("must not be called on an unfinished task")

    import braket.aws as aws

    original = aws.AwsQuantumTask
    aws.AwsQuantumTask = Task
    try:
        got = probe_hold.used_qubits("arn:task")
    finally:
        aws.AwsQuantumTask = original
    assert got["state"] == "QUEUED"


def test_every_device_gets_its_windows():
    """A device open and empty now says nothing about whether it is worth
    coming back to, so all of them are listed."""
    import builtins

    import probe_hold

    rows = [
        {
            "arn": "arn:open",
            "open": True,
            "normal": 0,
            "priority": 0,
            "cost": 0.3,
            "windows": [("Everyday", "02:00", "12:00")],
        },
        {
            "arn": "arn:shut",
            "open": False,
            "normal": 60,
            "priority": 0,
            "cost": 0.3,
            "windows": [("Monday", "18:00", "20:59")],
        },
    ]
    real, probe_hold.survey = probe_hold.survey, lambda shots=1: rows
    said = []
    rp, builtins.print = builtins.print, lambda *a, **k: said.append(
        " ".join(str(x) for x in a)
    )
    try:
        probe_hold.show_survey()
    finally:
        builtins.print = rp
        probe_hold.survey = real

    out = "\n".join(said)
    assert "arn:open  (0 queued, open)" in out
    assert "arn:shut  (60 queued, shut)" in out
    assert "02:00 to 12:00" in out and "18:00 to 20:59" in out


# ---------------------------------------------------------------------------
# the probe reads the queue from the task, not from the device counters


def test_the_poll_asks_for_queue_info():
    """queueInfo only comes back when GetQuantumTask is asked for it. Every
    run before this one polled without asking, saw no queue, no position and
    no priority, and fell back to device counters that lag by minutes."""
    from probe_hold import describe

    asked = {}

    class Client:
        def get_quantum_task(self, **kw):
            asked.update(kw)
            return {"status": "QUEUED"}

    describe(Client(), "arn:t")
    assert asked == {
        "quantumTaskArn": "arn:t",
        "additionalAttributeNames": ["QueueInfo"],
    }


def test_a_queued_task_reports_its_queue():
    from probe_hold import describe

    class Client:
        def get_quantum_task(self, **kw):
            return {
                "status": "QUEUED",
                "jobArn": "arn:job",
                "queueInfo": {
                    "queue": "QUANTUM_TASKS_QUEUE",
                    "position": "1",
                    "queuePriority": "Priority",
                },
            }

    seen = describe(Client(), "arn:t")
    assert (seen["priority"], seen["position"], seen["job"]) == (
        "Priority",
        "1",
        "arn:job",
    )


def test_a_position_of_none_is_none():
    """The service spells a missing position as the string None."""
    from probe_hold import describe

    class Client:
        def get_quantum_task(self, **kw):
            return {"status": "RUNNING", "queueInfo": {"position": "None"}}

    assert describe(Client(), "arn:t")["position"] is None


class _Ticker:
    """A clock and a sleep that agree, so follow can be run without waiting."""

    def __init__(self):
        self.t = 1000.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_follow_keeps_the_first_queue_sighting():
    """The queue fields describe a waiting task and are gone once it runs.
    The first sighting is the task's queue, the last position is where it
    was when it left, and the timeline holds every change in between."""
    import probe_hold

    polls = [
        {
            "status": "QUEUED",
            "jobArn": "arn:job",
            "queueInfo": {"queuePriority": "Priority", "position": "2"},
        },
        {
            "status": "QUEUED",
            "jobArn": "arn:job",
            "queueInfo": {"queuePriority": "Priority", "position": "1"},
        },
        {"status": "RUNNING", "jobArn": "arn:job"},
        {"status": "COMPLETED", "jobArn": "arn:job", "endedAt": "later"},
    ]

    class Client:
        def get_quantum_task(self, **kw):
            return polls.pop(0)

    tick = _Ticker()
    v = probe_hold.follow(Client(), ["t1"], sleep=tick.sleep, clock=tick.clock)["t1"]
    assert v["priority"] == "Priority"
    assert v["position"] == "2"
    assert v["position_last"] == "1"
    assert v["status"] == "COMPLETED"
    assert v["ended"] == "later"
    assert [row[1:] for row in v["timeline"]] == [
        ["QUEUED", "Priority", "2"],
        ["QUEUED", "Priority", "1"],
        ["RUNNING", None, None],
        ["COMPLETED", None, None],
    ]


def test_follow_gives_up_rather_than_hanging():
    """The old poll gave up after a fixed three minutes whatever the hold
    was, and released a hold with both tasks still queued. Now the caller
    sets the budget, and it is honoured."""
    import probe_hold

    class Stuck:
        def get_quantum_task(self, **kw):
            return {"status": "QUEUED", "jobArn": "arn:job"}

    tick = _Ticker()
    v = probe_hold.follow(
        Stuck(), ["t1"], timeout=60, interval=5, sleep=tick.sleep, clock=tick.clock
    )
    assert v["t1"]["status"] == "QUEUED"
    assert tick.t - 1000 <= 60


def test_follow_stops_when_the_token_task_is_done():
    """The control can wait all afternoon. The hold only has to stay up
    until the token task has finished, so that is the stop."""
    import probe_hold

    class Client:
        def __init__(self):
            self.n = 0

        def get_quantum_task(self, quantumTaskArn, **kw):
            self.n += 1
            if quantumTaskArn == "token":
                return {"status": "COMPLETED" if self.n > 2 else "QUEUED"}
            return {
                "status": "QUEUED",
                "queueInfo": {"queuePriority": "Normal", "position": "4"},
            }

    tick = _Ticker()
    v = probe_hold.follow(
        Client(),
        ["control", "token"],
        sleep=tick.sleep,
        clock=tick.clock,
        done=lambda v: v["token"]["status"] in probe_hold.TERMINAL,
    )
    assert v["token"]["status"] == "COMPLETED"
    assert v["control"]["status"] == "QUEUED"
    assert v["control"]["position_last"] == "4"


def test_follow_resumes_without_re_polling_finished_tasks():
    import probe_hold

    asked = []

    class Client:
        def get_quantum_task(self, quantumTaskArn, **kw):
            asked.append(quantumTaskArn)
            return {"status": "COMPLETED"}

    tick = _Ticker()
    views = {"done": {"status": "COMPLETED", "timeline": []}}
    probe_hold.follow(
        Client(), ["done", "other"], sleep=tick.sleep, clock=tick.clock, views=views
    )
    assert asked == ["other"]
    assert views["other"]["status"] == "COMPLETED"


def _at(seconds):
    import datetime

    return datetime.datetime(2026, 9, 29, 0, 0, 0, tzinfo=datetime.timezone.utc) + (
        datetime.timedelta(seconds=seconds)
    )


def test_the_verdict_reads_the_queue_from_the_task():
    """Priority against Normal, read from the tasks themselves, and the
    token task submitted after the control but finished before it."""
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {
                "job": "arn:job",
                "priority": "Priority",
                "position": "1",
                "created": _at(5),
                "ended": _at(100),
            },
            {
                "priority": "Normal",
                "position": "3",
                "created": _at(0),
                "ended": _at(400),
            },
            "qpu",
        )
    )
    assert "ASSOCIATED" in out
    assert "IN THE PRIORITY QUEUE" in out
    assert "Priority at position 1" in out and "Normal at position 3" in out
    assert "SERVED FIRST" in out
    assert "5s after the control" in out and "300s before it" in out
    assert "NOT PRIORITISED" not in out


def test_a_token_in_the_normal_queue_is_not_prioritised():
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {"job": "arn:job", "priority": "Normal", "position": "4"},
            {"priority": "Normal", "position": "3"},
            "qpu",
        )
    )
    assert "NOT PRIORITISED" in out


def test_finishing_in_order_buys_no_place():
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {
                "job": "arn:job",
                "priority": "Priority",
                "created": _at(1),
                "ended": _at(500),
            },
            {"priority": "Normal", "created": _at(0), "ended": _at(400)},
            "qpu",
        )
    )
    assert "SERVED IN ORDER" in out and "100s after it" in out


def test_a_control_still_queued_when_the_token_finished_counts():
    """The control may sit for an hour. The token task finishing while the
    control is still in line is the ordering, without waiting for the rest."""
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {
                "job": "arn:job",
                "priority": "Priority",
                "created": _at(1),
                "ended": _at(200),
            },
            {
                "priority": "Normal",
                "status": "QUEUED",
                "position_last": "2",
                "created": _at(0),
            },
            "qpu",
        )
    )
    assert "SERVED FIRST" in out
    assert "still QUEUED at position 2" in out


def test_a_token_task_that_did_not_finish_says_so():
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {
                "job": "arn:job",
                "priority": "Priority",
                "status": "QUEUED",
                "created": _at(1),
            },
            {"priority": "Normal", "created": _at(0)},
            "qpu",
        )
    )
    assert "did not finish" in out and "max-seconds" in out


def test_the_token_task_has_to_go_in_last():
    """Finishing first after being submitted first proves nothing."""
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {
                "job": "arn:job",
                "priority": "Priority",
                "created": _at(0),
                "ended": _at(10),
            },
            {"priority": "Normal", "created": _at(5), "ended": _at(20)},
            "qpu",
        )
    )
    assert "WARNING" in out and "created before the control" in out
    assert "SERVED FIRST" not in out


def test_nothing_waiting_is_said_once():
    """The ordinary simulator case: neither task was ever seen in a queue,
    so there is no queue to name and no order worth claiming."""
    from probe_hold import SV1, verdict

    out = verdict(
        {"job": "arn:job", "created": _at(1), "ended": _at(2)},
        {"created": _at(0), "ended": _at(3)},
        SV1,
    )
    assert len(out) == 2
    assert "cannot be seen" in out[1]


def test_the_estimate_covers_both_tasks_and_the_hold():
    """Read before anything is created, because the token task and the
    control both bill per shot and the hold bills by the minute."""
    from probe_hold import estimate

    d = type(
        "D",
        (),
        {
            "properties": type(
                "P",
                (),
                {
                    "service": type(
                        "S",
                        (),
                        {
                            "deviceCost": type(
                                "C", (), {"price": 0.08, "unit": "shot"}
                            )()
                        },
                    )()
                },
            )()
        },
    )()
    # two tasks at 0.30 + 100 * 0.08, plus half an hour of the hold instance
    assert abs(estimate(d, 100, 0, 500, 1800) - (2 * 8.30 + 0.115 / 2)) < 1e-6
    assert estimate(type("D", (), {})(), 100, 0, 500, 1800) is None


def test_a_cancelled_control_is_not_a_finish():
    """--cancel-control ends the control with an end time, but it never
    ran. Reading that as finished would turn a cancel into a race result."""
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {
                "job": "arn:job",
                "priority": "Priority",
                "status": "COMPLETED",
                "created": _at(1),
                "ended": _at(200),
            },
            {
                "priority": "Normal",
                "status": "CANCELLED",
                "position_last": "2",
                "created": _at(0),
                "ended": _at(230),
            },
            "qpu",
        )
    )
    assert "SERVED FIRST" in out
    assert "cancelled afterwards" in out and "at position 2" in out
    assert "before it" not in out


def test_a_failed_token_task_is_not_a_race_result():
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {
                "job": "arn:job",
                "priority": "Priority",
                "status": "FAILED",
                "created": _at(1),
                "ended": _at(5),
            },
            {
                "priority": "Normal",
                "status": "COMPLETED",
                "created": _at(0),
                "ended": _at(400),
            },
            "qpu",
        )
    )
    assert "FAILED" in out and "never ran" in out
    assert "SERVED" not in out


class _SimulatedDevice:
    """A Braket client over a device that serves one task every `each`
    seconds, Priority tasks first. Creating a task takes a second, as it
    does for real, so the first filler is already running when the token
    task arrives and the token task queues at Priority position 1."""

    def __init__(self, clock, each=30):
        self.clock = clock
        self.each = each
        self.tasks = {}
        self.n = 0

    def _now(self):
        return self.clock["t"]

    def create_quantum_task(self, **kw):
        self.n += 1
        arn = "arn:aws:braket:us-west-1:1:quantum-task/%d" % self.n
        self.tasks[arn] = {
            "created": self._now(),
            "priority": bool(kw.get("jobToken")),
            "job": "arn:job" if kw.get("jobToken") else None,
            "cancelled": None,
        }
        self.clock["t"] += 1
        return {"quantumTaskArn": arn}

    def _schedule(self, skip=()):
        """When each task starts, back to back, priority first."""
        free = min(t["created"] for t in self.tasks.values())
        left = {a: t for a, t in self.tasks.items() if a not in skip}
        starts = {}
        while left:
            ready = [a for a, t in left.items() if t["created"] <= free]
            if not ready:
                free = min(t["created"] for t in left.values())
                continue
            ready.sort(key=lambda a: (not left[a]["priority"], left[a]["created"]))
            starts[ready[0]] = free
            free += self.each
            del left[ready[0]]
        return starts

    def _view(self, arn):
        starts = self._schedule()
        gone = [
            a
            for a, t in self.tasks.items()
            if t["cancelled"] is not None and starts[a] >= t["cancelled"]
        ]
        starts = self._schedule(skip=gone)
        t = self.tasks[arn]
        if arn in gone:
            return "CANCELLED", None, t["cancelled"]
        start = starts[arn]
        now = self._now()
        if now >= start + self.each:
            return "COMPLETED", None, start + self.each
        if now >= start:
            return "RUNNING", None, None
        ahead = [
            a
            for a, s in starts.items()
            if a not in gone
            and self.tasks[a]["priority"] == t["priority"]
            and now < s < start
        ]
        return "QUEUED", str(len(ahead) + 1), None

    def get_quantum_task(self, quantumTaskArn, additionalAttributeNames=None):
        import datetime

        assert additionalAttributeNames == ["QueueInfo"], "the poll must ask"
        t = self.tasks[quantumTaskArn]
        status, position, ended = self._view(quantumTaskArn)
        stamp = lambda x: datetime.datetime.fromtimestamp(x, datetime.timezone.utc)
        out = {"status": status, "jobArn": t["job"], "createdAt": stamp(t["created"])}
        if ended:
            out["endedAt"] = stamp(ended)
        if status == "QUEUED":
            out["queueInfo"] = {
                "queue": "QUANTUM_TASKS_QUEUE",
                "position": position,
                "queuePriority": "Priority" if t["priority"] else "Normal",
            }
        return out

    def cancel_quantum_task(self, quantumTaskArn, clientToken):
        status, _, _ = self._view(quantumTaskArn)
        if status != "QUEUED":
            raise RuntimeError(
                "ConflictException: cannot cancel a task in the %s status" % status
            )
        self.tasks[quantumTaskArn]["cancelled"] = self._now()


def test_the_whole_run_against_a_simulated_device():
    """main, end to end, the way it will be run on Cepheus: six filler
    tasks, a control, then the token task. Nothing here touches AWS, and
    nothing sleeps, because the clock is ours."""
    import json
    import os
    import sys
    import tempfile
    import types

    import braket.aws as aws_mod

    import probe_hold

    clock = {"t": 1000.0}
    fake_time = types.SimpleNamespace(
        time=lambda: clock["t"],
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
    )
    device = _SimulatedDevice(clock)

    class FakeBackend:
        credential_note = "fake credentials"
        closed = 0

        def __init__(self):
            self._hold_job = types.SimpleNamespace(state=lambda: "RUNNING")
            self._hold_ready_after = None

        def open_session(self, opts):
            assert opts["hold"] == "job" and opts["hold_max_seconds"] == 900
            return "arn:job"

        def wait_for_priority(self, opts):
            clock["t"] += 100
            self._hold_ready_after = 100
            return True, "holding the device after 100s"

        def session_id(self, arn):
            return "tok-secret-0123456789abcdef"

        def close_session(self):
            FakeBackend.closed += 1

    class FakeDevice:
        def __init__(self, arn):
            self.properties = type(
                "P",
                (),
                {
                    "service": type(
                        "S",
                        (),
                        {
                            "executionWindows": ALWAYS,
                            "shotsRange": (10, 50000),
                            "deviceCost": type(
                                "C", (), {"price": 0.000425, "unit": "shot"}
                            )(),
                        },
                    )(),
                    "provider": type("V", (), {"specs": {}})(),
                },
            )()

    tmp = tempfile.mkdtemp()
    rec, log = os.path.join(tmp, "runs.jsonl"), os.path.join(tmp, "probe.log")
    saved = (
        probe_hold.time,
        probe_hold.BraketBackend,
        probe_hold.boto3,
        probe_hold.AwsSession,
        probe_hold.queue_depth,
        aws_mod.AwsDevice,
        sys.argv,
        sys.stdout,
        sys.stderr,
        os.environ.get("AWS_DEFAULT_REGION"),
    )
    probe_hold.time = fake_time
    probe_hold.BraketBackend = FakeBackend
    probe_hold.boto3 = types.SimpleNamespace(client=lambda name: device)
    probe_hold.AwsSession = lambda: types.SimpleNamespace(
        default_bucket=lambda: "bucket"
    )
    probe_hold.queue_depth = lambda d: {
        "normal": 0,
        "priority": 0,
        "jobs": 0,
        "jobs_note": "0",
    }
    aws_mod.AwsDevice = FakeDevice
    sys.argv = [
        "probe_hold.py",
        "--device",
        "arn:aws:braket:us-west-1::device/qpu/rigetti/Cepheus-1-108Q",
        "--shots",
        "10",
        "--make-queue",
        "6",
        "--filler-shots",
        "500",
        "--max-seconds",
        "900",
        "--record",
        rec,
        "--log",
        log,
    ]
    try:
        probe_hold.main()
    finally:
        (
            probe_hold.time,
            probe_hold.BraketBackend,
            probe_hold.boto3,
            probe_hold.AwsSession,
            probe_hold.queue_depth,
            aws_mod.AwsDevice,
            sys.argv,
            sys.stdout,
            sys.stderr,
            region,
        ) = saved
        if region is None:
            os.environ.pop("AWS_DEFAULT_REGION", None)
        else:
            os.environ["AWS_DEFAULT_REGION"] = region

    run = json.loads(open(rec).read().strip().splitlines()[-1])
    text = "\n".join(run["verdict"])
    assert "ASSOCIATED" in text
    assert "Priority at position 1" in text
    assert "Normal at position 6" in text
    assert "SERVED FIRST" in text and "180s before it" in text
    assert run["with_token"]["job"] == "arn:job" and not run["without"].get("job")
    # the filler is part of the measurement, so its ARNs are on record too.
    # Forte's went unrecorded and its finish time could not be read back
    assert len(run["filler_arns"]) == 6
    assert run["with_token"]["status"] == "COMPLETED"
    assert run["without"]["status"] == "COMPLETED"
    # the hold is released once, as soon as the token task is done
    assert FakeBackend.closed == 1

    logged = open(log).read()
    assert "hybrid job: arn:job" in logged
    assert "this run costs about $3." in logged
    assert "Priority queue position 1" in logged
    assert "SERVED FIRST" in logged
    assert "recorded to" in logged
    # the token is not written down. The prefix printed is not the token
    assert "tok-secret-0123456789abcdef" not in logged
    assert "tok-secret-0" in logged


def test_log_to_tees_and_headers():
    import io
    import os
    import sys
    import tempfile

    import probe_hold

    path = os.path.join(tempfile.mkdtemp(), "x.log")
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        probe_hold.log_to(path, argv=["probe_hold.py", "--survey"])
        print("hello")
        print("oops", file=sys.stderr)
    finally:
        sys.stdout, sys.stderr = saved
    logged = open(path).read()
    assert "=== " in logged and "probe_hold.py --survey" in logged
    assert "hello\n" in logged and "oops\n" in logged
    assert "hello" in out.getvalue() and "oops" in err.getvalue()


def test_labels_without_positions_mean_nobody_waited():
    """Cepheus, 01:03 on the 29th: both tasks finished 450ms after creation
    and Braket still labelled one Priority and one Normal. The labels are
    real, but finishing in submission order on an idle device is not a
    result, and the old wording called it one."""
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {
                "job": "arn:job",
                "status": "COMPLETED",
                "priority": "Priority",
                "created": _at(0.35),
                "ended": _at(0.81),
            },
            {
                "status": "COMPLETED",
                "priority": "Normal",
                "created": _at(0.0),
                "ended": _at(0.46),
            },
            "qpu",
        )
    )
    assert "IN THE PRIORITY QUEUE" in out
    assert "NO CONTENTION" in out and "ran in 0.5s" in out
    assert "SERVED" not in out and "bought no place" not in out


def test_quera_gets_an_analog_program_and_everyone_else_a_circuit():
    """Aquila runs Hamiltonians and refuses a circuit. It is also the one
    device with a queue that does not drain the moment it opens."""
    import json

    import probe_hold

    real = probe_hold.ahs_action
    probe_hold.ahs_action = lambda: "analog"
    try:
        assert (
            probe_hold.program_action(
                "arn:aws:braket:us-east-1::device/qpu/quera/Aquila"
            )
            == "analog"
        )
        circuit = json.loads(
            probe_hold.program_action(
                "arn:aws:braket:us-west-1::device/qpu/rigetti/Cepheus-1-108Q"
            )
        )
    finally:
        probe_hold.ahs_action = real
    assert circuit["braketSchemaHeader"]["name"] == "braket.ir.openqasm.program"
    assert "measure" in circuit["source"]


def test_submit_sends_the_device_program():
    import probe_hold

    sent = {}

    class Client:
        def create_quantum_task(self, **kw):
            sent.update(kw)
            return {"quantumTaskArn": "arn:t"}

    real = probe_hold.ahs_action
    probe_hold.ahs_action = lambda: "analog"
    try:
        probe_hold.submit(
            Client(),
            "arn:aws:braket:us-east-1::device/qpu/quera/Aquila",
            "b",
            "tok",
            10,
        )
    finally:
        probe_hold.ahs_action = real
    assert (
        sent["action"] == "analog" and sent["jobToken"] == "tok" and sent["shots"] == 10
    )


def test_inspect_reads_old_tasks_back():
    """Free. Two tasks from the Forte run sat five minutes behind two others,
    and how long they finally took is the number that says whether Forte
    can hold a queue."""
    import builtins

    import probe_hold

    class Client:
        def get_quantum_task(self, quantumTaskArn, additionalAttributeNames):
            assert additionalAttributeNames == ["QueueInfo"]
            return {
                "status": "COMPLETED",
                "createdAt": _at(0),
                "endedAt": _at(400),
                "queueInfo": {
                    "queuePriority": (
                        "Priority" if quantumTaskArn.endswith("/a") else "Normal"
                    )
                },
            }

    said = []
    real = builtins.print
    builtins.print = lambda *a, **k: said.append(" ".join(str(x) for x in a))
    try:
        rows = probe_hold.inspect(Client(), ["arn:x/a", "arn:x/b"])
    finally:
        builtins.print = real
    assert [r["priority"] for r in rows] == ["Priority", "Normal"]
    assert any("400s" in line and "Priority" in line for line in said)


def test_pace_reports_a_device_that_serves_one_task_at_a_time():
    """Filler only builds a queue on a device like this. The simulated
    device takes 30s a task, so three tasks span about 90s."""
    import probe_hold

    clock = {"t": 1000.0}
    device = _SimulatedDevice(clock)
    tick = lambda s: clock.__setitem__("t", clock["t"] + s)
    rows = probe_hold.pace(
        device, "arn:dev", "bucket", 3, 10, sleep=tick, clock=lambda: clock["t"]
    )
    assert [r["status"] for r in rows] == ["COMPLETED"] * 3
    assert rows[1]["position"] == "1" and rows[2]["position"] == "2"
    text = "\n".join(probe_hold.pace_report(rows))
    assert "SERIAL" in text and "filler will hold" in text


def test_pace_reports_a_device_that_runs_everything_at_once():
    """Cepheus: eight tasks, each done 450ms after creation, overlapping."""
    import probe_hold

    rows = [
        {
            "arn": "arn:x/%d" % i,
            "status": "COMPLETED",
            "created": _at(i * 0.3),
            "ended": _at(i * 0.3 + 0.45),
            "took": 0.45,
            "position": None,
        }
        for i in range(3)
    ]
    text = "\n".join(probe_hold.pace_report(rows))
    assert "OVERLAPPING" in text and "will not build a queue" in text


def test_a_long_wait_says_it_is_still_waiting():
    """Forte prints nothing for minutes while a task runs, which reads as a
    hang. A line a minute says otherwise, and what the queue looks like."""
    import builtins

    import probe_hold

    class Stuck:
        def get_quantum_task(self, **kw):
            return {
                "status": "QUEUED",
                "queueInfo": {"queuePriority": "Priority", "position": "1"},
            }

    said = []
    real = builtins.print
    builtins.print = lambda *a, **k: said.append(" ".join(str(x) for x in a))
    tick = _Ticker()
    try:
        probe_hold.follow(
            Stuck(),
            ["arn:x/token"],
            timeout=200,
            interval=5,
            sleep=tick.sleep,
            clock=tick.clock,
            say=lambda a, s: None,
        )
    finally:
        builtins.print = real
    beats = [x for x in said if "still waiting" in x]
    assert 2 <= len(beats) <= 4
    assert "QUEUED at 1" in beats[0]
