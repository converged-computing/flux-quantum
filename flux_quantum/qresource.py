##############################################################
# Copyright 2024 Lawrence Livermore National Security, LLC
# (c.f. AUTHORS, NOTICE.LLNS, COPYING)
#
# SPDX-License-Identifier: LGPL-3.0
##############################################################

"""The quantum resource shape, in one place.

A vendor device is qdevice_<vendor> -> qpu, hung off the graph root as a
sibling of rack, so it sits in a different subtree than the node cores.

Both the jobspec request and the add_subgraph payload are built from here, so
the graph and the jobspec cannot disagree on type name, nesting or
exclusivity. No flux imports, just dicts.
"""

import uuid

QDEVICE_PREFIX = "qdevice_"
QPU = "qpu"


def qdevice_type(vendor):
    """Fluxion resource type for a vendor device vertex."""
    return "{}{}".format(QDEVICE_PREFIX, vendor)


def jobspec_resource(vendor, nqpus=1):
    """A scout jobspec resources entry for the vendor device.

    The qpu must be exclusive. Fluxion only adds a planner span and emits a
    leaf device into R when it is asked for exclusively, since upd_plan calls
    planner_add_span only inside the excl branch. Ask for it any other way and
    it is matched but then dropped from the allocation.
    """
    return {
        "type": qdevice_type(vendor),
        "count": 1,
        "with": [{"type": QPU, "count": nqpus, "exclusive": True}],
    }


def _vertex(vid, vtype, path, vendor):
    return {
        "id": str(vid),
        "metadata": {
            "type": vtype,
            "rank": -1,
            "paths": {"containment": path},
            "properties": {vendor: ""},
        },
    }


def _edge(src, tgt):
    return {
        "source": str(src),
        "target": str(tgt),
        "metadata": {"subsystem": "containment"},
    }


def find_root(graph):
    """Return the root vertex of a JGF graph."""
    targets = {e["target"] for e in graph["edges"]}
    roots = [n for n in graph["nodes"] if n["id"] not in targets]
    if len(roots) != 1:
        raise ValueError("expected exactly 1 root, found %d" % len(roots))
    return roots[0]


def graph_subgraph(live_graph, vendors, qpus=1):
    """Build an add_subgraph payload planting qdevice_<vendor> -> qpu under the
    live graph root.

    The root is copied verbatim so the fluxion JGF reader matches it on
    containment path and rank, then attaches rather than duplicating it.
    """
    if isinstance(vendors, str):
        vendors = [vendors]
    root = find_root(live_graph)
    root_id = root["id"]
    root_path = root["metadata"]["paths"]["containment"]
    next_id = max(int(n["id"]) for n in live_graph["nodes"]) + 1

    nodes = [root]  # verbatim, so it is matched on path and rank
    edges = []
    for vendor in vendors:
        qd_id = next_id
        next_id += 1
        qd_type = qdevice_type(vendor)
        qd_path = "{}/{}0".format(root_path, qd_type)
        nodes.append(_vertex(qd_id, qd_type, qd_path, vendor))
        edges.append(_edge(root_id, qd_id))
        for q in range(qpus):
            qpu_id = next_id
            next_id += 1
            nodes.append(
                _vertex(qpu_id, QPU, "{}/{}{}".format(qd_path, QPU, q), vendor)
            )
            edges.append(_edge(qd_id, qpu_id))
    return {"graph": {"nodes": nodes, "edges": edges}}


def _node_to_core_intermediates(graph):
    """Return the containment types between node and core.

    An hwloc graph gives socket, issue1284 gives nothing. Returns None when
    there is no node to core path.
    """
    id2type = {n["id"]: n["metadata"]["type"] for n in graph["nodes"]}
    parent = {e["target"]: e["source"] for e in graph["edges"]}
    core = next((nid for nid, t in id2type.items() if t == "core"), None)
    if core is None:
        return None
    chain = []
    cur = parent.get(core)
    while cur is not None and id2type.get(cur) != "node":
        chain.append(id2type[cur])
        cur = parent.get(cur)
    if id2type.get(cur) != "node":
        return None
    chain.reverse()
    return chain


def classical_resource(live_graph, ncores=1, label="scout"):
    """Build the classical foothold for the scout, mirroring the node to core
    path in the live graph with a slot just above the core.

        node->core          ->  node -> slot -> core
        node->socket->core  ->  node -> socket -> slot -> core

    Falls back to node -> slot -> core when there is no node to core path.
    """
    intermediates = _node_to_core_intermediates(live_graph)
    if intermediates is None:
        intermediates = []
    inner = {
        "type": "slot",
        "count": 1,
        "label": label,
        "with": [{"type": "core", "count": ncores}],
    }
    for t in reversed(intermediates):
        inner = {"type": t, "count": 1, "with": [inner]}
    return {"type": "node", "count": 1, "with": [inner]}


def count_cores(jobspec):
    """Total cores a v1 jobspec asks for.

    Multiplies counts down the resource tree and sums the core leaves. The
    jobtap plugin needs this to keep a core budget, and doing it here means it
    is testable without a broker rather than parsed in C.
    """

    def walk(entries, factor):
        total = 0
        for entry in entries or []:
            count = entry.get("count", 1)
            if isinstance(count, dict):  # a range, take the minimum we must get
                count = count.get("min", 1)
            n = factor * int(count)
            if entry.get("type") == "core":
                total += n
            total += walk(entry.get("with"), n)
        return total

    return walk(jobspec.get("resources"), 1)
