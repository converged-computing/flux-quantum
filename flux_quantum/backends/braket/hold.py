"""The hybrid job that holds device priority.

Runs inside the Braket container. It does no quantum work. It exists so the
job is running, because a running hybrid job holds the priority queue for its
device, and it publishes the token that lets work elsewhere use that priority.

AMZN_BRAKET_JOB_TOKEN goes to CreateQuantumTask as jobToken. Tasks without it
get no priority and bill as standalone, so the token is what we carry out.

Then it waits for a release marker in S3, or its own deadline. That is the
hold. The classical work runs on our cluster while this keeps the place.
"""

import json
import os
import time


def _s3():
    import boto3

    return boto3.client("s3")


def publish(bucket, prefix, payload, client=None):
    """Write the token where the scout can read it."""
    client = client or _s3()
    client.put_object(
        Bucket=bucket,
        Key="{}/token.json".format(prefix.rstrip("/")),
        Body=json.dumps(payload).encode(),
    )


def released(bucket, prefix, client=None):
    """True once the release marker exists."""
    client = client or _s3()
    try:
        client.head_object(Bucket=bucket, Key="{}/release".format(prefix.rstrip("/")))
        return True
    except Exception:
        return False


def _hyperparameters():
    """What the scout passed in. create takes no environment, so config comes
    through as hyperparameters."""
    try:
        from braket.jobs.environment_variables import get_hyperparameters

        return get_hyperparameters()
    except Exception:
        return {}


def main(env=None, sleep=time.sleep, client=None, params=None):
    env = env or os.environ
    params = _hyperparameters() if params is None else params
    bucket = env["AMZN_BRAKET_OUT_S3_BUCKET"]
    prefix = (
        params.get("flux_quantum_prefix")
        or env.get("FLUX_QUANTUM_PREFIX")
        or "flux-quantum/{}".format(env.get("AMZN_BRAKET_JOB_NAME", "job"))
    )
    payload = {
        "token": env.get("AMZN_BRAKET_JOB_TOKEN"),
        "device": env.get("AMZN_BRAKET_DEVICE_ARN"),
        "job_arn": env.get("AMZN_BRAKET_JOB_ARN"),
        "published": time.time(),
    }
    publish(bucket, prefix, payload, client=client)
    print("hold: published token for {}".format(payload["device"]), flush=True)

    # Hold until released or out of time. The instance bills by the minute,
    # so a scout that never releases needs a stop.
    deadline = time.time() + float(
        params.get("flux_quantum_max_seconds")
        or env.get("FLUX_QUANTUM_MAX_SECONDS", 900)
    )
    interval = float(env.get("FLUX_QUANTUM_POLL", 5))
    while time.time() < deadline:
        if released(bucket, prefix, client=client):
            print("hold: released", flush=True)
            return 0
        sleep(interval)
    print("hold: deadline reached, giving up the queue slot", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
