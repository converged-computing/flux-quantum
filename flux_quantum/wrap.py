#!/usr/bin/env python3
##############################################################
# quantum-wrap: startup wrapper for the classical (reservation) job. Blocks
# until the scout deposits the session id in the rendezvous file keyed by THIS
# job's id, exports it as QUANTUM_SESSION_ID, removes the file, then execs the
# real work. Because the job is released only after the scout writes the file,
# the wait normally returns immediately; the poll is a safety margin.
#
#   quantum-wrap --rendezvous DIR -- real-program [args...]
##############################################################
import argparse
import os
import sys
import time


def main():
    ap = argparse.ArgumentParser(prog="quantum-wrap")
    ap.add_argument("--rendezvous", required=True)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    from flux.job import JobID

    fjid = os.environ.get("FLUX_JOB_ID")
    if not fjid:
        sys.exit("quantum-wrap: FLUX_JOB_ID not set (not running as a Flux job?)")
    key = str(int(JobID(fjid)))
    path = os.path.join(args.rendezvous, key)

    deadline = time.time() + args.timeout
    while not os.path.exists(path):
        if time.time() > deadline:
            sys.exit("quantum-wrap: timed out waiting for session at {}".format(path))
        time.sleep(0.2)

    with open(path) as f:
        session = f.read().strip()
    try:
        os.remove(path)
    except OSError:
        pass

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
        "quantum-wrap: QUANTUM_SESSION_ID={} -> exec {}\n".format(session, " ".join(cmd)))
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
