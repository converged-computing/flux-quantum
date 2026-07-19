"""Mock vendor backend(s) for token-free testing.

Registers mock vendors whose credentials are always "present" and whose probe
never calls a real API, so the whole pipeline -- discovery, selection,
validation, and the scout session handoff -- runs with NO real vendor token.

Opt-in: these are only registered when FLUX_QUANTUM_MOCK is set, so production
never silently exposes a mock vendor (see backends/__init__.py).

Two mocks are provided so ranking policies can be exercised without creds:
    mock       queue_depth 0  (default winner for --select any)
    mock_busy  queue_depth 9  (so --select queue prefers 'mock')
Tunables (override 'mock' only) for ad-hoc ranking tests:
    FLUX_QUANTUM_MOCK_QUEUE   int
    FLUX_QUANTUM_MOCK_COST    float
"""
import os
from .base import Backend, Signals, register


@register
class MockBackend(Backend):
    name = "mock"

    @classmethod
    def add_options(cls, add_option):
        add_option("--mock-session", metavar="ID", default=None,
                   help="mock: force this session id (testing)")
        add_option("--mock-latency", metavar="SECONDS", default=None,
                   help="mock: delay this many seconds before opening a session")

    def scout_options(self, args):
        return {
            "session": getattr(args, "mock_session", None),
            "latency": getattr(args, "mock_latency", None),
        }

    def open_session(self, options):
        import time
        latency = options.get("latency")
        if latency:
            time.sleep(float(latency))
        forced = options.get("session")
        if forced:
            return forced
        return "mock-session-{}-{}".format(int(time.time()), os.getpid())

    def credentials_present(self):
        return True, "mock: no credentials required"

    def probe(self):
        q = os.environ.get("FLUX_QUANTUM_MOCK_QUEUE")
        c = os.environ.get("FLUX_QUANTUM_MOCK_COST")
        return Signals(available=True,
                       queue_depth=int(q) if q is not None else 0,
                       cost=float(c) if c is not None else 0.0,
                       detail={"mock": True})


@register
class MockBusyBackend(Backend):
    name = "mock_busy"

    def credentials_present(self):
        return True, "mock_busy: no credentials required"

    def probe(self):
        return Signals(available=True, queue_depth=9, cost=5.0,
                       detail={"mock": True})
