"""Mock vendors for token free testing.

Only registered when FLUX_QUANTUM_MOCK is set, so production never sees them.
There are two so ranking policies can be exercised without credentials.

    mock       queue_depth 0, wins --select any
    mock_busy  queue_depth 9, so --select queue prefers mock

FLUX_QUANTUM_MOCK_QUEUE and FLUX_QUANTUM_MOCK_COST override the mock vendor.
"""

import os
import time

from .base import Backend, Signals, register


@register
class MockBackend(Backend):
    name = "mock"

    @classmethod
    def add_options(cls, add_option):
        add_option(
            "--mock-session",
            metavar="ID",
            default=None,
            help="mock: force this session id (testing)",
        )
        add_option(
            "--mock-latency",
            metavar="SECONDS",
            default=None,
            help="mock: delay this many seconds before opening a session",
        )

    def scout_options(self, args):
        return {
            "session": getattr(args, "mock_session", None),
            "latency": getattr(args, "mock_latency", None),
        }

    def open_session(self, options):
        latency = options.get("latency")
        if latency:
            time.sleep(float(latency))
        forced = options.get("session")
        if forced:
            return forced
        return "mock-session-{}-{}".format(int(time.time()), os.getpid())

    def close_session(self, session=None):
        self._closed = session
        return

    def credentials_present(self):
        return True, "mock: no credentials required"

    def probe(self):
        q = os.environ.get("FLUX_QUANTUM_MOCK_QUEUE")
        c = os.environ.get("FLUX_QUANTUM_MOCK_COST")
        return Signals(
            available=True,
            queue_depth=int(q) if q is not None else 0,
            cost=float(c) if c is not None else 0.0,
            detail={"mock": True},
        )


@register
class MockBusyBackend(Backend):
    name = "mock_busy"

    def close_session(self, session=None):
        self._closed = session
        return

    def credentials_present(self):
        return True, "mock_busy: no credentials required"

    def probe(self):
        return Signals(available=True, queue_depth=9, cost=5.0, detail={"mock": True})
