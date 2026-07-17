"""Vendor backends. Importing this package registers the built-in backends.

Real vendor backends (ibm, braket) are always registered. Mock backends are
registered only when FLUX_QUANTUM_MOCK is set, so token-free testing is opt-in
and production never exposes a mock vendor.
"""
import os

from .base import Backend, Signals, register, get_backend, known_vendors  # noqa: F401
from . import ibm    # noqa: F401  (registers IBMBackend)
from . import braket  # noqa: F401  (registers BraketBackend)

if os.environ.get("FLUX_QUANTUM_MOCK"):
    from . import mock  # noqa: F401  (registers MockBackend, MockBusyBackend)
