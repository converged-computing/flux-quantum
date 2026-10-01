"""Vendor backend interface.

A backend supplies the live signals the fluxion graph cannot, meaning queue
depth, cost and availability, and it owns the vendor session. Backends run in
userspace with the user credentials, both in the CLI plugin and in the scout.

Constructing a backend is the credential check. A backend whose SDK is not
installed or whose credentials cannot be found raises BackendError from
__init__, so nothing downstream ever holds an unusable backend.

Adding a vendor means adding a Backend subclass and registering it.
"""

import os
from abc import ABC, abstractmethod

# The submit options every vendor is driven by. The CLI collects them once,
# and each backend maps them to its own terms in scout_options. A user picks
# the vendor and the device and nothing else changes between vendors.
#
#   device    the vendor's own name for the device: a Braket ARN, an IonQ
#             backend, a QRMI resource id. Each vendor has a default
#   hold      session takes the vendor's real hold, a hybrid job on Braket,
#             a session on IonQ and IBM. probe submits a front of queue job
#             and holds nothing
#   hold_max  seconds the hold may last, so a scout that is never released
#             stops costing
#   wait      seconds to wait for the hold to be ours before giving up and
#             cancelling the held job. 0 means as long as the scout may run
#   dry_run   run on the vendor's simulator
COMMON_OPTIONS = ("device", "hold", "hold_max", "wait", "dry_run")
HOLDS = ("session", "probe")


def tuning(name, default=None):
    """An operator setting from the environment, FLUX_QUANTUM_<NAME>.

    Things like the instance the Braket hold runs on or the shot count of a
    warm-up job are tuning for whoever operates the installation, not a
    choice for someone submitting a job, so they are not submit options.
    """
    return os.environ.get("FLUX_QUANTUM_" + name.upper(), default)


def truthy(value):
    """An environment value read as a flag."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no")
    return bool(value)


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

    # what a dry run targets, or None when the vendor has no simulator
    simulator = None

    # the holds this vendor can take, out of HOLDS
    holds = ("session",)

    # set by __init__ to say where the credentials came from, for the log
    credential_note = ""

    def scout_options(self, common):
        """Map the common submit options onto this vendor's terms.

        common has the COMMON_OPTIONS keys. What comes back is whatever
        open_session and wait_for_priority need, and it travels to the
        scout as JSON. The default passes the common options through.
        """
        self.check_hold(common.get("hold"))
        return dict(common)

    def check_hold(self, hold):
        """Refuse a hold this vendor cannot take, at submit time, before
        anything is held."""
        hold = hold or "session"
        if hold not in HOLDS:
            raise BackendError(
                "{}: unknown hold {}. The holds are {}".format(
                    self.name, hold, " and ".join(HOLDS)
                )
            )
        if hold not in self.holds:
            raise BackendError(
                "{}: this vendor has no {} hold, only {}".format(
                    self.name, hold, " and ".join(self.holds)
                )
            )
        return hold

    def dry_run(self, common):
        """The common options for a dry run: the device becomes the
        simulator. A vendor whose simulator needs more, such as a noise
        model standing in for the hardware, adds it here."""
        out = dict(common, dry_run=True)
        if self.simulator:
            out["device"] = self.simulator
        return out

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
