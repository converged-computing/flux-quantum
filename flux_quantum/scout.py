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
import errno
import json
import signal
import sys

from flux_quantum.backends import BackendError, get_backend
from flux_quantum.keys import SESSION_KEY  # noqa: F401  (re-exported)

# the flux bindings are only importable under flux python. Every flux call
# takes an injectable so the unit tests can run without them.
try:
    import flux
    from flux.job import JobID, event_watch_async
    from flux.job import cancel as flux_cancel
except ImportError:
    flux = JobID = event_watch_async = flux_cancel = None


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
    """Block until the job reaches clean. A failed job still reaches clean,
    and the session is closed either way.

    SIGTERM while waiting unwinds with SystemExit so the caller's finally
    closes the session. The bindings' synchronous wait blocks inside the
    reactor, where a Python signal handler never gets to run, and a
    cancelled scout sat there until flux killed it with the session still
    open. So the watch runs in the reactor with a signal watcher beside it.
    """
    if waiter is not None:
        waiter(handle, jobid, "clean", raiseJobException=False)
        return

    outcome = {}

    def on_event(future, *_):
        try:
            event = future.get_event()
        except Exception as e:  # the eventlog is gone, or the job is unknown
            outcome["error"] = e
            handle.reactor_stop()
            return
        if event is None or event.name == "clean":
            outcome["done"] = True
            handle.reactor_stop()

    def on_signal(h, _watcher, signum, _args):
        outcome["signal"] = signum
        h.reactor_stop()

    future = event_watch_async(handle, jobid)
    future.then(on_event)
    watchers = []
    for sig in (signal.SIGTERM, signal.SIGALRM):
        watcher = handle.signal_watcher_create(sig, on_signal)
        watcher.start()
        watchers.append(watcher)
    try:
        handle.reactor_run()
    finally:
        for watcher in watchers:
            watcher.stop()
        # the reactor's handler is gone with the watcher, so the Python one
        # covers the close that follows
        _install_signal_handlers()
        try:
            future.cancel()
        except Exception:
            pass
    if "signal" in outcome:
        raise Signalled("quantum-scout: received signal {}".format(outcome["signal"]))
    if "error" in outcome:
        raise outcome["error"]


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


class Signalled(SystemExit):
    """Raised by the signal handlers, so an unwind can be told from an exit
    the scout chose. SIGALRM is what flux sends when the walltime runs out."""


# what flux and a user send to end a job, in the order they are tried
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)


def _install_signal_handlers():
    """Unwind on SIGTERM, SIGINT and SIGALRM so the session still gets closed
    and a held job that was never released is cancelled."""

    def _die(signum, frame):
        raise Signalled(
            "quantum-scout: received signal {} ({})".format(
                signum, signal.Signals(signum).name
            )
        )

    for sig in SIGNALS:
        try:
            signal.signal(sig, _die)
        except (ValueError, OSError):
            pass


def _release(handle, jobid):
    handle.rpc("sched-fluxion-qmanager.release", {"id": jobid}).get()


def run(
    handle,
    jobid,
    vendor,
    backend,
    opts,
    session=None,
    no_wait=False,
    cancel=None,
    release=None,
    post=None,
    waiter=None,
):
    """Open the session, hand it over, release the held job, hold the qpu
    until the classical is done, close the session.

    Whatever ends this early, a vendor refusal, a failed RPC, or a signal,
    including the SIGALRM flux sends at the walltime, leaves nothing behind:
    the session is closed if it was opened, and the held job is cancelled if
    it was never released. A classical job that is never released would
    otherwise wait forever, and a session nobody uses would still bill.

    The flux calls are injectable so this runs in the unit tests.
    """
    cancel = cancel or flux_cancel
    release = release or _release
    post = post or post_session
    released = False
    opened = None

    def close():
        nonlocal opened
        if backend is None or opened is None:
            return
        try:
            backend.close_session(opened)
            print("quantum-scout: closed {} session {}".format(vendor, opened))
        except Exception as e:  # do not mask an earlier failure
            print(
                "quantum-scout: WARNING failed to close {} session {}: {}".format(
                    vendor, opened, e
                ),
                file=sys.stderr,
            )
        opened = None

    try:
        if session is None:
            try:
                session = backend.open_session(opts)
            except Exception as e:
                abort_held(
                    handle,
                    jobid,
                    "opening {} session failed: {}".format(vendor, e),
                    cancel,
                )
            opened = session

            # opening is not the same as having the device, so wait until it
            # is ours before letting the classical job start
            try:
                ok, why = backend.wait_for_priority(opts)
            except Exception as e:
                close()
                abort_held(
                    handle,
                    jobid,
                    "waiting for {} priority failed: {}".format(vendor, e),
                    cancel,
                )
            if not ok:
                close()
                abort_held(
                    handle,
                    jobid,
                    "{} never became ours: {}".format(vendor, why),
                    cancel,
                )
            print("quantum-scout: {}".format(why))
            # what the classical job needs may only exist once the device is
            # held. Braket publishes its token after the hybrid job starts.
            session = backend.session_id(session)

        # must land before the release, or the job could start with no session
        try:
            post(handle, jobid, session)
        except Exception as e:
            abort_held(
                handle,
                jobid,
                "could not post the session to job {}: {}".format(jobid, e),
                cancel,
            )

        try:
            release(handle, jobid)
        except OSError as e:
            if e.errno != errno.EINVAL:
                abort_held(
                    handle,
                    jobid,
                    "release failed for job {}: {}".format(jobid, e),
                    cancel,
                )
            # EINVAL means the job is no longer pending: the queue policy did
            # not hold it, so it was scheduled already. The session is on its
            # eventlog, so it runs with it. Cancelling it would kill work that
            # is using the session, so carry on and hold the qpu instead.
            print(
                "quantum-scout: WARNING job {} was not held, it is past pending "
                "already. Is the qmanager queue-policy coschedule?".format(jobid),
                file=sys.stderr,
            )
        except Exception as e:
            abort_held(
                handle, jobid, "release failed for job {}: {}".format(jobid, e), cancel
            )
        released = True
        print(
            "quantum-scout: vendor={} session={} unheld job={}".format(
                vendor, session, jobid
            )
        )

        if no_wait:
            print("quantum-scout: --no-wait, not holding the session")
        else:
            wait_for_job(handle, jobid, waiter=waiter)
            print("quantum-scout: classical job {} finished".format(jobid))
    except Signalled as e:
        if not released:
            # cut short before the handoff, so the classical would never start
            try:
                cancel(handle, jobid, str(e))
                print(
                    "quantum-scout: cancelled held job {}".format(jobid),
                    file=sys.stderr,
                )
            except Exception as ex:
                print(
                    "quantum-scout: WARNING could not cancel held job {}: {}".format(
                        jobid, ex
                    ),
                    file=sys.stderr,
                )
        raise
    finally:
        close()


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

    # what the scout says has to survive a kill, so no block buffering
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    if flux is None:
        sys.exit(
            "quantum-scout: the flux bindings are not importable, run under flux python"
        )

    jobid = int(JobID(args.job))
    h = flux.Flux()

    # --session bypasses the backend for testing
    backend = None
    if not args.session:
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

    # From here a signal unwinds through run, which closes whatever is open
    # and cancels the held job if it was never released. That covers the
    # SIGALRM flux sends at the walltime, so a scout that runs out of time
    # while waiting for the vendor cleans up the same way as one that gives up.
    _install_signal_handlers()
    run(
        h,
        jobid,
        args.vendor,
        backend,
        json.loads(args.options),
        session=args.session,
        no_wait=args.no_wait,
    )


if __name__ == "__main__":
    main()
