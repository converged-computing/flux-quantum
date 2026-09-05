##############################################################
# Copyright 2024 Lawrence Livermore National Security, LLC
# (c.f. AUTHORS, NOTICE.LLNS, COPYING)
#
# SPDX-License-Identifier: LGPL-3.0
##############################################################

"""Splice vendor devices into the live fluxion graph.

Uses the sched-fluxion-resource find / add_subgraph RPCs through the python
bindings. The shape comes from qresource, so this module only does the RPCs.
These RPCs are owner-only, so a user can populate their own subinstance but not
the system graph.
"""

import json

from . import qresource


def get_live_graph(handle, criteria="status=up"):
    """Return the live fluxion graph of nodes and edges from the find RPC."""
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


def allocated_cores(handle, lister=None):
    """Cores the scheduler currently has allocated."""
    if lister is None:
        from flux.resource import resource_list as lister
    return int(lister(handle).get().allocated.ncores)


def populate(handle, vendors, qpus=1, graph=None, lister=None, force=False):
    """Ensure qdevice_<vendor> -> qpu exists in the live graph.

    Idempotent, only missing vendors are added. Returns what was added.

    Growing the graph while jobs hold resources corrupts fluxion. The spans of
    the running jobs no longer match the graph, so every later free fails with
    planner_multi_rem_span returned -1, those resources are never released, and
    the instance stops scheduling anything at all. It looks like free cores that
    nothing will use.

    So refuse to grow a busy graph. Populate at startup instead, before any
    jobs run, which is what the vendors are there for anyway. force=True is for
    a caller that knows the instance is idle.
    """
    if isinstance(vendors, str):
        vendors = [vendors]
    if graph is None:
        graph = get_live_graph(handle)
    missing = [v for v in vendors if v not in vendors_present(graph)]
    if not missing:
        return set()

    if not force:
        try:
            busy = allocated_cores(handle, lister)
        except Exception:
            busy = 0  # cannot tell, so do not block the caller
        if busy:
            raise RuntimeError(
                "quantum: {} would have to be added to the fluxion graph, but "
                "{} cores are allocated. Growing the graph now would break "
                "resource release for every running job and wedge the "
                "scheduler. Populate at startup instead, with "
                "flux python -m flux_quantum.populate".format(", ".join(missing), busy)
            )
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
