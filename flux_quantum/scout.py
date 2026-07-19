#!/usr/bin/env python3
##############################################################
# quantum-scout: open a quantum session for a HELD classical (reservation) job,
# hand the session id off via a user-owned rendezvous file, then release the
# classical job so it allocates its reserved footprint and runs.
#
# Runs as the user, in userspace, holding the user's vendor credentials. The
# credential never leaves this process; only the resulting session id is handed
# off. First pass MOCKS the vendor session (see open_session); swap that for a
# real QRMI / Braket noop-submit that returns the backend session id.
#
# Ordering matters: the session file is written BEFORE the release RPC, so the
# classical job always finds the session the moment it is released.
##############################################################
import argparse
import os
import sys
import time


def main():
    ap = argparse.ArgumentParser(prog="quantum-scout")
    ap.add_argument("--job", required=True,
                    help="classical (reservation) job id to release")
    ap.add_argument("--rendezvous", required=True,
                    help="shared, user-owned rendezvous directory")
    ap.add_argument("--vendor", default="ibm",
                    help="quantum vendor (decided by the jobtap plugin)")
    ap.add_argument("--options", default="{}",
                    help="vendor-specific options as JSON (from the backend)")
    ap.add_argument("--session",
                    help="use this session id instead of opening one (testing)")
    args = ap.parse_args()

    import json
    import flux
    from flux.job import JobID

    jobid = int(JobID(args.job))

    # 1. open the vendor session. The VENDOR's backend owns this logic and
    #    consumes the vendor-specific options; --session bypasses it for testing.
    if args.session:
        session = args.session
    else:
        try:
            from flux_quantum.backends import get_backend
        except Exception as e:
            sys.exit("quantum-scout: cannot import backends: {}".format(e))
        backend = get_backend(args.vendor)
        if backend is None:
            sys.exit("quantum-scout: no backend registered for vendor '{}' "
                     "(set FLUX_QUANTUM_MOCK for the mock vendor)".format(args.vendor))
        try:
            session = backend.open_session(json.loads(args.options))
        except NotImplementedError as e:
            sys.exit("quantum-scout: {}".format(e))
        except Exception as e:
            sys.exit("quantum-scout: opening {} session failed: {}".format(args.vendor, e))

    # 2. hand off via the user-owned rendezvous file, keyed by the classical job
    #    id. atomic write (tmp + rename) so the reader never sees a partial value.
    os.makedirs(args.rendezvous, exist_ok=True)
    dst = os.path.join(args.rendezvous, str(jobid))
    tmp = dst + ".tmp.{}".format(os.getpid())
    with open(tmp, "w") as f:
        f.write(session)
    os.rename(tmp, dst)

    # 3. RELEASE the classical job so it allocates its reserved footprint and
    #    runs (sched-fluxion-qmanager.release; the hold was set at submit time).
    h = flux.Flux()
    try:
        h.rpc("sched-fluxion-qmanager.release", {"id": jobid}).get()
    except Exception as e:
        sys.exit("quantum-scout: release failed for job {}: {}".format(args.job, e))

    print("quantum-scout: vendor={} session={} unheld job={}".format(
        args.vendor, session, args.job))


if __name__ == "__main__":
    main()
