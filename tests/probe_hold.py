#!/usr/bin/env python3
"""Does a hybrid job token give priority to work submitted elsewhere.

That is what this branch turns on. If the token travels, the hybrid job is the
scout and the classical work stays on our cluster. If not, the job has to
submit for us, which is a different design.

Run against SV1 first. A simulator runs tasks straight away so this says
nothing about ordering, but it does say whether the API takes the token from
outside the container and links the task to the job. That costs a few cents.
Then run it against a QPU, where queuePriority is the direct answer.

    python3 probe_hold.py                    # SV1
    python3 probe_hold.py --device arn:...   # a real QPU, costs money

The hold is closed in a finally, so nothing is left running.
"""

import argparse
import datetime
import json
import time
import uuid

import boto3
from braket.aws import AwsSession

from flux_quantum.backends.braket import (
    SV1,
    BraketBackend,
    is_open,
    queue_depth,
    shots_range,
    survey,
    windows,
)

# one qubit, measured. The cheapest thing that is still a real task.
BELL = """OPENQASM 3;
qubit[1] q;
bit[1] c;
h q[0];
c[0] = measure q[0];
"""


def submit(braket, device, bucket, token=None, shots=1):
    """Create a task, with or without the token.

    Without it the docs say the task gets no priority and bills standalone.
    The pair of calls is the comparison.
    """
    args = {
        "clientToken": str(uuid.uuid4()),
        "deviceArn": device,
        "shots": shots,
        "outputS3Bucket": bucket,
        "outputS3KeyPrefix": "flux-quantum-probe",
        "action": json.dumps(
            {
                "braketSchemaHeader": {
                    "name": "braket.ir.openqasm.program",
                    "version": "1",
                },
                "source": BELL,
            }
        ),
    }
    if token:
        args["jobToken"] = token
    return braket.create_quantum_task(**args)["quantumTaskArn"]


TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")


def describe(braket, arn):
    """What the service says about the task."""
    t = braket.get_quantum_task(quantumTaskArn=arn)
    q = t.get("queueInfo") or {}
    return {
        "status": t.get("status"),
        "job": t.get("jobArn"),
        "queue": q.get("queue"),
        "position": q.get("position"),
        "priority": q.get("queuePriority"),
    }


def settle(braket, arns, timeout=180, interval=1.0, sleep=time.sleep):
    """Follow both tasks to the end, keeping the best view of each.

    jobArn is set when the task is created and is still there afterwards, so
    the verdict never has to catch anything in flight. queueInfo is the
    opposite. It describes a task that is still waiting and is gone once it
    runs, so we record it if we see it and never wait for it. A one qubit task
    on a simulator can finish before the first poll returns.
    """
    best = {a: {} for a in arns}
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = False
        for a in arns:
            seen = describe(braket, a)
            for k, v in seen.items():
                # keep the first real value we see
                if v and not best[a].get(k):
                    best[a][k] = v
            best[a]["status"] = seen["status"]
            if seen["status"] not in TERMINAL:
                pending = True
        if not pending:
            break
        sleep(interval)
    return best


def make_queue(braket, device, bucket, count, shots=1):
    """Submit filler tasks so there is a queue to jump.

    A device is only busy while it is shut, and drains as soon as a window
    opens, so waiting for someone else's backlog means waiting for a window to
    open with work still in front of it. Making our own removes that.

    These go in without a token so they sit in the Normal queue, and they go
    in after the hold is ready, because a fast device empties during the two
    minutes the hold takes to provision.

    They cost the task fee each, so keep the count small.
    """
    arns = []
    for _ in range(count):
        arns.append(submit(braket, device, bucket, None, shots))
    return arns


def drain_queue(braket, arns):
    """Cancel the filler, so it does not run and bill for shots.

    A task that has already started cannot be cancelled, which is fine. The
    task fee is spent either way, the shots are what this saves.
    """
    for a in arns:
        try:
            # clientToken is required on this call, not optional as on most
            braket.cancel_quantum_task(quantumTaskArn=a, clientToken=str(uuid.uuid4()))
        except Exception as e:
            # a task that already ran cannot be cancelled, which is expected
            # on a fast device and not worth a wall of text
            if "COMPLETED" not in str(e):
                print("could not cancel %s: %s" % (a.split("/")[-1], e))


def wait_for_queue(device, timeout=20, interval=0.5, sleep=time.sleep):
    """Wait until the filler actually shows up in the Normal queue.

    Submitting returns before the service counts the task, and on a fast
    device the window between counted and finished is short. Reading the
    depth straight after the submit can miss it in either direction.
    """
    deadline = time.time() + timeout
    last = queue_depth(device)
    while time.time() < deadline:
        if last["normal"] > 0:
            return last
        sleep(interval)
        last = queue_depth(device)
    return last


def calibration(device):
    """A summary of the device's calibration state, not the whole dump.

    Which edges were usable and when they were last characterised is what a
    result has to be read against. A run against a device with a sixth of its
    couplers dead is not the same measurement as one against a healthy one.
    """
    try:
        specs = device.properties.provider.specs
    except Exception:
        return None
    edges, stamps = [], []
    for group in _walk_specs(specs):
        ids, value, when = group
        edges.append((ids, value))
        if when:
            stamps.append(when)
    live = [v for _, v in edges if v is not None and v > 0.6]
    if not live:
        return None
    return {
        "live": len(live),
        "dead": len(edges) - len(live),
        "error_min": round(1 - max(live), 6),
        "error_max": round(1 - min(live), 6),
        "calibrated_from": min(stamps) if stamps else None,
        "calibrated_to": max(stamps) if stamps else None,
    }


def _walk_specs(node):
    """fCZ entries wherever the provider put them."""
    if isinstance(node, dict):
        if "node_ids" in node and "characteristics" in node:
            for c in node["characteristics"]:
                if c.get("name") == "fCZ":
                    yield tuple(node["node_ids"]), c.get("value"), c.get("timestamp")
            return
        for v in node.values():
            yield from _walk_specs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_specs(v)


def used_qubits(braket_task_arn):
    """Which physical qubits the task actually ran on.

    The compiler places the circuit, so what you asked for and what ran are
    not the same thing. Without this a result cannot be read against the
    calibration data for the edges it used.
    """
    try:
        from braket.aws import AwsQuantumTask

        task = AwsQuantumTask(braket_task_arn)
        # result() polls until the task finishes, five days by default. On a
        # task that is still queued that is a hang, not a wait.
        if task.state() != "COMPLETED":
            return {"state": task.state(), "note": "not finished, nothing to read"}
        r = task.result()
        out = {"measured_qubits": list(r.measured_qubits or [])}
        meta = getattr(r, "additional_metadata", None)
        prog = getattr(getattr(meta, "rigettiMetadata", None), "compiledProgram", None)
        if prog:
            out["compiled"] = prog
        return out
    except Exception as e:
        return {"error": str(e)}


def record(path, row):
    """One JSON line per run.

    Printing to a terminal that is then closed is how a campaign ends up with
    numbers nobody can reproduce. The token is left out on purpose: it grants
    priority and bills to the job, so it does not belong in a log.
    """
    import json as _json

    with open(path, "a") as fh:
        fh.write(_json.dumps(row, default=str) + "\n")


def settled_depth(device, timeout=30, interval=2.0, sleep=time.sleep):
    """Read the queue once it has stopped moving.

    The counters lag submission by a few seconds. Reading straight after a
    submit gave normal=1 right after six filler tasks went in, then 7 a
    moment later, so a token task's arrival in Priority was attributed to the
    filler catching up. Wait for two readings in a row to agree.
    """
    last = queue_depth(device)
    deadline = time.time() + timeout
    while time.time() < deadline:
        sleep(interval)
        now = queue_depth(device)
        if all(now.get(k) == last.get(k) for k in ("normal", "priority")):
            return now
        last = now
    return last


def moved(before, after):
    """Which queue grew between two readings."""
    return {
        k: after.get(k, 0) - v
        for k, v in before.items()
        if isinstance(v, int) and isinstance(after.get(k), int)
    }


def _tzname():
    """What the times below are in, so nobody has to guess."""
    import time as _t

    return _t.tzname[_t.daylight and _t.localtime().tm_isdst > 0] or "local time"


def show_survey(shots=1):
    """What is worth running against right now."""
    rows = survey()
    if not rows:
        print("no online QPUs found")
        return
    print("%-70s %-6s %-7s %s" % ("device", "open", "normal", "priority"))
    for r in rows:
        print(
            "%-70s %-6s %-7d %d"
            % (r["arn"], "yes" if r["open"] else "no", r["normal"], r["priority"])
        )
    usable = [r for r in rows if r["open"] and r["normal"] > 0]

    # every device, with when it runs. A device being open and empty now says
    # nothing about whether it is worth coming back to.
    print("\n  windows in %s" % _tzname())
    for r in rows:
        print(
            "  %s  (%d queued, %s)"
            % (r["arn"], r["normal"], "open" if r["open"] else "shut")
        )
        for w in r["windows"]:
            print("      %-10s %s to %s" % w)

    if usable:
        # busiest first is the best demonstration, but cheapest is the one
        # worth naming when they are all queued
        cheapest = min(
            (r for r in usable if r["cost"] is not None),
            key=lambda r: r["cost"],
            default=usable[0],
        )
        print("\nbusiest queue:  %s" % usable[0]["arn"])
        if cheapest is not usable[0] and cheapest["cost"] is not None:
            print(
                "cheapest probe: %s at $%.2f" % (cheapest["arn"], 2 * cheapest["cost"])
            )


def verdict(
    with_token, without, device, by_token=None, by_control=None, after_token=None
):
    """What the two tasks say, strongest evidence first.

    jobArn is the one that matters. AWS says a task without the token gets no
    priority and bills standalone, so association is what the token carries.

    queuePriority only exists while a task waits, so it is reported when we
    saw it and not otherwise. A simulator usually shows nothing here.
    """
    out = []
    if with_token.get("job"):
        out.append("ASSOCIATED. The token travels: the task belongs to the job.")
    else:
        out.append(
            "NOT ASSOCIATED. The call was accepted but the task is standalone,\n"
            "which bills separately and gets no priority. Check the bill rather\n"
            "than the return code before believing otherwise."
        )
    if without.get("job"):
        out.append(
            "WARNING: the control task is also associated, so this comparison\n"
            "says nothing. Something other than the token is linking them."
        )

    # Queue depth is counted per device and per queue, so a task landing in
    # Priority shows up without having to catch it in flight.
    settled = False

    # The count itself beats the difference between two counts. Priority has
    # read zero on every survey of these devices, so one task sitting in it
    # after we submitted exactly one token task is the plainest evidence
    # there is. Movement missed this because the counters lag a submit.
    if after_token and after_token.get("priority", 0) > 0:
        out.append(
            "IN THE PRIORITY QUEUE. %d task%s waiting there against %d in "
            "Normal, and the only token task submitted was ours."
            % (
                after_token["priority"],
                "" if after_token["priority"] == 1 else "s",
                after_token.get("normal", 0),
            )
        )
        settled = True

    # A queue that emptied while the hold was coming up is not the same as a
    # device that never had one. Taking the hold costs a couple of minutes,
    # and a fast device clears its backlog in that time.
    drained = by_token and by_token.get("normal", 0) < 0
    if drained:
        out.append(
            "The Normal queue lost %d tasks while the hold was provisioning, "
            "so there was nothing left to jump by the time the token task "
            "went in. Use --make-queue to build one after the hold is up."
            % -by_token["normal"]
        )
        settled = True
    if not settled and by_token and by_control:
        if by_token.get("priority", 0) > 0 and by_control.get("normal", 0) > 0:
            out.append(
                "PRIORITISED. The token submit grew the Priority queue and "
                "the control grew Normal."
            )
            settled = True
        elif by_token.get("normal", 0) > 0:
            out.append(
                "NOT PRIORITISED. The token submit grew the Normal queue, so "
                "the task queues like any other."
            )
            settled = True

    if not settled:
        a, c = with_token.get("priority"), without.get("priority")
        if a and c and a != c:
            out.append("PRIORITISED. {} against {} without the token.".format(a, c))
        elif a and c:
            out.append(
                "Same queuePriority either way ({}), so the token associates "
                "but does not lift.".format(a)
            )
        else:
            out.append(
                "Nothing was waiting on the device, so priority cannot be "
                "seen. Rerun against a QPU with a queue."
            )

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=SV1)
    ap.add_argument("--shots", type=int, default=1)
    ap.add_argument(
        "--max-seconds",
        type=int,
        default=900,
        help="ceiling on the hold, so a mistake stops billing",
    )
    ap.add_argument(
        "--record",
        default="probe-runs.jsonl",
        metavar="PATH",
        help="append a JSON line per run, so a result can be read back "
        "against the device state it was taken on",
    )
    ap.add_argument(
        "--survey",
        action="store_true",
        help="list online QPUs with their queues and windows, then stop",
    )
    ap.add_argument(
        "--filler-shots",
        type=int,
        default=500,
        metavar="N",
        help="shots per filler task. Shots are what keep a task occupying the "
        "device: ten of them finish before the queue can be read, five "
        "hundred hold it long enough to queue behind",
    )
    ap.add_argument(
        "--make-queue",
        type=int,
        default=0,
        metavar="N",
        help="submit N filler tasks first so there is a Normal queue to jump. "
        "Costs the task fee each, and they are cancelled afterwards",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="run even if the device is shut or has nothing queued",
    )
    args = ap.parse_args()

    if args.survey:
        show_survey(args.shots)
        return

    opts = {
        "hold": "job",
        "device": args.device,
        "hold_instance": "ml.m5.large",
        "hold_max_seconds": args.max_seconds,
        "queue_timeout": args.max_seconds,
    }

    run = {
        "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "device": args.device,
        "shots": args.shots,
        "filler_tasks": args.make_queue,
        "filler_shots": args.filler_shots,
    }

    b = BraketBackend()
    print(b.credential_note)
    print("device:", args.device)

    # A shut device or an empty queue means the run cannot show anything, and
    # finding that out after provisioning wastes time and money.
    q0 = queue_depth(args.device)
    print("device queues:", q0)
    if args.device != SV1 and not args.force:
        from braket.aws import AwsDevice

        d = AwsDevice(args.device)
        run["calibration"] = calibration(d)
        run["windows"] = windows(d)
        limits = shots_range(d)
        for what, n in (("--shots", args.shots), ("--filler-shots", args.filler_shots)):
            if limits and not (limits[0] <= n <= limits[1]):
                raise SystemExit(
                    "this device takes {} to {} shots and {} is {}. "
                    "Submitting would fail after the hold is up and "
                    "billing.".format(limits[0], limits[1], what, n)
                )
        if not is_open(d):
            print("execution windows, {}:".format(_tzname()))
            for w in windows(d):
                print("  %-10s %s to %s" % w)
            raise SystemExit(
                "the device is outside its execution window, so the job would "
                "just sit there. Wait for a window, or pass --force."
            )
        if q0["normal"] == 0 and not args.make_queue:
            raise SystemExit(
                "nothing is waiting on this device, so a priority comparison "
                "would say nothing. Pick a busier one, pass --make-queue N to "
                "build a backlog, or pass --force."
            )

    arn = None
    filler = []
    braket = boto3.client("braket")
    bucket = AwsSession().default_bucket()
    try:
        arn = b.open_session(opts)
        print("hybrid job:", arn)
        ok, why = b.wait_for_priority(opts)
        print(why)
        if not ok:
            raise SystemExit("never got the hold, nothing submitted, nothing spent")

        run["hold_job"] = arn
        run["hold_ready_after_s"] = getattr(b, "_hold_ready_after", None)
        token = b.session_id(arn)
        print("token:", token[:12], "...")
        print("job state at submit time:", b._hold_job.state())

        # Build the backlog now, not before the hold. Taking the hold costs a
        # couple of minutes, and a fast device empties in that time: four ten
        # shot tasks on Cepheus were all COMPLETED before the hold was ready,
        # so there was nothing left to jump.
        if args.make_queue:
            filler = make_queue(
                braket, args.device, bucket, args.make_queue, args.filler_shots
            )
            q0 = settled_depth(args.device)
            print("queued %d filler tasks, device now %s" % (len(filler), q0))
            if q0["normal"] == 0:
                print(
                    "the filler ran before it could queue. %d tasks of %d "
                    "shots is not enough to occupy this device, so raise "
                    "--filler-shots." % (args.make_queue, args.filler_shots)
                )

        with_token = submit(braket, args.device, bucket, token, args.shots)
        q1 = settled_depth(args.device)
        print("\nwith the token:", with_token, "queues", q1)

        without = submit(braket, args.device, bucket, None, args.shots)
        q2 = settled_depth(args.device)
        print("without it:    ", without, "queues", q2)

        by_token, by_control = moved(q0, q1), moved(q1, q2)
        print("\nmovement, token submit:  ", by_token)
        print("movement, control submit:", by_control)

        seen = settle(braket, [with_token, without])
        a, c = seen[with_token], seen[without]
        print("\n  %-10s %-30s %s" % ("", "with token", "without"))
        for k in ("status", "job", "queue", "position", "priority"):
            print("  %-10s %-30s %s" % (k, a.get(k), c.get(k)))

        run.update(
            {
                "queues": {"before": q0, "after_token": q1, "after_control": q2},
                "moved": {"token": by_token, "control": by_control},
                "with_token": {"arn": with_token, **a},
                "without": {"arn": without, **c},
            }
        )
        lines = verdict(a, c, args.device, by_token, by_control, q1)
        run["verdict"] = lines
        for line in lines:
            print(line)

        # which physical qubits ran, so the result can be read against the
        # calibration for those edges
        run["with_token"]["qubits"] = used_qubits(with_token)
        run["without"]["qubits"] = used_qubits(without)
    finally:
        run["ended"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            record(args.record, run)
            print("recorded to %s" % args.record)
        except Exception as e:
            print("could not record this run: %s" % e)
        if filler:
            print("\ncancelling %d filler tasks" % len(filler))
            drain_queue(braket, filler)
        if arn:
            print("releasing the hold")
            b.close_session()
        print("job state:", b._hold_job.state() if b._hold_job else "none")


if __name__ == "__main__":
    main()
