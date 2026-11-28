#!/usr/bin/env python3
"""List, and with --yes cancel, every unfinished IBM job and the sessions
they belong to. For when something was left running.

    python3 ibm-cancel.py           list what is pending, change nothing
    python3 ibm-cancel.py --yes     cancel those jobs and close their sessions

Credentials come from the same variables ibm-run uses: QISKIT_IBM_TOKEN and
QISKIT_IBM_INSTANCE, or IBM_CLOUD_TOKEN and IBM_CLOUD_CRN.

IBM bills a dedicated session from the moment its first job starts until it
is closed or its last job ends, so cancelling the jobs and the session is
what stops the meter. A session with no job running costs nothing, but it is
closed here too so nothing can start in it later.
"""

import argparse
import os
import sys

from qiskit_ibm_runtime import QiskitRuntimeService, Session


def say(msg):
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--yes", action="store_true", help="actually cancel, otherwise only list")
    ap.add_argument("--limit", type=int, default=20, help="how many recent jobs to look at, 20")
    args = ap.parse_args()

    token = os.environ.get("QISKIT_IBM_TOKEN") or os.environ.get("IBM_CLOUD_TOKEN")
    instance = os.environ.get("QISKIT_IBM_INSTANCE") or os.environ.get("IBM_CLOUD_CRN")
    if not token or not instance:
        sys.exit("set QISKIT_IBM_TOKEN and QISKIT_IBM_INSTANCE, or IBM_CLOUD_TOKEN and IBM_CLOUD_CRN")
    say("connecting to IBM")
    svc = QiskitRuntimeService(channel="ibm_quantum_platform", token=token, instance=instance)

    # pending means queued or running, which is everything that can still bill
    say("asking IBM for pending jobs")
    pending = list(svc.jobs(limit=args.limit, pending=True))
    say("{} pending job(s)".format(len(pending)))
    sessions = {}
    for job in pending:
        sid = getattr(job, "session_id", None)
        say("  job {}  {}  session={}".format(job.job_id(), job.status(), sid))
        if sid:
            sessions[sid] = None

    # sessions of recent jobs, each looked up once
    say("checking the sessions of the last {} jobs".format(args.limit))
    for job in svc.jobs(limit=args.limit):
        sid = getattr(job, "session_id", None)
        if sid and sid not in sessions:
            sessions[sid] = None
    details = {}
    for sid in sessions:
        try:
            details[sid] = Session.from_id(sid, service=svc).details() or {}
        except Exception as e:
            details[sid] = {"error": str(e)}

    # IBM keeps accepting_jobs True on a session it has already closed, so the
    # state is what says whether it can still bill
    def is_open(d):
        state = str(d.get("state") or "").lower()
        return state not in ("closed", "completed", "cancelled", "canceled", "") and not d.get("error")

    open_ids = [sid for sid in sessions if is_open(details[sid])]
    say("{} session(s) seen, {} still open".format(len(sessions), len(open_ids)))
    for sid in sessions:
        d = details[sid]
        say("  session {}  backend={}  state={}  usage_s={}{}".format(
            sid, d.get("backend_name"), d.get("state"), d.get("usage_time"),
            "  <- OPEN" if sid in open_ids else ""))

    if not args.yes:
        say("\nnothing changed. Run again with --yes to cancel the pending jobs and open sessions above.")
        return

    for job in pending:
        try:
            job.cancel()
            print("cancelled job {}".format(job.job_id()))
        except Exception as e:
            print("could not cancel job {}: {}".format(job.job_id(), e))
    for sid in open_ids:
        session = Session.from_id(sid, service=svc)
        # cancel ends the session and drops anything still queued in it.
        # close only stops new jobs, so it is the fallback
        for verb in ("cancel", "close"):
            fn = getattr(session, verb, None)
            if fn is None:
                continue
            try:
                fn()
                print("{} session {}".format("cancelled" if verb == "cancel" else "closed", sid))
                break
            except Exception as e:
                print("could not {} session {}: {}".format(verb, sid, e))


if __name__ == "__main__":
    main()
