#!/usr/bin/env python3
# Build the scout jobspec. The scout asks for a small classical foothold and
# the vendor device, so fluxion co-allocates a core and a qpu. Even with the
# mock the graph match is real, so a missing qdevice makes it unsatisfiable.
import argparse
import json
import os

from flux_quantum import qresource


def build_scout_jobspec(
    vendor,
    hold_job,
    ncores=1,
    duration=0,
    scout_path=None,
    session=None,
    live_graph=None,
    options=None,
):
    """Return a v1 jobspec dict for the scout.

    The foothold and the qdevice are two top level resources rather than one
    slot. The qdevice is a sibling of rack, so it sits in a different subtree
    than the node cores and fluxion cannot place one slot across both.
    """
    if scout_path is None:
        scout_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "scout.py"
        )
    command = ["flux", "python", scout_path, "--job", str(hold_job), "--vendor", vendor]
    if session:
        command += ["--session", session]
    # options from scout_options travel to the scout as JSON, where
    # open_session consumes them
    if options:
        command += ["--options", json.dumps(options)]
    # classical foothold, derived from the live graph so the node to core path
    # matches the real hierarchy, socket or not. Falls back to node->slot->core
    # when no graph is available, as in unit tests.
    if live_graph is not None:
        classical = qresource.classical_resource(
            live_graph, ncores=ncores, label="scout"
        )
    else:
        classical = {
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
        }
    return {
        "version": 1,
        "resources": [
            classical,
            # quantum device qdevice_<vendor> -> qpu, exclusive so it lands in
            # R. One shape source, shared with the graph populator.
            qresource.jobspec_resource(vendor),
        ],
        "attributes": {"system": {"duration": duration}},
        "tasks": [{"command": command, "slot": "scout", "count": {"per_slot": 1}}],
    }


def main():
    ap = argparse.ArgumentParser(
        prog="quantum-scout-launch",
        description="submit the scout job (requests core + qdevice_<v> -> qpu)",
    )
    ap.add_argument(
        "--job", required=True, help="classical (main) job id the scout will unhold"
    )
    ap.add_argument(
        "--vendor",
        required=True,
        help="quantum vendor; its qdevice_<vendor> -> qpu is matched",
    )
    ap.add_argument(
        "--cores",
        type=int,
        default=1,
        help="classical foothold cores for the scout (default 1)",
    )
    ap.add_argument(
        "--duration",
        type=int,
        default=0,
        help="scout duration seconds (0 = scheduler default)",
    )
    ap.add_argument(
        "--session", help="fixed session id for scout.py (testing; else mocked)"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the scout jobspec and exit (no flux, no submit)",
    )
    args = ap.parse_args()

    jobspec = build_scout_jobspec(
        args.vendor, args.job, args.cores, args.duration, session=args.session
    )
    if args.dry_run:
        print(json.dumps(jobspec, indent=2))
        return

    import flux
    from flux.job import submit

    h = flux.Flux()
    print(submit(h, json.dumps(jobspec)))


if __name__ == "__main__":
    main()
