#!/usr/bin/env python3
##############################################################
# Build an add-subgraph payload that plants qvendor_* -> qpu subtrees under the
# LIVE graph root: each vendor vertex carries a qpu child so a job can request
# qvendor_<v> -> qpu and fluxion co-allocates it (the scout coschedule match).
# Also serves token-free registry / discovery (the qvendor_* vertex).
#
# fluxion's JGF reader (resource_reader_jgf) matches existing vertices by
# (containment path, rank) and requires only id + metadata.type +
# metadata.paths per vertex; apply_defaults fills the rest. So we copy the live
# root verbatim -- it matches the existing root (so it enters the edge vmap
# without being duplicated) -- and attach one marker per vendor with a new path
# and a containment edge.
#
#   flux ion-resource find --format=jgf status=up | sed -n '/^{/,$p' \
#     | subgraph.py --vendor mock --vendor ibm --output sub.json
#   flux ion-resource add-subgraph sub.json
#
# Pure dict manipulation -- no flux imports.
##############################################################
import argparse
import sys
import json


def get_graph(doc):
    if "graph" in doc:
        return doc["graph"]
    if "scheduling" in doc and "graph" in doc["scheduling"]:
        return doc["scheduling"]["graph"]
    sys.exit("subgraph.py: input has no graph")


def find_root(graph):
    targets = {e["target"] for e in graph["edges"]}
    roots = [n for n in graph["nodes"] if n["id"] not in targets]
    if len(roots) != 1:
        sys.exit("subgraph.py: expected exactly 1 root, found %d" % len(roots))
    return roots[0]


def main():
    p = argparse.ArgumentParser(prog="subgraph.py")
    p.add_argument("--vendor", action="append", default=[],
                   help="vendor name -> qvendor_<name> marker (repeatable)")
    p.add_argument("--qpus", type=int, default=1,
                   help="qpu vertices under each vendor (default 1)")
    p.add_argument("--input", dest="ifn", metavar="FILENAME")
    p.add_argument("--output", dest="ofn", metavar="FILENAME")
    args = p.parse_args()
    if not args.vendor:
        sys.exit("subgraph.py: give at least one --vendor")

    doc = json.loads((open(args.ifn) if args.ifn else sys.stdin).read())
    graph = get_graph(doc)
    root = find_root(graph)
    root_id = root["id"]
    root_path = root["metadata"]["paths"]["containment"]
    max_id = max(int(n["id"]) for n in graph["nodes"])

    nodes = [root]   # verbatim: matched by (path, rank); enters vmap, not dup'd
    edges = []
    next_id = max_id + 1
    for v in args.vendor:
        vid = str(next_id); next_id += 1
        vtype = "qvendor_%s" % v
        vname = vtype + "0"
        vpath = "%s/%s" % (root_path, vname)
        nodes.append({
            "id": vid,
            "metadata": {
                "type": vtype,
                "rank": -1,
                "paths": {"containment": vpath},
                "properties": {v: ""},
            },
        })
        edges.append({"source": root_id, "target": vid,
                      "metadata": {"subsystem": "containment"}})
        # qpu child(ren): a job requests qvendor_<v> -> qpu and matches here
        for q in range(args.qpus):
            qid = str(next_id); next_id += 1
            nodes.append({
                "id": qid,
                "metadata": {
                    "type": "qpu",
                    "rank": -1,
                    "paths": {"containment": "%s/qpu%d" % (vpath, q)},
                    "properties": {v: ""},
                },
            })
            edges.append({"source": vid, "target": qid,
                          "metadata": {"subsystem": "containment"}})

    out = {"graph": {"nodes": nodes, "edges": edges}}
    outfile = open(args.ofn, "w") if args.ofn else sys.stdout
    print(json.dumps(out), file=outfile)
    if args.ofn:
        outfile.close()


if __name__ == "__main__":
    main()
