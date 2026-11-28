#!/usr/bin/env python3
"""Does a hybrid job token give priority to work submitted from outside.

That is what this branch turns on. If the token travels, the hybrid job is the
scout and the classical work stays on our cluster. If not, the job has to
submit for us, which is a different design.

The measurement is a race the token task has to win from behind.

    open the hold, a hybrid job that does nothing but publish its token
    submit a control task without the token
    submit the token task after it, so it is behind the control
    watch both until the token task finishes, with the hold still up

Two things say whether the token bought anything. Braket reports which queue
a task is waiting in, Normal or Priority, and its position there, but only
when GetQuantumTask is asked for QueueInfo. Earlier runs never asked, so
they saw nothing and fell back to the device counters, which lag a submit by
minutes. Then the service timestamps say whether the token task finished
ahead of a control that was submitted before it. Both are read straight from
the tasks, so neither depends on the counters.

Run against SV1 first. A simulator runs tasks straight away so this says
nothing about ordering, but it does say whether the API takes the token from
outside the container and links the task to the job. That costs a few cents.
Then run it against a QPU that has a queue, where the queue is the answer.

    python3 tests/probe_hold.py --survey                # who has a queue
    python3 tests/probe_hold.py                         # SV1
    python3 tests/probe_hold.py --device arn:... --shots 100 --max-seconds 1800

--max-seconds is the hold, and the token task has to finish inside it. On a
device where each task takes minutes, give it half an hour. The hold is
closed in a finally, so nothing is left running.
"""

import argparse
import datetime
import json
import os
import sys
import time
import uuid

import boto3
from braket.aws import AwsSession

from flux_quantum.backends.braket import (
    SV1,
    BraketBackend,
    cost,
    is_open,
    queue_depth,
    region_for,
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

TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")


def qasm_action():
    """The one qubit circuit as a task action."""
    return json.dumps(
        {
            "braketSchemaHeader": {
                "name": "braket.ir.openqasm.program",
                "version": "1",
            },
            "source": BELL,
        }
    )


def ahs_program():
    """The smallest analog program Aquila will take.

    Aquila runs Hamiltonians, not circuits, so a circuit is refused. One
    atom, one microsecond, every field held at zero. Built with the SDK so
    the schema is the SDK's and not ours.
    """
    from braket.ahs.analog_hamiltonian_simulation import AnalogHamiltonianSimulation
    from braket.ahs.atom_arrangement import AtomArrangement
    from braket.ahs.driving_field import DrivingField
    from braket.timings.time_series import TimeSeries

    zero = TimeSeries().put(0.0, 0.0).put(1e-6, 0.0)
    return AnalogHamiltonianSimulation(
        register=AtomArrangement().add((0.0, 0.0)),
        hamiltonian=DrivingField(amplitude=zero, phase=zero, detuning=zero),
    )


def ahs_action():
    ir = ahs_program().to_ir()
    dump = getattr(ir, "model_dump_json", None) or ir.json
    return dump()


def program_action(device):
    """The task action for this device: analog for QuEra, a circuit otherwise."""
    if "/quera/" in device:
        return ahs_action()
    return qasm_action()


def check_program(device):
    """Run the program on the local simulator, which validates it for free.

    The analog simulator checks the program against Aquila's own limits
    before it simulates, so a program it accepts is one Aquila accepts, and
    a program it refuses would have been refused after the hold was up.
    """
    from braket.devices import LocalSimulator

    if "/quera/" in device:
        LocalSimulator("braket_ahs").run(ahs_program(), shots=1).result()
    else:
        from braket.ir.openqasm import Program

        LocalSimulator().run(Program(source=BELL), shots=1).result()


# the ml.m5.large the hold runs on, per hour. Braket bills the instance by
# the minute and nothing else for a job that submits no tasks of its own
HOLD_PER_HOUR = 0.115


def submit(braket, device, bucket, token=None, shots=1, action=None):
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
        "action": action or program_action(device),
    }
    if token:
        args["jobToken"] = token
    return braket.create_quantum_task(**args)["quantumTaskArn"]


def describe(braket, arn):
    """What the service says about the task, queue included.

    queueInfo only comes back when the call asks for it. Without
    additionalAttributeNames the response has no queue, no position and no
    queuePriority, which is what every run before this one saw.
    """
    t = braket.get_quantum_task(
        quantumTaskArn=arn, additionalAttributeNames=["QueueInfo"]
    )
    q = t.get("queueInfo") or {}
    position = q.get("position")
    if position in ("None", ""):
        position = None
    return {
        "status": t.get("status"),
        "job": t.get("jobArn"),
        "queue": q.get("queue"),
        "position": position,
        "priority": q.get("queuePriority"),
        "message": q.get("message"),
        "created": t.get("createdAt"),
        "ended": t.get("endedAt"),
    }


def _stamp(now):
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()


def follow(
    braket,
    arns,
    timeout=1800,
    interval=5.0,
    sleep=time.sleep,
    clock=time.time,
    done=None,
    views=None,
    say=None,
    heartbeat=60,
):
    """Poll the tasks until done says so, keeping what each one showed.

    The queue fields describe a task that is waiting and are gone once it
    runs, so the first sighting of each is kept as the task's queue, and the
    timeline records every change so the run can be read back later. A one
    qubit task on a simulator can finish before the first poll returns, and
    then there is simply no queue entry, which the verdict allows for.

    done takes the views and says whether to stop. The default is every task
    terminal. Pass views back in to keep following after a stop.
    """
    if done is None:

        def done(v):
            return all(v[a].get("status") in TERMINAL for a in arns)

    views = views if views is not None else {}
    for a in arns:
        views.setdefault(a, {"timeline": []})
    deadline = clock() + timeout
    started = last_word = clock()
    while True:
        now = clock()
        for a in arns:
            v = views[a]
            if v.get("status") in TERMINAL:
                continue
            seen = describe(braket, a)
            for k in ("job", "created", "ended", "queue", "message"):
                if seen.get(k) and not v.get(k):
                    v[k] = seen[k]
            # which queue the task waited in, and where it started
            if seen["priority"] and not v.get("priority"):
                v["priority"] = seen["priority"]
                v["position"] = seen["position"]
            if seen["position"]:
                v["position_last"] = seen["position"]
            v["status"] = seen["status"]
            change = (seen["status"], seen["priority"], seen["position"])
            if not v["timeline"] or v["timeline"][-1][1:] != list(change):
                v["timeline"].append([_stamp(now), *change])
                if say:
                    say(a, seen)
                    last_word = now
        if done(views) or clock() + interval > deadline:
            return views
        # a device that takes minutes per task prints nothing for minutes,
        # which looks like a hang. Say we are still here, and what we see
        if say and heartbeat and now - last_word >= heartbeat:
            print(
                "  still waiting after %.0fs, %s"
                % (
                    now - started,
                    ", ".join(
                        "%s %s%s"
                        % (
                            a.split("/")[-1][:8],
                            views[a].get("status"),
                            (
                                " at %s" % views[a]["position_last"]
                                if views[a].get("position_last")
                                else ""
                            ),
                        )
                        for a in arns
                    ),
                ),
                flush=True,
            )
            last_word = now
        sleep(interval)


def seconds_between(earlier, later):
    """later minus earlier in seconds, or None if either is missing."""
    if not earlier or not later:
        return None
    return (later - earlier).total_seconds()


def make_queue(braket, device, bucket, count, shots=1):
    """Submit filler tasks so there is a queue to jump.

    A device that is idle runs the control and the token task at once and
    shows nothing. These go in without a token so they sit in Normal, and
    they go in after the hold is ready, because a fast device empties during
    the two minutes the hold takes to provision.

    They cost the task fee each, so keep the count small.
    """
    arns = []
    for _ in range(count):
        arns.append(submit(braket, device, bucket, None, shots))
    return arns


def drain_queue(braket, arns):
    """Cancel what is still waiting, so it does not run and bill for shots.

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


class Tee:
    """Everything printed goes to the terminal and to a file.

    A terminal that is closed takes the run with it. The backend prints its
    progress too, and a traceback goes to stderr, so both streams are teed.
    """

    def __init__(self, path, stream):
        self.fh = open(path, "a")
        self.stream = stream

    def write(self, s):
        self.stream.write(s)
        self.fh.write(s)
        self.fh.flush()
        return len(s)

    def flush(self):
        self.stream.flush()
        self.fh.flush()


def log_to(path, argv=None):
    """Tee stdout and stderr to path, and write a header naming the run."""
    sys.stdout = Tee(path, sys.stdout)
    sys.stderr = Tee(path, sys.stderr)
    print(
        "\n=== %s  %s"
        % (
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            " ".join(argv if argv is not None else sys.argv),
        )
    )


def record(path, row):
    """One JSON line per run.

    Printing to a terminal that is then closed is how a campaign ends up with
    numbers nobody can reproduce. The token is left out on purpose: it grants
    priority and bills to the job, so it does not belong in a log.
    """
    import json as _json

    with open(path, "a") as fh:
        fh.write(_json.dumps(row, default=str) + "\n")


def inspect(braket, arns):
    """What the service says about tasks from earlier runs. Free.

    createdAt and endedAt give how long a device took to serve a task, which
    is what decides whether a queue on it is real. The queue label survives
    completion, so the Priority against Normal reading is still there too.
    """
    rows = [describe(braket, a) for a in arns]
    print(
        "  %-14s %-10s %-9s %-26s %-26s %s"
        % ("task", "status", "queue", "created", "ended", "took")
    )
    for a, r in zip(arns, rows):
        took = seconds_between(r.get("created"), r.get("ended"))
        print(
            "  %-14s %-10s %-9s %-26s %-26s %s"
            % (
                a.split("/")[-1][:12],
                r.get("status"),
                r.get("priority") or "-",
                r.get("created") or "-",
                r.get("ended") or "-",
                "-" if took is None else "%.0fs" % took,
            )
        )
    return rows


def pace(braket, device, bucket, count, shots, timeout=600, **kw):
    """How fast a device serves plain tasks, with no hold and no token.

    Whether filler can build a queue depends on this alone. Cepheus served
    eight tasks in under a second, Forte held two for six minutes. Tasks go
    in back to back and each is followed to its end, and the report says
    how long each took and whether they ran one after another or all at
    once. Costs the task fee and shots each, nothing else.
    """
    arns = make_queue(braket, device, bucket, count, shots)
    views = follow(braket, arns, timeout=timeout, **kw)
    rows = []
    for a in arns:
        v = views[a]
        rows.append(
            {
                "arn": a,
                "status": v.get("status"),
                "created": v.get("created"),
                "ended": v.get("ended"),
                "took": seconds_between(v.get("created"), v.get("ended")),
                "position": v.get("position"),
            }
        )
    return rows


def pace_report(rows):
    """Serial or not, and how long each task took."""
    out = []
    for r in rows:
        out.append(
            "  %-14s %-10s queued at %-5s took %s"
            % (
                r["arn"].split("/")[-1][:12],
                r["status"],
                r["position"] or "-",
                "-" if r["took"] is None else "%.1fs" % r["took"],
            )
        )
    done = [r for r in rows if r["created"] and r["ended"]]
    if len(done) >= 2:
        # created to ended includes the wait, so it says nothing on its own.
        # The gap between one finish and the next is the service time, and
        # a queue is only visible when that is longer than a poll
        ends = sorted(r["ended"] for r in done)
        gaps = [seconds_between(a, b) for a, b in zip(ends, ends[1:])]
        if min(gaps) >= 5:
            out.append(
                "SERIAL. %d tasks finished %.0fs apart at the least, about "
                "%.0fs each, so filler will hold this device."
                % (len(done), min(gaps), sum(gaps) / len(gaps))
            )
        else:
            out.append(
                "OVERLAPPING. %d tasks finished within %.1fs of each other, so "
                "this device runs them together or in under a poll, and "
                "filler will not build a queue here." % (len(done), min(gaps))
            )
    return out


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


def _where(view):
    """A task's queue and position as words, for the verdict."""
    if not view.get("priority"):
        return None
    out = view["priority"]
    if view.get("position"):
        out += " at position %s" % view["position"]
    return out


def verdict(with_token, without, device, queues=None):
    """What the two tasks say, strongest evidence first.

    jobArn is the one that matters. AWS says a task without the token gets no
    priority and bills standalone, so association is what the token carries.

    Then the queue each task waited in, read from the task itself, and then
    the order they finished in. The token task went in after the control, so
    finishing before it means it was served out of order.

    The device counters are not consulted. They lag a submit by minutes and
    are the reason earlier runs read nothing.
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

    tq, cq = _where(with_token), _where(without)
    if with_token.get("priority") == "Priority":
        line = "IN THE PRIORITY QUEUE. The token task waited as %s" % tq
        if cq:
            line += ", the control as %s" % cq
        out.append(line + ".")
    elif with_token.get("priority") == "Normal":
        out.append(
            "NOT PRIORITISED. The token task waited in the Normal queue like "
            "any other%s." % (", the control as %s" % cq if cq else "")
        )
    elif not cq:
        # neither task was ever seen waiting, which is what an idle device or
        # a simulator looks like. Ordering says nothing then either.
        out.append(
            "Nothing was waiting on the device, so priority cannot be seen. "
            "Rerun against a QPU with a queue."
        )
        return out
    else:
        out.append(
            "The token task was never seen waiting, while the control waited "
            "as %s." % cq
        )

    lag = seconds_between(without.get("created"), with_token.get("created"))
    if lag is not None and lag < 0:
        out.append(
            "WARNING: the token task was created before the control, so the "
            "finishing order says nothing."
        )
        return out
    after = "" if lag is None else " %.0fs after the control" % lag

    ts, cs = with_token.get("status"), without.get("status")
    waited = any(
        v.get("position") or v.get("position_last") for v in (with_token, without)
    )
    if ts == "COMPLETED" and not waited:
        # the queue labels survive completion, but a position is only ever
        # reported while a task waits. Neither had one, so neither waited,
        # and finishing order on an idle device is submission order
        took = seconds_between(with_token.get("created"), with_token.get("ended"))
        out.append(
            "NO CONTENTION. Neither task was ever seen waiting%s, so there "
            "was no queue to jump and the finishing order says nothing. The "
            "queue labels above are still the service's own. Rerun against a "
            "device with a backlog."
            % ("" if took is None else " and the token task ran in %.1fs" % took)
        )
        return out
    if ts in ("FAILED", "CANCELLED"):
        out.append(
            "The token task %s, so it never ran and the order says nothing. "
            "Check the task's failure reason in the console." % ts
        )
        return out
    te = with_token.get("ended")
    # a cancelled or failed control has an end time but never ran, so it
    # counts as still waiting, not as finished
    ce = without.get("ended") if cs not in ("FAILED", "CANCELLED") else None
    if te and ce:
        gap = seconds_between(te, ce)
        if gap > 0:
            out.append(
                "SERVED FIRST. Submitted%s and finished %.0fs before it." % (after, gap)
            )
        else:
            out.append(
                "SERVED IN ORDER. Submitted%s and finished %.0fs after it, so "
                "the token bought no place." % (after, -gap)
            )
    elif te:
        pos = without.get("position_last")
        where = " at position %s" % pos if pos else ""
        if cs == "CANCELLED":
            tail = "still waiting%s, and was cancelled afterwards" % where
        else:
            tail = "still %s%s" % (cs or "waiting", where)
        out.append(
            "SERVED FIRST. Submitted%s and finished while the control was %s."
            % (after, tail)
        )
    else:
        out.append(
            "The token task did not finish while the hold was up (last seen "
            "%s), so the order is unknown. Raise --max-seconds."
            % (with_token.get("status") or "unknown")
        )
    return out


def estimate(device, shots, filler, filler_shots, max_seconds):
    """What the run will cost, before anything is created."""
    per = cost(device, shots)
    if per is None:
        return None
    total = 2 * per
    if filler:
        total += filler * (cost(device, filler_shots) or 0)
    total += HOLD_PER_HOUR * max_seconds / 3600.0
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=SV1)
    ap.add_argument("--shots", type=int, default=1)
    ap.add_argument(
        "--max-seconds",
        type=int,
        default=900,
        help="how long the hold may last. The token task has to finish inside "
        "it, so give a slow device half an hour. A mistake stops billing here",
    )
    ap.add_argument(
        "--follow-seconds",
        type=int,
        default=None,
        help="after the token task finishes and the hold is released, keep "
        "watching the control for this long so its finish time is on record. "
        "Default is --max-seconds",
    )
    ap.add_argument(
        "--cancel-control",
        action="store_true",
        help="cancel the control once the token task has finished instead of "
        "waiting for it. Saves its shots, loses its finish time",
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
        "Costs the task fee each, and they are cancelled afterwards. Only "
        "works on a device slow enough to queue: Cepheus ran six 500 shot "
        "tasks in under a second, so there it buys nothing",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="run even if the device is shut or has nothing queued",
    )
    ap.add_argument(
        "--inspect",
        nargs="+",
        metavar="TASK_ARN",
        help="print what the service says about these tasks and stop. Free. "
        "How long a device took to serve a task says whether a queue on it "
        "is real, and the queue label is still on a finished task",
    )
    ap.add_argument(
        "--pace",
        type=int,
        default=0,
        metavar="N",
        help="submit N plain tasks with no hold, at --filler-shots, follow "
        "them to the end and report how long each took and whether they ran "
        "one at a time. Says whether filler can build a queue here, for the "
        "task fee and shots each",
    )
    ap.add_argument(
        "--check-program",
        action="store_true",
        help="run the task program on the local simulator and stop. Free, "
        "and the analog simulator checks Aquila's limits, so a refusal "
        "here is one that would otherwise come after the hold is billing",
    )
    ap.add_argument(
        "--log",
        default="probe-hold.log",
        metavar="PATH",
        help="append everything printed, including the backend's progress "
        "and any traceback, to this file. Empty to disable",
    )
    args = ap.parse_args()

    if args.log:
        log_to(args.log)

    if args.survey:
        show_survey(args.shots)
        return
    if args.inspect:
        os.environ["AWS_DEFAULT_REGION"] = region_for(args.inspect[0])
        inspect(boto3.client("braket"), args.inspect)
        return
    if args.check_program:
        check_program(args.device)
        print("the program for %s runs on the local simulator" % args.device)
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
        "max_seconds": args.max_seconds,
    }

    b = BraketBackend()
    print(b.credential_note)
    print("device:", args.device)

    # A shut device or an empty queue means the run cannot show anything, and
    # finding that out after provisioning wastes time and money.
    q0 = queue_depth(args.device)
    print("device queues:", q0)
    d = None
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
        if q0["normal"] == 0 and not (args.make_queue or args.pace):
            raise SystemExit(
                "nothing is waiting on this device, so a priority comparison "
                "would say nothing. Pick a busier one, pass --make-queue N to "
                "build a backlog, or pass --force."
            )
        total = estimate(
            d, args.shots, args.make_queue, args.filler_shots, args.max_seconds
        )
        if total is not None and not args.pace:
            print("this run costs about $%.2f if the hold runs its full time" % total)

    if args.pace:
        os.environ["AWS_DEFAULT_REGION"] = region_for(args.device)
        per = cost(d, args.filler_shots) if d is not None else None
        if per is not None:
            print("this costs about $%.2f" % (per * args.pace))
        rows = pace(
            boto3.client("braket"),
            args.device,
            AwsSession().default_bucket(),
            args.pace,
            args.filler_shots,
            sleep=time.sleep,
            clock=time.time,
        )
        lines = pace_report(rows)
        for line in lines:
            print(line)
        run.update({"pace": rows, "verdict": lines})
        run["ended"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record(args.record, run)
        print("recorded to %s" % args.record)
        return

    arn = None
    filler = []
    # the device ARN names the region. The backend sets this too, but only
    # once the hold is opened, and the client and bucket are made before that
    os.environ["AWS_DEFAULT_REGION"] = region_for(args.device)
    braket = boto3.client("braket")
    bucket = AwsSession().default_bucket()
    views = {}
    control = with_token = None

    def say(a, seen):
        who = "token  " if a == with_token else "control"
        where = ""
        if seen["priority"]:
            where = " %s queue" % seen["priority"]
            if seen["position"]:
                where += " position %s" % seen["position"]
        print("  %s %-10s%s" % (who, seen["status"], where), flush=True)

    try:
        arn = b.open_session(opts)
        print("hybrid job:", arn)
        ok, why = b.wait_for_priority(opts)
        print(why)
        if not ok:
            raise SystemExit("never got the hold, nothing submitted, nothing spent")

        # the container gives up the hold this long after it published the
        # token, and everything the token task needs has to happen before then
        hold_until = time.time() + args.max_seconds - 30
        run["hold_job"] = arn
        run["hold_ready_after_s"] = getattr(b, "_hold_ready_after", None)
        token = b.session_id(arn)
        print("token:", token[:12], "...")
        print("job state at submit time:", b._hold_job.state())

        # Build the backlog now, not before the hold. Taking the hold costs a
        # couple of minutes, and a fast device empties in that time.
        if args.make_queue:
            filler = make_queue(
                braket, args.device, bucket, args.make_queue, args.filler_shots
            )
            run["filler_arns"] = filler
            print("queued %d filler tasks" % len(filler))
            for a in filler:
                print("  filler:     ", a)

        # control first, so the token task has something to overtake.
        # Finishing first after being submitted first would prove nothing.
        control = submit(braket, args.device, bucket, None, args.shots)
        with_token = submit(braket, args.device, bucket, token, args.shots)
        q1 = queue_depth(args.device)
        print("\ncontrol:    ", control)
        print("with token: ", with_token)
        print("device queues after both (these lag):", q1)

        print(
            "\nwatching until the token task finishes, hold up for %ds"
            % (hold_until - time.time())
        )
        follow(
            braket,
            [control, with_token],
            timeout=hold_until - time.time(),
            done=lambda v: v[with_token].get("status") in TERMINAL,
            views=views,
            say=say,
            sleep=time.sleep,
            clock=time.time,
        )
        q2 = queue_depth(args.device)
        run["queues"] = {"before": q0, "after_submit": q1, "after_token": q2}

        # the hold has done its work, stop paying for it before waiting on
        # the control
        print("\nreleasing the hold")
        b.close_session()
        arn = None

        if views[control].get("status") not in TERMINAL:
            if args.cancel_control:
                print("cancelling the control")
                drain_queue(braket, [control])
                # one more look, so the record says cancelled and not queued
                follow(
                    braket,
                    [control],
                    timeout=0,
                    views=views,
                    say=say,
                    sleep=time.sleep,
                    clock=time.time,
                )
            else:
                left = (
                    args.max_seconds
                    if args.follow_seconds is None
                    else args.follow_seconds
                )
                print("watching the control for up to %ds more" % left)
                follow(
                    braket,
                    [control],
                    timeout=left,
                    views=views,
                    say=say,
                    sleep=time.sleep,
                    clock=time.time,
                )

        a, c = views[with_token], views[control]
        print("\n  %-10s %-30s %s" % ("", "with token", "control"))
        for k in ("status", "job", "priority", "position", "created", "ended"):
            print("  %-10s %-30s %s" % (k, a.get(k), c.get(k)))

        run.update(
            {
                "with_token": {"arn": with_token, **a},
                "without": {"arn": control, **c},
            }
        )
        lines = verdict(a, c, args.device)
        run["verdict"] = lines
        print()
        for line in lines:
            print(line)

        # which physical qubits ran, so the result can be read against the
        # calibration for those edges
        run["with_token"]["qubits"] = used_qubits(with_token)
        run["without"]["qubits"] = used_qubits(control)
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
