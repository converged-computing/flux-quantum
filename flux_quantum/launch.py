#!/usr/bin/env python3
##############################################################
# Build and submit the quantum SCOUT job.
#
# The scout requests a small classical foothold AND the selected vendor's
# quantum device from the resource graph, in ONE slot:
#
#     slot -> [ core , qvendor_<vendor> -> qpu ]
#
# so fluxion co-allocates a core and a qpu for it. THIS is the coschedule match
# against the quantum vertices modeled in the graph (see `flux inject` / the
# add-subgraph helper). When the scout runs it opens the vendor session (the
# backend may be mocked) and unholds the paired, larger classical job.
#
# The mock replaces only the vendor API/session -- the graph match is real: if
# qvendor_<vendor> -> qpu is not in the graph, the scout is unsatisfiable and
# never runs, exactly as intended.
##############################################################
import argparse
import json
import os


def build_scout_jobspec(vendor, rendezvous, hold_job, ncores=1, duration=0,
                        scout_path=None, session=None):
    """Return a v1 jobspec dict for the scout.

    Requests a classical core AND the vendor's quantum device as TWO top-level
    resources -- a node-level slot (node->slot->core) AND a separate root-level
    qvendor_<vendor> -> qpu. A qpu is a root-level device (a sibling of node,
    like an ssd), so it lives in a different graph subtree than the node's cores;
    fluxion cannot place ONE slot spanning both subtrees (that is infeasible and
    returns EBUSY). Two top-level resources is the proven issue1284 pattern.
    """
    if scout_path is None:
        scout_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "scout.py")
    command = ["flux", "python", scout_path,
               "--job", str(hold_job),
               "--rendezvous", rendezvous,
               "--vendor", vendor]
    if session:
        command += ["--session", session]
    return {
        "version": 1,
        "resources": [
            {
                "type": "node",
                "count": 1,
                "with": [
                    {
                        "type": "slot",
                        "count": 1,
                        "label": "scout",
                        "with": [{"type": "core", "count": ncores}],
                    }
                ],
            },
            {
                "type": "qvendor_{}".format(vendor),
                "count": 1,
                "with": [{"type": "qpu", "count": 1}],
            },
        ],
        "attributes": {"system": {"duration": duration}},
        "tasks": [
            {"command": command, "slot": "scout", "count": {"per_slot": 1}}
        ],
    }


def submit_scout(handle, vendor, rendezvous, hold_job, ncores=1, duration=0,
                 session=None):
    """Submit the scout job; return its flux JobID."""
    from flux.job import submit
    jobspec = build_scout_jobspec(vendor, rendezvous, hold_job, ncores, duration,
                                  session=session)
    return submit(handle, json.dumps(jobspec))


def main():
    ap = argparse.ArgumentParser(
        prog="quantum-scout-launch",
        description="submit the scout job (requests core + qvendor_<v> -> qpu)")
    ap.add_argument("--job", required=True,
                    help="classical (main) job id the scout will unhold")
    ap.add_argument("--rendezvous", required=True,
                    help="shared, user-owned rendezvous directory")
    ap.add_argument("--vendor", required=True,
                    help="quantum vendor; its qvendor_<vendor> -> qpu is matched")
    ap.add_argument("--cores", type=int, default=1,
                    help="classical foothold cores for the scout (default 1)")
    ap.add_argument("--duration", type=int, default=0,
                    help="scout duration seconds (0 = scheduler default)")
    ap.add_argument("--session",
                    help="fixed session id for scout.py (testing; else mocked)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the scout jobspec and exit (no flux, no submit)")
    args = ap.parse_args()

    jobspec = build_scout_jobspec(args.vendor, args.rendezvous, args.job,
                                  args.cores, args.duration, session=args.session)
    if args.dry_run:
        print(json.dumps(jobspec, indent=2))
        return

    import flux
    from flux.job import submit
    h = flux.Flux()
    print(submit(h, json.dumps(jobspec)))


if __name__ == "__main__":
    main()
