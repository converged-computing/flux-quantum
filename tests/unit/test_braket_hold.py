"""The hybrid job hold, tested without AWS.

A running hybrid job holds the priority queue for its device and publishes
AMZN_BRAKET_JOB_TOKEN. Work submitted anywhere with that token gets the job's
priority. Without it the docs say it gets none and bills standalone. So the
token, not the job ARN, is what the classical job has to be handed.
"""

import json


def test_entry_publishes_the_token_then_waits_for_release():
    from flux_quantum.backends.hold import entry

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
    from flux_quantum.backends.hold import entry

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


def test_scout_options_carry_the_hold_mode():

    class Args:
        braket_device = None
        braket_region = None
        braket_shots = None
        braket_queue_timeout = None
        braket_ungate_position = None
        braket_hold = "job"
        braket_hold_instance = None
        braket_hold_max_seconds = None

    opts = _backend().scout_options(Args())
    assert opts["hold"] == "job"
    # the cheapest instance, since it does no work
    assert opts["hold_instance"] == "ml.m5.large"
    assert opts["hold_max_seconds"] == 900


def test_probe_stays_the_default():
    """The older mode keeps working for anyone not asking for a hybrid job."""

    class Args:
        braket_device = None
        braket_region = None
        braket_shots = None
        braket_queue_timeout = None
        braket_ungate_position = None

    assert _backend().scout_options(Args())["hold"] == "probe"


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


def test_the_verdict_survives_a_task_that_finishes_instantly():
    """jobArn is set at creation and is still there afterwards, so the check
    never has to win a race. A one qubit task on a simulator can finish before
    the first poll returns."""
    import probe_hold

    class Instant:
        def get_quantum_task(self, quantumTaskArn):
            return {
                "status": "COMPLETED",
                "jobArn": "arn:job" if quantumTaskArn == "t1" else None,
            }

    seen = probe_hold.settle(Instant(), ["t1", "t2"], sleep=lambda _: None)
    assert seen["t1"]["job"] == "arn:job"
    assert not seen["t2"].get("job")


def test_a_queue_glimpse_is_kept_but_not_required():
    """queueInfo describes a waiting task and is gone once it runs. Record it
    if it shows up, never depend on it."""
    import probe_hold

    class Fleeting:
        def __init__(self):
            self.n = 0

        def get_quantum_task(self, quantumTaskArn):
            self.n += 1
            if self.n <= 2:
                return {
                    "status": "QUEUED",
                    "jobArn": "arn:job",
                    "queueInfo": {"queuePriority": "Priority", "position": "1"},
                }
            return {"status": "COMPLETED", "jobArn": "arn:job"}

    seen = probe_hold.settle(Fleeting(), ["t1"], sleep=lambda _: None)
    assert seen["t1"]["priority"] == "Priority"
    assert seen["t1"]["status"] == "COMPLETED"


def test_settle_gives_up_rather_than_hanging():
    import probe_hold

    class Stuck:
        def get_quantum_task(self, quantumTaskArn):
            return {"status": "QUEUED", "jobArn": "arn:job"}

    calls = {"n": 0}

    def sleep(_):
        calls["n"] += 1
        if calls["n"] > 500:
            raise AssertionError("did not respect the timeout")

    seen = probe_hold.settle(Stuck(), ["t1"], timeout=0, sleep=sleep)
    assert seen["t1"] == {}


def test_verdict_handles_a_queue_that_was_never_visible():
    """The ordinary simulator case. settle only records fields it saw, so
    reading priority unconditionally raised KeyError on the very run this
    probe is for."""
    from probe_hold import SV1, verdict

    out = "\n".join(verdict({"job": "arn:job"}, {}, SV1))
    assert "ASSOCIATED" in out
    assert "cannot be seen" in out


def test_verdict_reports_priority_when_both_were_seen():
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {"job": "arn:job", "priority": "Priority"}, {"priority": "Normal"}, "qpu"
        )
    )
    assert "PRIORITISED" in out


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


def test_queue_depth_splits_normal_from_priority():
    from flux_quantum.backends.braket import queue_depth

    _FakeDevice.fleet = {"arn:aws:braket:::device/qpu/x/y": (7, 3)}
    q = queue_depth("arn:aws:braket:::device/qpu/x/y", cls=_FakeDevice)
    assert (q["normal"], q["priority"], q["jobs"]) == (7, 3, 0)


def test_only_qpus_with_work_waiting_are_offered():
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


def test_priority_is_read_from_which_queue_grew():
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {"job": "arn:job"},
            {},
            "qpu",
            {"normal": 0, "priority": 1},
            {"normal": 1, "priority": 0},
        )
    )
    assert "PRIORITISED" in out and "NOT PRIORITISED" not in out


def test_a_token_that_queues_normally_is_called_out():
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {"job": "arn:job"},
            {},
            "qpu",
            {"normal": 1, "priority": 0},
            {"normal": 1, "priority": 0},
        )
    )
    assert "NOT PRIORITISED" in out


def test_an_idle_device_gives_one_verdict_not_two():
    from probe_hold import SV1, verdict

    out = verdict(
        {"job": "arn:job"},
        {},
        SV1,
        {"normal": 0, "priority": 0},
        {"normal": 0, "priority": 0},
    )
    assert len([x for x in out if "PRIORITIS" in x or "cannot be seen" in x]) == 1


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


def test_survey_puts_open_devices_first_and_skips_shut_ones():
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


def test_queue_depth_survives_the_note_braket_appends():
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


def test_moved_ignores_the_note():
    from probe_hold import moved

    before = {"normal": 1, "priority": 0, "jobs": 0, "jobs_note": "0 (1 running)"}
    after = {"normal": 1, "priority": 1, "jobs": 0, "jobs_note": "0 (1 running)"}
    assert moved(before, after) == {"normal": 0, "priority": 1, "jobs": 0}


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


def test_filler_goes_in_without_a_token():
    """It is there to sit in the Normal queue. A token would put it in
    Priority, which is the queue we are trying to jump."""
    from probe_hold import make_queue

    seen = {}
    arns = make_queue(_api_bound_client(seen), "arn:dev", "bucket", 3)
    assert len(arns) == 3
    assert seen["tokens"] == [None, None, None]


def test_filler_is_cancelled_with_a_client_token():
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


def test_the_device_region_wins_over_the_environment():
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


def test_wait_for_queue_returns_as_soon_as_the_filler_registers():
    """Submitting returns before the service counts the task, so reading the
    depth straight after can miss it."""
    import probe_hold

    depths = [
        {"normal": 0, "priority": 0},
        {"normal": 0, "priority": 0},
        {"normal": 3, "priority": 0},
    ]
    probe_hold.queue_depth = lambda d: depths.pop(0) if depths else {"normal": 3}
    try:
        assert probe_hold.wait_for_queue("arn:dev", sleep=lambda _: None)["normal"] == 3
    finally:
        from flux_quantum.backends.braket import queue_depth

        probe_hold.queue_depth = queue_depth


def test_wait_for_queue_gives_up_on_a_device_too_fast_to_queue():
    """Cepheus ran four ten shot tasks before the hold was ready. Waiting
    forever for a queue that will never form helps nobody."""
    import probe_hold

    probe_hold.queue_depth = lambda d: {"normal": 0, "priority": 0}
    calls = {"n": 0}

    def sleep(_):
        calls["n"] += 1
        if calls["n"] > 200:
            raise AssertionError("did not respect the timeout")

    try:
        assert (
            probe_hold.wait_for_queue("arn:dev", timeout=0, sleep=sleep)["normal"] == 0
        )
    finally:
        from flux_quantum.backends.braket import queue_depth

        probe_hold.queue_depth = queue_depth


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


def test_filler_shots_are_separate_from_probe_shots():
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


def test_a_queue_that_drained_during_the_hold_says_so():
    """Seen four times: the device has a backlog when the run starts and none
    by the time the hold is up. That is not the same as never having one."""
    from probe_hold import verdict

    out = "\n".join(
        verdict({"job": "j"}, {}, "qpu", {"normal": -7, "priority": 0}, {"normal": 0})
    )
    assert "lost 7 tasks while the hold was provisioning" in out
    assert "Nothing was waiting" not in out


def test_an_idle_device_still_reads_as_idle():
    from probe_hold import verdict

    out = "\n".join(
        verdict({"job": "j"}, {}, "qpu", {"normal": 0, "priority": 0}, {"normal": 0})
    )
    assert "Nothing was waiting" in out
    assert "provisioning" not in out


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


def test_a_task_sitting_in_priority_is_the_plainest_evidence():
    """Priority read zero on every survey of these devices. One task in it
    after we submitted exactly one token task is the answer, and movement
    between two laggy counts is not."""
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {"job": "j"},
            {},
            "qpu",
            {"normal": 6, "priority": 0},
            {"normal": 0},
            after_token={"normal": 8, "priority": 1},
        )
    )
    assert "IN THE PRIORITY QUEUE" in out
    assert "NOT PRIORITISED" not in out


def test_an_empty_priority_queue_falls_back_to_movement():
    from probe_hold import verdict

    out = "\n".join(
        verdict(
            {"job": "j"},
            {},
            "qpu",
            {"normal": 1, "priority": 0},
            {"normal": 1},
            after_token={"normal": 8, "priority": 0},
        )
    )
    assert "NOT PRIORITISED" in out


def test_a_reading_is_taken_only_once_it_stops_moving():
    """The counters lag a submit. Six filler tasks read as one, then seven a
    moment later, and a token task's arrival in Priority was credited to the
    filler catching up."""
    import probe_hold

    readings = [
        {"normal": 1, "priority": 0},
        {"normal": 7, "priority": 0},
        {"normal": 7, "priority": 1},
        {"normal": 7, "priority": 1},
    ]
    real, probe_hold.queue_depth = probe_hold.queue_depth, lambda d: readings.pop(0)
    try:
        got = probe_hold.settled_depth("arn:dev", sleep=lambda _: None)
    finally:
        probe_hold.queue_depth = real
    assert got == {"normal": 7, "priority": 1}


def test_settling_gives_up_rather_than_waiting_forever():
    import probe_hold

    flip = [{"normal": i % 2, "priority": 0} for i in range(200)]
    real, probe_hold.queue_depth = probe_hold.queue_depth, lambda d: flip.pop(0)
    try:
        probe_hold.settled_depth("arn:dev", timeout=0, sleep=lambda _: None)
    finally:
        probe_hold.queue_depth = real
