#!/usr/bin/env python3
# quantum-scout opens a vendor session, hands it to the held classical job and
# releases it. Runs as the user, so the vendor credentials stay in this process.
#
# open session -> memo it to the classical -> release -> wait for the classical
# to finish -> close the session.
#
# We memo before releasing so the session is on the eventlog before the job can
# start, and we wait so the qpu stays allocated for as long as the vendor
# session is open.
import argparse
import json
import signal
import sys

from flux_quantum.backends import BackendError, get_backend
from flux_quantum.keys import SESSION_KEY  # noqa: F401  (re-exported)

# the flux bindings are only importable under flux python. Every flux call
# takes an injectable so the unit tests can run without them.
try:
    import flux
    from flux.job import JobID, event_wait
    from flux.job import cancel as flux_cancel
except ImportError:
    flux = JobID = event_wait = flux_cancel = None


def post_session(handle, jobid, session, rpc=None):
    """Memo the session id onto the classical job's eventlog.

    The memo RPC is authorized against the job owner, so no shared filesystem
    or owner privilege is needed. The released key is what qmanager reads
    after a restart so it does not hold the job a second time.
    """
    if rpc is None:
        rpc = handle.rpc
    memo = {SESSION_KEY: session, "released": 1}
    rpc("job-manager.memo", {"id": int(jobid), "memo": memo}).get()


def wait_for_job(handle, jobid, waiter=None):
    """Block until the job reaches clean."""
    waiter = waiter or event_wait
    # a failed job still reaches clean, and we close the session either way
    waiter(handle, jobid, "clean", raiseJobException=False)


def abort_held(handle, jobid, why, cancel=None):
    """Cancel the held classical and exit, so a failure before the release
    does not leave it parked forever."""
    cancel = cancel or flux_cancel
    try:
        cancel(handle, jobid, why)
        print("quantum-scout: cancelled held job {}".format(jobid), file=sys.stderr)
    except Exception as e:
        print(
            "quantum-scout: WARNING could not cancel held job {}: {}".format(jobid, e),
            file=sys.stderr,
        )
    sys.exit("quantum-scout: {}".format(why))


def _install_signal_handlers():
    """Unwind on SIGTERM/SIGINT so the session still gets closed."""

    def _die(signum, frame):
        raise SystemExit("quantum-scout: received signal {}".format(signum))

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _die)
        except (ValueError, OSError):
            pass


def main():
    ap = argparse.ArgumentParser(prog="quantum-scout")
    ap.add_argument(
        "--job", required=True, help="classical (reservation) job id to release"
    )
    ap.add_argument("--vendor", default="ibm", help="quantum vendor")
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
        help="exit after the release instead of holding the qpu for the "
        "classical job's lifetime (testing only, leaks the session)",
    )
    args = ap.parse_args()

    if flux is None:
        sys.exit(
            "quantum-scout: the flux bindings are not importable, run under flux python"
        )

    jobid = int(JobID(args.job))
    h = flux.Flux()

    # --session bypasses the backend for testing
    backend = None
    if args.session:
        session = args.session
    else:
        try:
            backend = get_backend(args.vendor)
        except BackendError as e:
            abort_held(h, jobid, str(e))
        if backend is None:
            abort_held(
                h,
                jobid,
                "no backend registered for vendor {}, set FLUX_QUANTUM_MOCK for "
                "the mock vendor".format(args.vendor),
            )
        opts = json.loads(args.options)
        try:
            session = backend.open_session(opts)
        except Exception as e:
            abort_held(h, jobid, "opening {} session failed: {}".format(args.vendor, e))

        # opening is not the same as having the device, so wait until it is ours
        # before letting the classical job start
        try:
            ok, why = backend.wait_for_priority(opts)
        except Exception as e:
            backend.close_session(session)
            abort_held(
                h, jobid, "waiting for {} priority failed: {}".format(args.vendor, e)
            )
        if not ok:
            backend.close_session(session)
            abort_held(h, jobid, "{} never became ours: {}".format(args.vendor, why))
        print("quantum-scout: {}".format(why))
        # what the classical job needs may only exist once the device is
        # held. Braket publishes its token after the hybrid job starts.
        session = backend.session_id(session)

    # must land before the release, or the job could start with no session
    try:
        post_session(h, jobid, session)
    except Exception as e:
        abort_held(
            h, jobid, "could not post the session to job {}: {}".format(args.job, e)
        )

    _install_signal_handlers()
    try:
        try:
            h.rpc("sched-fluxion-qmanager.release", {"id": jobid}).get()
        except Exception as e:
            sys.exit("quantum-scout: release failed for job {}: {}".format(args.job, e))

        print(
            "quantum-scout: vendor={} session={} unheld job={}".format(
                args.vendor, session, args.job
            )
        )

        if args.no_wait:
            print("quantum-scout: --no-wait, not holding the session")
        else:
            wait_for_job(h, jobid)
            print("quantum-scout: classical job {} finished".format(args.job))
    finally:
        if backend is not None:
            try:
                backend.close_session(session)
                print(
                    "quantum-scout: closed {} session {}".format(args.vendor, session)
                )
            except Exception as e:  # do not mask an earlier failure
                print(
                    "quantum-scout: WARNING failed to close {} session {}: {}".format(
                        args.vendor, session, e
                    ),
                    file=sys.stderr,
                )


if __name__ == "__main__":
    main()
