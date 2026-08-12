##############################################################
# Copyright 2024 Lawrence Livermore National Security, LLC
# (c.f. AUTHORS, NOTICE.LLNS, COPYING)
#
# SPDX-License-Identifier: LGPL-3.0
##############################################################

"""Live-graph populator.

Queries the fluxion resource graph and splices in the vendor quantum devices,
using the owner-gated ``sched-fluxion-resource.find`` / ``add_subgraph`` RPCs via
the flux Python bindings -- no subprocess, no shelling to ``flux inject``. The
shape comes from ``qresource``; this module only does the RPCs. Because the RPCs
are owner-only, a user can populate their OWN subinstance's graph but not the
system graph.
"""

import json

from . import qresource


def get_live_graph(handle, criteria="status=up"):
    """Return the live fluxion graph {"nodes":..,"edges":..} via the find RPC."""
    try:
        resp = handle.rpc(
            "sched-fluxion-resource.find",
            {"criteria": criteria, "format": "jgf"},
        ).get()
    except Exception as exc:
        raise RuntimeError(
            "sched-fluxion-resource.find RPC failed (is fluxion loaded, and are "
            "you the instance owner?): {}".format(exc)
        )
    if "R" not in resp:
        raise RuntimeError("find response missing 'R': {!r}".format(resp))
    doc = resp["R"]
    if isinstance(doc, str):
        doc = json.loads(doc)
    if "graph" in doc:
        return doc["graph"]
    if "scheduling" in doc and "graph" in doc["scheduling"]:
        return doc["scheduling"]["graph"]
    raise ValueError("find response has no graph")


def vendors_present(graph):
    """Vendor names already in the graph (from qdevice_<vendor> vertices)."""
    out = set()
    for n in graph["nodes"]:
        t = n["metadata"]["type"]
        if t.startswith(qresource.QDEVICE_PREFIX):
            out.add(t[len(qresource.QDEVICE_PREFIX) :])
    return out


def populate(handle, vendors, qpus=1, graph=None):
    """Ensure qdevice_<vendor> -> qpu subtrees exist in the live graph.

    Idempotent: queries the graph (find), builds a subgraph for the MISSING
    vendors only (qresource.graph_subgraph), and splices it in via add_subgraph.
    Returns the set of vendor names actually added.
    """
    if isinstance(vendors, str):
        vendors = [vendors]
    if graph is None:
        graph = get_live_graph(handle)
    missing = [v for v in vendors if v not in vendors_present(graph)]
    if not missing:
        return set()
    subgraph = qresource.graph_subgraph(graph, missing, qpus=qpus)
    try:
        handle.rpc(
            "sched-fluxion-resource.add_subgraph",
            {"subgraph": json.dumps(subgraph)},
        ).get()
    except Exception as exc:
        raise RuntimeError(
            "sched-fluxion-resource.add_subgraph RPC failed for vendors {}: "
            "{}".format(missing, exc)
        )
    return set(missing)
