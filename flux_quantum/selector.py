"""Vendor selection: generic discovery + ranking over vendor backends.

Selection, not scheduling. Given a set of candidate vendors (either named by
the user or discovered from the fluxion registry), probe each candidate's
backend for live signals and pick one by a simple policy. Runs in userspace
(the CLI plugin) because probing needs the user's credentials.

Discovery of the fluxion "qdevice_*" registry (via the resource.find RPC) is
optional and only used when the user does not name candidates explicitly; it is
kept behind discover_registry_vendors() so the selector works with or without a
live fluxion handle.
"""

from . import qresource
from .backends import get_backend, known_vendors


class SelectionError(Exception):
    pass


def _rank_key(policy):
    """Return a sort key(fn) over (vendor, Signals) for the given policy."""
    if policy in (None, "any", "available"):
        return lambda item: 0
    if policy == "queue":       # shortest queue first
        return lambda item: (item[1].queue_depth is None, item[1].queue_depth or 0)
    if policy == "cost":        # cheapest first
        return lambda item: (item[1].cost is None, item[1].cost or 0)
    raise SelectionError("unknown select policy: {}".format(policy))


def select_vendor(candidates=None, policy=None):
    """Pick a vendor from *candidates* by *policy*.

    Args:
        candidates: iterable of vendor names to consider. If None, all vendors
            that have a registered backend are considered.
        policy: "any"/"queue"/"cost" (default "any").

    Returns:
        (vendor_name, Signals, log_lines)

    Raises SelectionError if no candidate is usable (creds missing / all
    unavailable), with the per-candidate reasons in the message.
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
        raise SelectionError(
            "no usable quantum vendor.\n  " + "\n  ".join(log))

    usable.sort(key=_rank_key(policy))
    name, sig = usable[0]
    log.append("selected: {} (policy={})".format(name, policy or "any"))
    return name, sig, log


def discover_registry_vendors(handle):
    """Discover qdevice_* types from the live fluxion graph via resource.find.

    Optional helper: returns the set of vendor names present in the registry
    (the part after 'qdevice_'). Requires a flux handle. Returns empty set on
    any error so callers can fall back to backend/user-named candidates.
    """
    try:
        import json
        resp = handle.rpc("sched-fluxion-resource.find",
                          {"criteria": "status=up", "format": "jgf"}).get()
        R = resp.get("R")
        graph = R if isinstance(R, dict) else json.loads(R)
        vendors = set()
        for node in graph.get("graph", {}).get("nodes", []):
            t = node.get("metadata", {}).get("type", "")
            if t.startswith(qresource.QDEVICE_PREFIX):
                vendors.add(t[len(qresource.QDEVICE_PREFIX):])
        return vendors
    except Exception:
        return set()
