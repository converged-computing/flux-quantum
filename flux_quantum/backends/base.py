"""Vendor backend interface.

A backend supplies the live signals the fluxion graph cannot, meaning queue
depth, cost and availability, and it owns the vendor session. Backends run in
userspace with the user credentials, both in the CLI plugin and in the scout.

Adding a vendor means adding a Backend subclass and registering it.
"""

from abc import ABC, abstractmethod


class Signals:
    """Live signals for one vendor, returned by Backend.probe()."""

    def __init__(self, available, queue_depth=None, cost=None, detail=None):
        self.available = bool(available)
        self.queue_depth = queue_depth  # pending jobs ahead, lower better
        self.cost = cost  # lower better
        self.detail = detail or {}

    def __repr__(self):
        return "Signals(available={}, queue_depth={}, cost={})".format(
            self.available, self.queue_depth, self.cost
        )


class Backend(ABC):
    """One vendor. Live signals plus the session the scout opens."""

    # vendor key like ibm, must match the qdevice_<name> graph type
    name = None

    @classmethod
    def add_options(cls, add_option):
        """Declare the flux submit options for this vendor.

        Names get a quantum prefix, so --ibm-backend becomes
        --quantum-ibm-backend. Namespace them by vendor to avoid collisions.
        """
        return

    def scout_options(self, args):
        """Return the options for this vendor from the parsed args.

        Only the selected vendor gets collected, so reading another vendor
        args here is harmless.
        """
        return {}

    def open_session(self, options):
        """Open a session with the user credentials and return the id.

        Runs in the scout. The options come from scout_options.
        """
        raise NotImplementedError(
            "{}: open_session is not implemented for this vendor".format(self.name)
        )

    def close_session(self, session=None):
        """Release the session opened by open_session.

        Called in the scout after the classical job finishes. A backend holding
        vendor state, like a QRMI lock, stashes it on self and releases it
        here. Defaults to doing nothing for vendors with nothing to release.
        """
        return

    @abstractmethod
    def credentials_present(self):
        """Return ok and a message, where ok is False if credentials are
        missing. Never returns or logs the secret itself.
        """

    @abstractmethod
    def probe(self):
        """Return Signals for this vendor.

        Only called once credentials_present is ok. May raise. The selector
        drops a backend that raises or is unavailable.
        """


_REGISTRY = {}


def register(cls):
    """Class decorator that registers a Backend subclass by name."""
    if not getattr(cls, "name", None):
        raise ValueError("Backend subclass must set a 'name'")
    _REGISTRY[cls.name] = cls
    return cls


def get_backend(name):
    """Return an instantiated backend for a vendor, or None if unknown."""
    cls = _REGISTRY.get(name)
    return cls() if cls else None


def known_vendors():
    """Return the set of vendor names that have a registered backend."""
    return set(_REGISTRY)


def backend_classes():
    """Return the registered Backend subclasses."""
    return list(_REGISTRY.values())
