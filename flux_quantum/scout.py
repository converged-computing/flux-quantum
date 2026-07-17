#!/usr/bin/env python3
##############################################################
# quantum-scout: open a quantum session for a HELD classical (reservation) job,
# hand the session id off via a user-owned rendezvous file, then unhold the
# classical job so it allocates its reserved footprint and runs.
#
# Runs as the user, in userspace, holding the user's vendor credentials. The
# credential never leaves this process; only the resulting session id is handed
# off. First pass MOCKS the vendor session (see open_session); swap that for a
# real QRMI / Braket noop-submit that returns the backend session id.
#
# Ordering matters: the session file is written BEFORE the unhold RPC, so the
# classical job always finds the session the moment it is released.
##############################################################
import argparse
import os
import sys
import time


def open_session(vendor):
    # MOCK. Real impl: using the user's credentials, submit a noop task to the
    # vendor (IBM via QRMI, AWS Braket, ...) to open a session and return its id.
    return "{}-session-{}-{}".format(vendor, int(time.time()), os.getpid())


def main():
    ap = argparse.ArgumentParser(prog="quantum-scout")
    ap.add_argument("--job", required=True,
                    help="classical (reservation) job id to unhold")
    ap.add_argument("--rendezvous", required=True,
                    help="shared, user-owned rendezvous directory")
    ap.add_argument("--vendor", default="ibm",
                    help="quantum vendor (decided by the jobtap plugin)")
    ap.add_argument("--session",
                    help="use this session id instead of opening one (testing)")
    args = ap.parse_args()

    import flux
    from flux.job import JobID

    jobid = int(JobID(args.job))

    # 1. open the session (holds user creds; mock for now)
    session = args.session if args.session else open_session(args.vendor)

    # 2. hand off via the user-owned rendezvous file, keyed by the classical job
    #    id. atomic write (tmp + rename) so the reader never sees a partial value.
    os.makedirs(args.rendezvous, exist_ok=True)
    dst = os.path.join(args.rendezvous, str(jobid))
    tmp = dst + ".tmp.{}".format(os.getpid())
    with open(tmp, "w") as f:
        f.write(session)
    os.rename(tmp, dst)

    # 3. unhold the classical job (our sched-fluxion-qmanager.hold RPC)
    h = flux.Flux()
    try:
        h.rpc("sched-fluxion-qmanager.hold",
              {"id": jobid, "hold": False}).get()
    except Exception as e:
        sys.exit("quantum-scout: unhold RPC failed for job {}: {}".format(args.job, e))

    print("quantum-scout: vendor={} session={} unheld job={}".format(
        args.vendor, session, args.job))


if __name__ == "__main__":
    main()
