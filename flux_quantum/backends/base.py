"""Vendor backend interface for quantum coscheduling.

The core selector is vendor-agnostic: it discovers candidate vendors and asks
each candidate's backend for LIVE, STATEFUL signals (queue depth, cost,
availability) that fluxion's static graph cannot provide. Each backend knows
how to talk to exactly one vendor (QRMI, IBM, Braket, ...) using the USER's
credentials -- so backends only ever run in userspace (the CLI plugin), never
in the owner-side jobtap plugin.

Add a vendor == add a Backend subclass and register it. The core never learns a
vendor's API; a backend never learns about job lifecycle.
"""
from abc import ABC, abstractmethod


class Signals:
    """Live signals for one vendor, returned by Backend.probe()."""

    def __init__(self, available, queue_depth=None, cost=None, detail=None):
        self.available = bool(available)   # is the vendor usable right now?
        self.queue_depth = queue_depth     # pending jobs ahead (lower better), or None
        self.cost = cost                   # relative/absolute cost (lower better), or None
        self.detail = detail or {}         # free-form extras for logging/policy

    def __repr__(self):
        return "Signals(available={}, queue_depth={}, cost={})".format(
            self.available, self.queue_depth, self.cost)


class Backend(ABC):
    """One vendor's live-signal provider. Runs in userspace with user creds."""

    #: short vendor key, e.g. "ibm"; must match the qvendor_<name> registry type
    name = None

    @abstractmethod
    def credentials_present(self):
        """Return (ok, message). ok=False if required env/creds are missing.

        Checks the USER's environment for whatever this vendor needs (API token,
        endpoint, etc.). Never returns or logs the secret itself.
        """

    @abstractmethod
    def probe(self):
        """Return a Signals for this vendor (queue depth, cost, availability).

        Called only after credentials_present() is ok. Talks to the vendor API
        with the user's credentials. May raise; the selector treats a raising
        or unavailable backend as a non-candidate.
        """


_REGISTRY = {}


def register(cls):
    """Class decorator: register a Backend subclass by its .name."""
    if not getattr(cls, "name", None):
        raise ValueError("Backend subclass must set a 'name'")
    _REGISTRY[cls.name] = cls
    return cls


def get_backend(name):
    """Return an instantiated backend for vendor *name*, or None if unknown."""
    cls = _REGISTRY.get(name)
    return cls() if cls else None


def known_vendors():
    """Return the set of vendor names that have a registered backend."""
    return set(_REGISTRY)
