#!/usr/bin/env python3
##############################################################
# quantum-scout: open a quantum session for a HELD classical (reservation) job,
# hand the session id off on that job's EVENTLOG, then release the classical job
# so it allocates its reserved footprint and runs.
#
# Runs as the user, in userspace, holding the user's vendor credentials. The
# credential never leaves this process; only the resulting session id is handed
# off. First pass MOCKS the vendor session (see open_session); swap that for a
# real QRMI / Braket noop-submit that returns the backend session id.
#
# Lifecycle (this is what makes the co-allocation real):
#   acquire the vendor session -> memo it to the classical -> release the hold ->
#   WAIT for the classical to finish -> close the vendor session -> exit.
#
# The scout deliberately stays alive for the whole classical run. It holds the
# fluxion qpu allocation for exactly as long as it holds the vendor session, so
# the scheduler's view and the vendor's view agree. If the scout exited right
# after the release, fluxion would free the qpu while the vendor still considered
# it acquired, and the vendor session would leak.
#
# Ordering matters: the memo is posted BEFORE the release RPC, so the session is
# already on the classical job's eventlog the moment it is released -- the reader
# never has to wait or poll.
#
# The handoff needs NO shared filesystem: job-manager.memo is a FLUX_ROLE_USER
# service authorized against the job's owner uid ("guests can only add a memo to
# their own jobs"), which is the same authorization the release RPC already
# requires. The scout and the classical are owned by the same user, so this works
# for an unprivileged user and across nodes.
##############################################################
import argparse
import sys

#: eventlog memo key carrying the vendor session id to the classical job
SESSION_KEY = "quantum_session"


def post_session(handle, jobid, session, rpc=None):
    """Post the vendor session id onto the classical job's eventlog as a memo.

    Uses job-manager.memo, which is FLUX_ROLE_USER and authorized against the
    job's owner uid -- no shared filesystem and no instance-owner privilege.
    """
    if rpc is None:
        rpc = handle.rpc
    rpc("job-manager.memo", {"id": int(jobid), "memo": {SESSION_KEY: session}}).get()


def wait_for_job(handle, jobid, waiter=None):
    """Block until the classical job reaches `clean` (finished, however it
    ended). Injectable for testing."""
    if waiter is None:
        from flux.job import event_wait as waiter
    # raiseJobException=False: a failed classical still reaches clean, and the
    # vendor session must be closed either way.
    waiter(handle, jobid, "clean", raiseJobException=False)


def _install_signal_handlers():
    """Turn SIGTERM/SIGINT into a normal unwind so the vendor session is still
    closed when flux cancels or times out the scout."""
    import signal

    def _die(signum, frame):
        raise SystemExit("quantum-scout: received signal {}".format(signum))

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _die)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass


def main():
    ap = argparse.ArgumentParser(prog="quantum-scout")
    ap.add_argument(
        "--job", required=True, help="classical (reservation) job id to release"
    )
    ap.add_argument(
        "--vendor", default="ibm", help="quantum vendor (decided by the jobtap plugin)"
    )
    ap.add_argument(
        "--options",
        default="{}",
        help="vendor-specific options as JSON (from the backend)",
    )
    ap.add_argument(
        "--session", help="use this session id instead of opening one (testing)"
    )
    ap.add_argument(
        "--no-wait",
        action="store_true",
        help="exit right after the release instead of holding the qpu "
        "allocation for the classical job's lifetime (testing only -- "
        "this leaks the vendor session)",
    )
    args = ap.parse_args()

    import json
    import flux
    from flux.job import JobID

    jobid = int(JobID(args.job))

    # 1. open the vendor session. The VENDOR's backend owns this logic and
    #    consumes the vendor-specific options; --session bypasses it for testing.
    backend = None
    if args.session:
        session = args.session
    else:
        try:
            from flux_quantum.backends import get_backend
        except Exception as e:
            sys.exit("quantum-scout: cannot import backends: {}".format(e))
        backend = get_backend(args.vendor)
        if backend is None:
            sys.exit(
                "quantum-scout: no backend registered for vendor '{}' "
                "(set FLUX_QUANTUM_MOCK for the mock vendor)".format(args.vendor)
            )
        try:
            session = backend.open_session(json.loads(args.options))
        except NotImplementedError as e:
            sys.exit("quantum-scout: {}".format(e))
        except Exception as e:
            sys.exit(
                "quantum-scout: opening {} session failed: {}".format(args.vendor, e)
            )

    h = flux.Flux()

    # 2. hand off on the classical job's eventlog. Must succeed before the
    #    release, otherwise the job could start with no session to read.
    try:
        post_session(h, jobid, session)
    except Exception as e:
        sys.exit(
            "quantum-scout: could not post the session to job {}: {}".format(
                args.job, e
            )
        )

    _install_signal_handlers()
    try:
        # 3. RELEASE the classical job so it allocates its reserved footprint
        #    and runs (sched-fluxion-qmanager.release; hold set at submit time).
        try:
            h.rpc("sched-fluxion-qmanager.release", {"id": jobid}).get()
        except Exception as e:
            sys.exit("quantum-scout: release failed for job {}: {}".format(args.job, e))

        print(
            "quantum-scout: vendor={} session={} unheld job={}".format(
                args.vendor, session, args.job
            )
        )

        # 4. HOLD the qpu allocation for as long as the classical runs, so the
        #    scheduler's view matches the vendor's.
        if args.no_wait:
            print("quantum-scout: --no-wait, not holding the session")
        else:
            wait_for_job(h, jobid)
            print("quantum-scout: classical job {} finished".format(args.job))
    finally:
        # 5. Always release the vendor session, on every exit path.
        if backend is not None:
            try:
                backend.close_session(session)
                print(
                    "quantum-scout: closed {} session {}".format(args.vendor, session)
                )
            except Exception as e:  # never mask an original failure
                print(
                    "quantum-scout: WARNING failed to close {} session {}: {}".format(
                        args.vendor, session, e
                    ),
                    file=sys.stderr,
                )


if __name__ == "__main__":
    main()
