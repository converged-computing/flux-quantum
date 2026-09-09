"""AWS Braket backend.

Two ways to take a device, set with --braket-hold.

probe is the default. Submit a trivial task and wait for it to reach the front
of the queue. That reserves nothing, so another user can take the device
between our task running and the classical job starting.

job starts a hybrid job. Braket runs one hybrid job at a time per QPU and gives
it priority, and the job publishes AMZN_BRAKET_JOB_TOKEN. A task created with
that token gets the job's priority and bills to the job. Without it, neither.

So the hybrid job is the scout and the token is the session id. The classical
work stays on our cluster instead of the small instance Braket gives the job,
and the hold costs only that instance, about $0.12 an hour.

The SDK is optional. The module imports without it and the backend refuses to
construct.
"""

import datetime
import json
import os
import time

from .base import Backend, BackendError, Signals, register

try:
    import boto3
    from braket.aws import AwsDevice
    from braket.circuits import Circuit
except ImportError:
    boto3 = AwsDevice = Circuit = None

# state simulator, no queue to speak of and cents per task
SV1 = "arn:aws:braket:::device/quantum-simulator/amazon/sv1"

# a task that has left the queue reports no position at all, so these are how
# we notice we missed the window rather than waiting for a value that can no
# longer appear
RAN = ("RUNNING", "COMPLETED")
FAILED = ("FAILED", "CANCELLED")


def position_at_most(pos, threshold):
    """True when the queue position is at or under threshold.

    Braket reports anything over 2000 as the string >2000, so a position that
    is not a number counts as far away.
    """
    try:
        return int(pos) <= int(threshold)
    except (TypeError, ValueError):
        return False


def region_for(device_arn, override=None):
    """Region to talk to. Devices are not all in one, and a QPU searched in the
    wrong region simply never appears."""
    parts = device_arn.split(":")
    from_arn = parts[3] if len(parts) > 3 else ""
    return (
        override
        or from_arn
        or os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or "us-east-1"
    )


def _count(value):
    """Leading number out of a queue depth field.

    Braket returns these as strings and sometimes appends a note, as in
    "0 (1 prioritized hybrid job running)". Anything without a leading digit
    counts as zero.
    """
    import re

    m = re.match(r"\s*(\d+)", str(value or ""))
    return int(m.group(1)) if m else 0


def queue_depth(device_arn, cls=None):
    """How many tasks are waiting on a device, split by queue.

    Braket counts Normal and Priority separately. A task submitted with a
    hybrid job token lands in Priority, so the two counts are how you tell
    from outside whether the token did anything.
    """
    from braket.aws import AwsDevice
    from braket.aws.queue_information import QueueType

    d = (cls or AwsDevice)(device_arn).queue_depth()
    return {
        "normal": _count(d.quantum_tasks.get(QueueType.NORMAL)),
        "priority": _count(d.quantum_tasks.get(QueueType.PRIORITY)),
        "jobs": _count(d.jobs),
        # the note Braket sometimes appends, which says whether a prioritised
        # hybrid job is running
        "jobs_note": str(d.jobs or ""),
    }


def is_open(device, now=None):
    """Is the device inside an execution window right now.

    ONLINE does not mean running. Most QPUs only execute during set hours and
    park everything else, which from outside looks the same as a long queue.
    A window names a weekday or one of the groups Everyday, Weekdays and
    Weekend, and a device can have several a day. Garnet stops between 15:30
    and 17:15 that way.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    groups = {
        "Everyday": True,
        "Weekdays": now.weekday() < 5,
        "Weekend": now.weekday() >= 5,
        now.strftime("%A"): True,
    }
    for w in device.properties.service.executionWindows:
        if groups.get(w.executionDay.value) and (
            w.windowStartHour <= now.time() < w.windowEndHour
        ):
            return True
    return False


def windows(device, local=True, now=None):
    """The device execution windows, as readable tuples.

    Braket gives these in UTC. Reading them against your own clock is where
    the mistakes happen, so they are converted to local time by default. A
    window that crosses midnight in the conversion is marked, since the day
    label no longer matches the hours.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = []
    for w in device.properties.service.executionWindows:
        if local:
            start, end = _to_local(w.windowStartHour, now), _to_local(
                w.windowEndHour, now
            )
        else:
            start = w.windowStartHour.strftime("%H:%M")
            end = w.windowEndHour.strftime("%H:%M")
        out.append((w.executionDay.value, start, end))
    return out


def _to_local(t, now):
    """A UTC clock time as local, tagged when it lands on another day.

    The day label on a window is the UTC day, so a converted time can sit
    before or after it. Saying so is the difference between a readable line
    and a misleading one.
    """
    stamp = datetime.datetime.combine(now.date(), t, tzinfo=datetime.timezone.utc)
    here = stamp.astimezone()
    shift = (here.date() - stamp.date()).days
    tag = {0: "", -1: " (prev day)", 1: " (next day)"}.get(shift, "")
    return here.strftime("%H:%M") + tag


# Braket charges a flat fee per task on top of whatever the device charges
# per shot. It is not in the device properties, so it lives here.
TASK_FEE = 0.30


def cost(device, shots=1):
    """What one task costs on this device.

    deviceCost comes from the service, so it does not go stale here. The unit
    is usually shot, sometimes minute for simulators, and the flat task fee is
    on top either way.
    """
    try:
        c = device.properties.service.deviceCost
        per, unit = float(c.price), str(c.unit).lower()
    except Exception:
        return None
    if unit == "shot":
        return TASK_FEE + per * shots
    # per minute devices bill for time, so a shot count says nothing
    return None


def shots_range(device):
    """The shot count this device accepts, or None if it does not say.

    Cepheus takes 10 to 50000 and refuses 1. Finding that out from a
    ValidationException means the hold is already up and billing.
    """
    try:
        r = device.properties.service.shotsRange
        return int(r[0]), int(r[1])
    except Exception:
        return None


def survey(minimum=0, cls=None, now=None, shots=1):
    """Online QPUs with their queue and whether they are open right now.

    Open ones first, then busiest. Simulators are left out since they scale
    on demand and never queue.
    """
    from braket.aws import AwsDevice

    out = []
    for d in (cls or AwsDevice).get_devices(statuses=["ONLINE"]):
        if "/qpu/" not in d.arn:
            continue
        try:
            q = queue_depth(d.arn, cls=cls)
            row = {
                "arn": d.arn,
                "open": is_open(d, now),
                "windows": windows(d),
                "cost": cost(d, shots),
                **q,
            }
        except Exception:
            continue
        if row["normal"] >= minimum:
            out.append(row)
    return sorted(out, key=lambda r: (not r["open"], -r["normal"]))


def qpus_with_a_queue(minimum=1, cls=None, now=None):
    """Devices where a priority comparison would show something.

    Both have to be true. An idle device runs a token task and a plain one at
    once, and a shut device runs neither.
    """
    return [d for d in survey(minimum, cls, now) if d["open"]]


@register
class BraketBackend(Backend):
    name = "braket"

    def __init__(self):
        if boto3 is None or AwsDevice is None:
            raise BackendError(
                "braket: amazon-braket-sdk is not installed, "
                "pip install amazon-braket-sdk"
            )
        # resolve credentials the way boto3 normally would, and fail here
        # rather than at the first API call
        self._credentials = boto3.Session().get_credentials()
        if self._credentials is None:
            raise BackendError(
                "braket: no AWS credentials. Set AWS_ACCESS_KEY_ID and "
                "AWS_SECRET_ACCESS_KEY, or configure ~/.aws/credentials"
            )
        method = getattr(self._credentials, "method", "unknown")
        if method in ("iam-role", "instance-metadata"):
            # the instance role belongs to the node and not to the user
            self.credential_note = (
                "braket: using the EC2 instance role, which belongs to the "
                "node and not to you. Export your own keys to keep the "
                "credential in user space"
            )
        else:
            self.credential_note = "braket: credentials present via {}".format(method)
        self._task = None
        self._hold_job = None
        self._hold_prefix = None
        self._session = None
        # what this run made, recorded as it is made so cleanup never has to
        # work it out afterwards
        self._made = {"job": None, "bucket": None, "prefix": None}
        # the device the token is good for, which is the device the hybrid job
        # named and nothing else
        self._token_device = None
        # wall time from asking for the hold to having it
        self._hold_started = None
        self._hold_ready_after = None

    @classmethod
    def add_options(cls, add_option):
        add_option(
            "--braket-device",
            metavar="ARN",
            default=None,
            help="Braket: device ARN, defaults to the SV1 simulator",
        )
        add_option(
            "--braket-region",
            metavar="REGION",
            default=None,
            help="Braket: AWS region, defaults to the one in the device ARN",
        )
        add_option(
            "--braket-shots",
            metavar="N",
            default=None,
            help="Braket: shots for the queue probe task, default 1",
        )
        add_option(
            "--braket-ungate-position",
            metavar="N",
            default=None,
            help="Braket: release the classical job once the probe task is at "
            "this queue position or closer. Default 1, next in line. Raise it "
            "when the classical job is slow to start",
        )
        add_option(
            "--braket-hold",
            metavar="MODE",
            default=None,
            help="Braket: how to take the device. probe waits for the front "
            "of the queue, the default. job starts a hybrid job, which holds "
            "the priority queue and hands out a token that work anywhere can "
            "submit with",
        )
        add_option(
            "--braket-hold-instance",
            metavar="TYPE",
            default=None,
            help="Braket: instance for the holding hybrid job. Default "
            "ml.m5.large, the cheapest, since it does no work",
        )
        add_option(
            "--braket-hold-max-seconds",
            metavar="SECONDS",
            default=None,
            help="Braket: give up the queue slot after this long, so a scout "
            "that is never released stops billing. Default 900",
        )
        add_option(
            "--braket-queue-timeout",
            metavar="SECONDS",
            default=None,
            help="Braket: give up if the probe task is still queued after this "
            "long. Default 0, meaning wait as long as the scout may run",
        )

    def scout_options(self, args):
        device = getattr(args, "braket_device", None) or SV1
        shots = getattr(args, "braket_shots", None)
        timeout = getattr(args, "braket_queue_timeout", None)
        return {
            "device": device,
            "region": region_for(device, getattr(args, "braket_region", None)),
            "hold": getattr(args, "braket_hold", None) or "probe",
            "hold_instance": getattr(args, "braket_hold_instance", None)
            or "ml.m5.large",
            "hold_max_seconds": float(
                getattr(args, "braket_hold_max_seconds", None) or 900
            ),
            "shots": 1 if shots is None else int(shots),
            "queue_timeout": 0 if timeout is None else float(timeout),
            "ungate_position": int(
                getattr(args, "braket_ungate_position", None)
                or os.environ.get("QUANTUM_BRAKET_UNGATE_POSITION", 1)
            ),
        }

    def job_environment(self, options):
        """The classical job may want to fetch the result, and it needs the same
        region to find the task."""
        return {
            "BRAKET_DEVICE": options.get("device") or SV1,
            "AWS_DEFAULT_REGION": options.get("region") or "us-east-1",
        }

    def open_session(self, options):
        """Take the device. probe returns a task ARN, job returns a job ARN.

        The scout calls wait_for_priority next. In job mode the token replaces
        the ARN once the job is running.
        """
        if (options.get("hold") or "probe") == "job":
            return self._open_hold_job(options)
        device_arn = options.get("device") or SV1
        region = options.get("region") or region_for(device_arn)
        # the device ARN carries the region, so it wins. setdefault let a
        # stale AWS_DEFAULT_REGION point the session at the wrong region, and
        # then the bucket we polled for the token, and the cancel on cleanup,
        # both went somewhere the job did not exist
        os.environ["AWS_DEFAULT_REGION"] = region

        device = AwsDevice(device_arn)
        # identity on one qubit. Braket measures every qubit, so this is the
        # smallest thing that still occupies the device.
        self._task = device.run(Circuit().i(0), shots=int(options.get("shots") or 1))
        return self._task.id

    def _open_hold_job(self, options):
        """Start a hybrid job that does nothing but hold the queue.

        Its entry point publishes the token to S3 and waits. We cannot read
        the token until the job runs, so this returns the job ARN and
        wait_for_priority swaps in the token later.
        """
        # only needed in job mode, so kept out of the module import
        from braket.aws import AwsQuantumJob
        from braket.jobs.config import InstanceConfig, StoppingCondition

        from . import hold

        device_arn = options.get("device") or SV1
        region = options.get("region") or region_for(device_arn)
        # the device ARN carries the region, so it wins. setdefault let a
        # stale AWS_DEFAULT_REGION point the session at the wrong region, and
        # then the bucket we polled for the token, and the cancel on cleanup,
        # both went somewhere the job did not exist
        os.environ["AWS_DEFAULT_REGION"] = region

        self._hold_started = time.time()
        # resolve the bucket now. Working it out during cleanup means a lookup
        # that can fail at the one moment it has to work, and the token stays
        # in the bucket when it does.
        from braket.aws import AwsSession

        self._made["bucket"] = AwsSession().default_bucket()
        name = "flux-quantum-hold-{}".format(int(self._hold_started))
        self._hold_prefix = "flux-quantum/{}".format(name)
        self._made["prefix"] = self._hold_prefix
        max_seconds = int(options.get("hold_max_seconds") or 900)
        self._hold_job = AwsQuantumJob.create(
            device=device_arn,
            source_module=os.path.dirname(os.path.abspath(hold.__file__)),
            entry_point="hold.entry:main",
            job_name=name,
            instance_config=InstanceConfig(
                instanceType=options.get("hold_instance") or "ml.m5.large",
                instanceCount=1,
                volumeSizeInGb=30,
            ),
            # create takes no environment, so config goes in here
            hyperparameters={
                "flux_quantum_prefix": self._hold_prefix,
                "flux_quantum_max_seconds": str(max_seconds),
            },
            # Braket stops the job itself, so a wedged entry point still
            # stops billing
            stopping_condition=StoppingCondition(maxRuntimeInSeconds=max_seconds + 300),
            wait_until_complete=False,
        )
        self._made["job"] = self._hold_job
        return self._hold_job.arn

    def _wait_for_hold_job(self, options, interval, sleep):
        """Ready once the job is RUNNING and its token has landed.

        Braket says RUNNING when the instance is up. The container still has
        to start and publish, which takes a minute or so. RUNNING without a
        token is reported as starting, not running.
        """
        timeout = float(options.get("queue_timeout") or 0)
        started = getattr(self, "_hold_started", None) or time.time()
        deadline = started + timeout if timeout > 0 else None
        s3 = boto3.client("s3")
        bucket = None
        while True:
            state = self._hold_job.state()
            waited = time.time() - started
            if state in ("FAILED", "CANCELLED", "COMPLETED"):
                return False, "hybrid job {} after {:.0f}s".format(state, waited)
            if state == "RUNNING":
                if bucket is None:
                    bucket = self._output_bucket()
                token = self._read_token(s3, bucket)
                if token:
                    asked = options.get("device") or SV1
                    held = getattr(self, "_token_device", None)
                    if held and held != asked:
                        return False, (
                            "the hold is on {} but the work is for {}. A job "
                            "token is only valid for the device its hybrid "
                            "job named, and submitting elsewhere with it "
                            "silently gets no priority".format(held, asked)
                        )
                    self._session = token
                    self._hold_ready_after = waited
                    return True, "holding the device after {:.0f}s".format(waited)
                said = "starting the container"
            else:
                said = state.lower()
            print(
                "braket: {} ({:.0f}s)".format(said, waited),
                flush=True,
            )
            if deadline is not None and time.time() + interval >= deadline:
                return False, "gave up after {:.0f}s, still {}".format(waited, said)
            sleep(interval)

    def _output_bucket(self):
        """The bucket the job writes to.

        We pass no code_location or output_data_config, so the job uses the
        session default. That is what the container sees as
        AMZN_BRAKET_OUT_S3_BUCKET.
        """
        from braket.aws import AwsSession

        return AwsSession().default_bucket()

    def _read_token(self, s3, bucket):
        """The token the job published, or None if it is not there yet.

        The device is kept alongside it. A token is only valid for the device
        its hybrid job named, so it has to travel with one.
        """
        try:
            body = s3.get_object(
                Bucket=bucket, Key="{}/token.json".format(self._hold_prefix)
            )["Body"].read()
            payload = json.loads(body)
        except Exception:
            return None
        self._token_device = payload.get("device")  # noqa: E501
        return payload.get("token")

    def session_id(self, opened):
        """The token replaces the job ARN. The token is what a task elsewhere
        submits with."""
        return self._session or opened

    def wait_for_priority(self, options=None, interval=5.0, sleep=time.sleep):
        """Poll until the probe task is next in line.

        Ready at the requested position or closer, and also once the task is
        RUNNING or COMPLETED, because a task that left the queue reports no
        position. FAILED and CANCELLED are a failure and not readiness, so the
        caller cancels the classical job rather than starting it.

        Returns (ok, reason).
        """
        options = options or {}
        if (options.get("hold") or "probe") == "job":
            return self._wait_for_hold_job(options, interval, sleep)
        timeout = float(options.get("queue_timeout") or 0)
        position = int(options.get("ungate_position") or 1)
        deadline = time.time() + timeout if timeout > 0 else None
        while True:
            state = self._task.state()
            if state in FAILED:
                return False, state
            if state in RAN:
                return True, state
            pos = self.queue_position()
            if position_at_most(pos, position):
                return True, "queue position {}".format(pos)
            print("braket: queued at position {}".format(pos), flush=True)
            if deadline is not None and time.time() + interval >= deadline:
                return False, "still queued at position {} after {}s".format(
                    pos, timeout
                )
            sleep(interval)

    def queue_position(self):
        """Position in the device queue, or None once the task has left it."""
        try:
            return self._task.queue_position().queue_position
        except Exception:
            return None

    def close_session(self, session=None):
        """Undo whatever this run made, in reverse.

        Driven by what was recorded at the time, so a run that failed halfway
        still cleans up the half it made. Safe to call twice, and safe to call
        when nothing was made at all.

        The release marker goes before the cancel, or a container polling in
        between never sees it. Each step is best effort, because a later step
        still needs to run when an earlier one fails.
        """
        made, self._made = self._made, {"job": None, "bucket": None, "prefix": None}
        bucket, prefix, job = made["bucket"], made["prefix"], made["job"]

        if bucket and prefix:
            self._s3_call(
                "put_object",
                Bucket=bucket,
                Key="{}/release".format(prefix),
                Body=b"released",
            )
        if job is not None:
            try:
                if job.state() not in ("COMPLETED", "FAILED", "CANCELLED"):
                    job.cancel()
            except Exception as e:
                print("braket: could not cancel the hold job: {}".format(e))
        if bucket and prefix:
            for key in ("token.json", "release"):
                self._s3_call(
                    "delete_object", Bucket=bucket, Key="{}/{}".format(prefix, key)
                )

    @staticmethod
    def _s3_call(op, **kw):
        """One S3 call that must not raise. Cleanup that gives up on the first
        failure leaves more behind than it removes."""
        try:
            getattr(boto3.client("s3"), op)(**kw)
        except Exception as e:
            print("braket: {} on {} failed: {}".format(op, kw.get("Key"), e))

    def probe(self):
        """Report the device status. Cheap, it is a metadata call."""
        try:
            device = AwsDevice(SV1)
            return Signals(available=device.status == "ONLINE", detail={"device": SV1})
        except Exception as e:
            return Signals(available=False, detail={"error": str(e)})
