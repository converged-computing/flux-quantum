#!/bin/false
##############################################################
# flux-inject: add qpu vertices to a Flux resource set (R).
#
# Adds N qpu vertices as direct children of the cluster root (sibling to
# racks/nodes) -- a qpu is a rack-level device and does not require a node.
# Accepts EITHER a raw R (from `flux kvs get resource.R`) or an already
# encoded R (with a .scheduling key). If .scheduling is absent it is produced
# first via the same FluxionResourceGraphV1 encode that `flux ion-R encode`
# uses, so `flux kvs get resource.R | flux inject` works directly.
#
# SPDX-License-Identifier: LGPL-3.0
##############################################################
import argparse
import sys
import json
import logging
import flux
from fluxion.resourcegraph.V1 import FluxionResourceGraphV1

LOGGER = logging.getLogger("flux-inject")


def ensure_scheduling(doc):
    #
    # If the R has no .scheduling key yet, encode it (same as
    # `flux ion-R encode`). Idempotent: a doc that already has .scheduling
    # is returned unchanged.
    #
    if "scheduling" not in doc:
        graph = FluxionResourceGraphV1(doc)
        doc["scheduling"] = graph.to_JSON()
    return doc


def add_qpu(doc, count, scheduling_only):
    graph = doc["graph"] if scheduling_only else doc["scheduling"]["graph"]
    nodes = graph["nodes"]
    edges = graph["edges"]
    targets = {e["target"] for e in edges}
    roots = [n for n in nodes if n["id"] not in targets]
    if len(roots) != 1:
        raise ValueError(
            "expected exactly 1 root vertex, found {}".format(len(roots))
        )
    root_id = roots[0]["id"]
    root_path = roots[0]["metadata"]["paths"]["containment"]
    next_id = max(int(n["id"]) for n in nodes) + 1
    for i in range(count):
        name = "qpu{}".format(i)
        nodes.append(
            {
                "id": str(next_id),
                "metadata": {
                    "type": "qpu",
                    "id": i,
                    "rank": -1,
                    "paths": {"containment": "{}/{}".format(root_path, name)},
                },
            }
        )
        edges.append({"source": root_id, "target": str(next_id)})
        next_id += 1
    return doc


@flux.util.CLIMain(LOGGER)
def main():
    parser = argparse.ArgumentParser(
        prog="flux-inject", formatter_class=flux.util.help_formatter()
    )
    parser.add_argument(
        "--count", type=int, default=1,
        help="number of qpu vertices to add at the cluster root (default 1)",
    )
    parser.add_argument(
        "--input", dest="ifn", metavar="FILENAME",
        help="read R from FILENAME instead of stdin",
    )
    parser.add_argument(
        "--output", dest="ofn", metavar="FILENAME",
        help="write R to FILENAME instead of stdout",
    )
    parser.add_argument(
        "--scheduling-only", action="store_true",
        help="input is a bare scheduling graph ({graph:...}), not a full R",
    )
    args = parser.parse_args()

    infile = open(args.ifn, "r") if args.ifn else sys.stdin
    outfile = open(args.ofn, "w") if args.ofn else sys.stdout
    try:
        doc = json.loads(infile.read())
        if not args.scheduling_only:
            doc = ensure_scheduling(doc)
        doc = add_qpu(doc, args.count, args.scheduling_only)
        print(json.dumps(doc), file=outfile)
    finally:
        if args.ofn:
            outfile.close()
        if args.ifn:
            infile.close()


if __name__ == "__main__":
    main()
