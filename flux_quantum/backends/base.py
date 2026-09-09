"""Vendor backend interface.

A backend supplies the live signals the fluxion graph cannot, meaning queue
depth, cost and availability, and it owns the vendor session. Backends run in
userspace with the user credentials, both in the CLI plugin and in the scout.

Constructing a backend is the credential check. A backend whose SDK is not
installed or whose credentials cannot be found raises BackendError from
__init__, so nothing downstream ever holds an unusable backend.

Adding a vendor means adding a Backend subclass and registering it.
"""

from abc import ABC, abstractmethod


class BackendError(RuntimeError):
    """The backend cannot be used. The message names what is missing, never
    a credential value."""


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

    # set by __init__ to say where the credentials came from, for the log
    credential_note = ""

    @classmethod
    def add_options(cls, add_option):
        """Declare the flux submit options for this vendor.

        Names get a quantum prefix, so --ibm-backend becomes
        --quantum-ibm-backend. Namespace them by vendor to avoid collisions.
        """
        return

    def scout_options(self, args):
        """Return the options for this vendor from the parsed args."""
        return {}

    def session_id(self, opened):
        """What the classical job should be handed.

        Usually the id open_session returned. Braket publishes its token only
        once the hybrid job is running, and the token rather than the job ARN
        is what lets work elsewhere submit against the hold.

        Called after wait_for_priority.
        """
        return opened

    def open_session(self, options):
        """Open a session with the user credentials and return the id.

        Runs in the scout. The options come from scout_options.
        """
        raise NotImplementedError(
            "{}: open_session is not implemented for this vendor".format(self.name)
        )

    def job_environment(self, options):
        """Env vars to add to the classical job, for example which QPU it has."""
        return {}

    def wait_for_priority(self, options=None):
        """Block until the vendor is actually ours, then return (ok, reason).

        Opening a session is not the same as having the device. IBM activates a
        session when its first task reaches the head of the queue, and Braket
        reports a queue position and never holds anything. The scout does not
        release the classical job until this returns ok.
        """
        return True, "not applicable"

    def close_session(self, session=None):
        """Release the session opened by open_session. Called in the scout
        after the classical job finishes."""
        return

    @abstractmethod
    def probe(self):
        """Return Signals for this vendor. May raise. The selector drops a
        backend that raises or is unavailable."""


_REGISTRY = {}


def register(cls):
    """Class decorator that registers a Backend subclass by name."""
    if not getattr(cls, "name", None):
        raise ValueError("Backend subclass must set a 'name'")
    _REGISTRY[cls.name] = cls
    return cls


def get_backend(name):
    """Return a backend for a vendor, or None if the name is unknown.

    Raises BackendError when the vendor is known but cannot be used.
    """
    cls = _REGISTRY.get(name)
    return cls() if cls else None


def known_vendors():
    """Return the set of vendor names that have a registered backend."""
    return set(_REGISTRY)


def backend_classes():
    """Return the registered Backend subclasses."""
    return list(_REGISTRY.values())
