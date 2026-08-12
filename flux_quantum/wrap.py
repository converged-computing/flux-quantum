#!/usr/bin/env python3
##############################################################
# quantum-wrap: startup wrapper for the classical (reservation) job. Reads the
# vendor session id from THIS job's own eventlog (posted as a memo by the scout
# before it released us), exports it as QUANTUM_SESSION_ID, then execs the real
# work.
#
#   quantum-wrap -- real-program [args...]
#
# No shared filesystem is involved: job-info.eventlog-watch is a FLUX_ROLE_USER
# service authorized against the job's owner uid, so an unprivileged user can
# read the eventlog of a job they own from whatever node they landed on.
#
# The scout posts the memo BEFORE the release RPC, so by the time this job is
# unheld and starts, the memo is already in the replayed eventlog. The timeout
# is only a guard against a scout that died between release and memo.
##############################################################
import argparse
import os
import signal
import sys

from flux_quantum.scout import SESSION_KEY


class _Timeout(Exception):
    pass


def read_session(handle, jobid, timeout=60.0, watcher=None):
    """Return the session id posted on this job's eventlog by the scout.

    `watcher` is injectable for testing; by default it is flux.job.event_watch,
    a generator that replays the existing eventlog and then follows it.
    """
    if watcher is None:
        from flux.job import event_watch as watcher

    def _alarm(signum, frame):
        raise _Timeout()

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        for event in watcher(handle, jobid):
            if event.name == "memo" and SESSION_KEY in event.context:
                return event.context[SESSION_KEY]
    except _Timeout:
        raise RuntimeError(
            "timed out after {}s waiting for the scout's session memo".format(timeout)
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    raise RuntimeError("job eventlog ended without a session memo")


def main():
    ap = argparse.ArgumentParser(prog="quantum-wrap")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    import flux
    from flux.job import JobID

    fjid = os.environ.get("FLUX_JOB_ID")
    if not fjid:
        sys.exit("quantum-wrap: FLUX_JOB_ID not set (not running as a Flux job?)")

    try:
        session = read_session(flux.Flux(), int(JobID(fjid)), timeout=args.timeout)
    except Exception as e:
        sys.exit("quantum-wrap: {}".format(e))

    os.environ["QUANTUM_SESSION_ID"] = session

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("quantum-wrap: QUANTUM_SESSION_ID={} (no command given)".format(session))
        return
    # stderr so it shows in the job's output without interfering with the
    # wrapped program's stdout; confirms the handoff reached this process.
    sys.stderr.write(
        "quantum-wrap: QUANTUM_SESSION_ID={} -> exec {}\n".format(
            session, " ".join(cmd)
        )
    )
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
