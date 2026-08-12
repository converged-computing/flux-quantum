#!/usr/bin/env python3
# quantum-wrap wraps the classical job. It reads the session id the scout put
# on our eventlog, exports it as QUANTUM_SESSION_ID, then execs the real work.
#
#   quantum-wrap -- real-program [args...]
#
# The memo is posted before the release, so it is already there when we start.
# The timeout only guards against a scout that died before memoing.
import argparse
import os
import signal
import sys

from flux_quantum.scout import SESSION_KEY


class _Timeout(Exception):
    pass


def read_session(handle, jobid, timeout=60.0, watcher=None):
    """Return the session id the scout put on the eventlog for this job."""
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
    # stderr so it shows in the job output without disturbing the stdout of
    # the wrapped program, and confirms the handoff reached this process
    sys.stderr.write(
        "quantum-wrap: QUANTUM_SESSION_ID={} -> exec {}\n".format(
            session, " ".join(cmd)
        )
    )
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
