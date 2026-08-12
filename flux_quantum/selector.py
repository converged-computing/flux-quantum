"""Vendor selection, meaning discovery and ranking over backends.

This is selection and not scheduling. Probe each candidate backend for live
signals and pick one by policy. Runs in userspace because probing needs the
user credentials.

Graph discovery is optional and only used when the user names no candidates, so
the selector works with or without a flux handle.
"""

import json

from . import qresource
from .backends import get_backend, known_vendors


class SelectionError(Exception):
    pass


def _rank_key(policy):
    """Return a sort key over vendor and Signals for the given policy."""
    if policy in (None, "any", "available"):
        return lambda item: 0
    if policy == "queue":  # shortest queue first
        return lambda item: (item[1].queue_depth is None, item[1].queue_depth or 0)
    if policy == "cost":  # cheapest first
        return lambda item: (item[1].cost is None, item[1].cost or 0)
    raise SelectionError("unknown select policy: {}".format(policy))


def select_vendor(candidates=None, policy=None):
    """Pick a vendor from candidates by policy, one of any, queue or cost.

    Passing no candidates means every vendor with a registered backend.
    Returns the vendor, its Signals and the log lines. Raises SelectionError
    with the reason for each candidate when nothing is usable.
    """
    names = list(candidates) if candidates else sorted(known_vendors())
    if not names:
        raise SelectionError("no candidate vendors and no backends registered")

    usable = []
    log = []
    for name in names:
        backend = get_backend(name)
        if backend is None:
            log.append("{}: no backend registered".format(name))
            continue
        ok, msg = backend.credentials_present()
        if not ok:
            log.append(msg)
            continue
        try:
            sig = backend.probe()
        except Exception as e:  # a raising backend is simply not a candidate
            log.append("{}: probe failed: {}".format(name, e))
            continue
        if not sig.available:
            log.append("{}: not available".format(name))
            continue
        usable.append((name, sig))
        log.append("{}: candidate ({})".format(name, sig))

    if not usable:
        raise SelectionError("no usable quantum vendor.\n  " + "\n  ".join(log))

    usable.sort(key=_rank_key(policy))
    name, sig = usable[0]
    log.append("selected: {} (policy={})".format(name, policy or "any"))
    return name, sig, log


def discover_registry_vendors(handle):
    """Return the vendor names present in the live graph as qdevice_* types.

    Empty set on any error, so callers can fall back to the backends.
    """
    try:
        resp = handle.rpc(
            "sched-fluxion-resource.find", {"criteria": "status=up", "format": "jgf"}
        ).get()
        R = resp.get("R")
        graph = R if isinstance(R, dict) else json.loads(R)
        vendors = set()
        for node in graph.get("graph", {}).get("nodes", []):
            t = node.get("metadata", {}).get("type", "")
            if t.startswith(qresource.QDEVICE_PREFIX):
                vendors.add(t[len(qresource.QDEVICE_PREFIX) :])
        return vendors
    except Exception:
        return set()
