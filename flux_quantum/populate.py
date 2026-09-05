##############################################################
# Copyright 2024 Lawrence Livermore National Security, LLC
# (c.f. AUTHORS, NOTICE.LLNS, COPYING)
#
# SPDX-License-Identifier: LGPL-3.0
##############################################################

"""Add vendor devices to the fluxion graph once, at startup.

This has to happen before any job holds resources. Growing the graph while jobs
are allocated corrupts fluxion, every later free fails with
planner_multi_rem_span returned -1, and the instance stops scheduling. Doing it
at startup means the first quantum submit never has to grow anything.

    flux python -m flux_quantum.populate ibm braket mock

Run it with flux python, not the system python. The flux bindings are installed
for whichever interpreter flux was built against, so a console script on the
default python cannot import them.
"""

import argparse
import sys

import flux

from . import graph


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="flux-quantum-populate",
        description="add vendor devices to the fluxion graph, once, at startup",
    )
    ap.add_argument("vendors", nargs="+", help="vendor names, e.g. ibm braket mock")
    ap.add_argument(
        "--qpus",
        type=int,
        default=1,
        help="qpu vertices per vendor. This is the concurrency limit, the qpu "
        "is matched exclusively so it caps how many pairs run at once",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="grow the graph even with jobs allocated. This corrupts fluxion, "
        "only use it if you know the instance is idle",
    )
    args = ap.parse_args(argv)

    handle = flux.Flux()
    try:
        added = graph.populate(handle, args.vendors, qpus=args.qpus, force=args.force)
    except Exception as exc:
        sys.exit("flux-quantum-populate: {}".format(exc))

    if added:
        print("added {}".format(", ".join(sorted(added))))
    else:
        print("nothing to add, every vendor is already in the graph")
    return 0


if __name__ == "__main__":
    sys.exit(main())
