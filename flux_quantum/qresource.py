##############################################################
# Copyright 2024 Lawrence Livermore National Security, LLC
# (c.f. AUTHORS, NOTICE.LLNS, COPYING)
#
# SPDX-License-Identifier: LGPL-3.0
##############################################################

"""Single source of truth for the quantum-resource shape.

A vendor's quantum device is modeled as ``qdevice_<vendor> -> qpu``: a device
subtree hung off the graph ROOT, i.e. a sibling of rack (a "rack-level" device,
like an ssd) living in a different subtree than the node's cores. This module is
the ONLY place that shape is written:

  * the submit CLI plugin builds its jobspec request from ``jobspec_resource()``
  * the graph populator builds its add_subgraph payload from ``graph_subgraph()``

so the graph and the jobspec can never disagree on the type name, nesting, or
exclusivity. Pure dict manipulation -- no flux imports, unit-testable.
"""

QDEVICE_PREFIX = "qdevice_"
QPU = "qpu"


def qdevice_type(vendor):
    """Fluxion resource type for a vendor's quantum-device container vertex."""
    return "{}{}".format(QDEVICE_PREFIX, vendor)


def jobspec_resource(vendor, nqpus=1):
    """A scout jobspec ``.resources`` entry requesting the vendor's device.

    The qpu is requested EXCLUSIVE on purpose. Fluxion only allocates (adds a
    planner span) and emits a leaf device into R when it is requested
    exclusively; a non-exclusive leaf device is matched (it is required for
    satisfiability) but silently dropped from the emitted allocation -- see
    flux-sched ``upd_plan``, which does ``n++`` / ``planner_add_span`` only
    inside ``if (excl)``. Without ``exclusive: true`` the qpu vanishes from R
    with a misleading "allocated" result.
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
    """Return the single root vertex of a JGF graph (no incoming containment)."""
    targets = {e["target"] for e in graph["edges"]}
    roots = [n for n in graph["nodes"] if n["id"] not in targets]
    if len(roots) != 1:
        raise ValueError("expected exactly 1 root, found %d" % len(roots))
    return roots[0]


def graph_subgraph(live_graph, vendors, qpus=1):
    """Build an ``add_subgraph`` payload planting ``qdevice_<vendor> -> qpu``
    subtrees under the live graph root (each qdevice a sibling of rack).

    ``live_graph`` is the ``{"nodes": [...], "edges": [...]}`` returned by
    ``sched-fluxion-resource.find`` (``--format=jgf``). The root is copied
    verbatim so fluxion's JGF reader matches it by (containment path, rank) and
    attaches the new subtrees rather than duplicating the root. Returns
    ``{"graph": {"nodes": [...], "edges": [...]}}``.
    """
    if isinstance(vendors, str):
        vendors = [vendors]
    root = find_root(live_graph)
    root_id = root["id"]
    root_path = root["metadata"]["paths"]["containment"]
    next_id = max(int(n["id"]) for n in live_graph["nodes"]) + 1

    nodes = [root]  # verbatim: matched by (path, rank), enters vmap not dup'd
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
    """Return the intermediate containment types between 'node' and 'core' in a
    live graph (e.g. ['socket'] for an hwloc graph, [] for issue1284).

    Returns None if the graph has no node->...->core containment path (caller
    then falls back). Derived from the graph so the classical foothold always
    matches the real hierarchy rather than a hardcoded guess.
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
    chain.reverse()  # top-down order under the node
    return chain


def classical_resource(live_graph, ncores=1, label="scout"):
    """Build the scout's classical foothold, mirroring the live graph's
    node->...->core path with a slot inserted just above the core.

    issue1284 (node->core)        -> node -> slot -> core
    hwloc     (node->socket->core)-> node -> socket -> slot -> core

    If no node->core path is found, falls back to node -> slot -> core.
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
