"""AWS Braket backend.

Braket has no session to acquire. You submit a task, it waits in the device
queue, and it runs. So the scout submits a trivial no-op task and waits for it
to reach the front of the queue, which is as close to priority as Braket
offers.

Note the limitation, because it is the vendor's and not ours. Reaching the
front of the queue does not reserve anything, so another user can take the
device between our task running and the classical job starting. IBM sessions do
hold the device, at the price of billing wall clock for it. Braket exposes the
queue position but no way to hold it.

The SDK is an optional dependency, so it is imported inside the methods.
"""

import os
import time

from .base import Backend, Signals, register

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


@register
class BraketBackend(Backend):
    name = "braket"

    def __init__(self):
        self._task = None

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
        """Submit the probe task and return its ARN, which is what the
        classical job is handed. The scout calls wait_for_priority next."""
        from braket.aws import AwsDevice
        from braket.circuits import Circuit

        device_arn = options.get("device") or SV1
        region = options.get("region") or region_for(device_arn)
        os.environ.setdefault("AWS_DEFAULT_REGION", region)

        device = AwsDevice(device_arn)
        # identity on one qubit. Braket measures every qubit, so this is the
        # smallest thing that still occupies the device.
        self._task = device.run(Circuit().i(0), shots=int(options.get("shots") or 1))
        return self._task.id

    def wait_for_priority(self, options=None, interval=5.0, sleep=time.sleep):
        """Poll until the probe task is next in line.

        Ready at the requested position or closer, and also once the task is
        RUNNING or COMPLETED, because a task that left the queue reports no
        position and the value we wait for can never arrive. FAILED and
        CANCELLED are a failure and not readiness, so the caller can cancel the
        classical job rather than start it against nothing.

        Returns (ok, reason).
        """
        options = options or {}
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
            # positions over 2000 come back as the string >2000, so compare as
            # a string rather than an int
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
        """Nothing to release. The probe task finishes or is already terminal."""
        return

    def credentials_present(self):
        """Ask boto3 to resolve credentials the normal way, and say where they
        came from. An EC2 instance role works but belongs to the node rather
        than to you, so it is called out."""
        try:
            import boto3
        except ImportError:
            return (
                False,
                "braket: boto3 is not installed, pip install amazon-braket-sdk",
            )
        creds = boto3.Session().get_credentials()
        if creds is None:
            return False, (
                "braket: no AWS credentials. Set AWS_ACCESS_KEY_ID and "
                "AWS_SECRET_ACCESS_KEY, or configure ~/.aws/credentials"
            )
        method = getattr(creds, "method", "unknown")
        if method in ("iam-role", "instance-metadata"):
            return True, (
                "braket: using the EC2 instance role, which belongs to the node "
                "and not to you. Export your own keys to keep the credential in "
                "user space"
            )
        return True, "braket: credentials present via {}".format(method)

    def probe(self):
        """Report the device status. Cheap, it is a metadata call."""
        ok, _ = self.credentials_present()
        if not ok:
            return Signals(available=False)
        try:
            from braket.aws import AwsDevice

            device = AwsDevice(SV1)
            return Signals(available=device.status == "ONLINE", detail={"device": SV1})
        except Exception as e:
            return Signals(available=False, detail={"error": str(e)})
