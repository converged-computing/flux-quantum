"""Mock vendors for token free testing.

Only registered when FLUX_QUANTUM_MOCK is set, so production never sees them.
There are two so ranking policies can be exercised without credentials.

    mock       queue_depth 0, wins --select any
    mock_busy  queue_depth 9, so --select queue prefers mock

FLUX_QUANTUM_MOCK_QUEUE and FLUX_QUANTUM_MOCK_COST override the mock vendor.

The mock also simulates a vendor queue, which is the only way to control queue
depth as an experiment variable. No real vendor lets you do that. The wait is

    base_overhead + depth * service_time

where base_overhead is the fixed cost of getting a task onto the device even
with nothing ahead of it. On ibm_marrakesh at depth 0 that measured 10 to 12
seconds, hence the default.
"""

import os
import random
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
        add_option(
            "--mock-queue-depth",
            metavar="N",
            default=None,
            help="mock: tasks ahead of us in the simulated vendor queue",
        )
        add_option(
            "--mock-service-time",
            metavar="SECONDS",
            default=None,
            help="mock: seconds per task ahead of us, default 0.1 so a deep "
            "queue still finishes in a runnable time",
        )
        add_option(
            "--mock-base-overhead",
            metavar="SECONDS",
            default=None,
            help="mock: fixed wait even at depth 0, default 10 which is what "
            "ibm_marrakesh measured",
        )
        add_option(
            "--mock-jitter",
            metavar="FRACTION",
            default=None,
            help="mock: multiplicative noise on the wait, 0 for a "
            "deterministic run which is what an experiment wants",
        )
        add_option(
            "--mock-seed",
            metavar="N",
            default=None,
            help="mock: seed for the jitter, so a run can be repeated",
        )

    def scout_options(self, args):
        def num(name, default):
            v = getattr(args, name, None)
            return default if v is None else float(v)

        return {
            "session": getattr(args, "mock_session", None),
            "latency": getattr(args, "mock_latency", None),
            "queue_depth": int(num("mock_queue_depth", 0)),
            "service_time": num("mock_service_time", 0.1),
            "base_overhead": num("mock_base_overhead", 10.0),
            "jitter": num("mock_jitter", 0.0),
            "seed": getattr(args, "mock_seed", None),
        }

    def queue_wait(self, options):
        """Seconds the simulated queue makes us wait."""
        depth = int(options.get("queue_depth") or 0)
        wait = float(options.get("base_overhead") or 0) + depth * float(
            options.get("service_time") or 0
        )
        jitter = float(options.get("jitter") or 0)
        if jitter:
            rng = random.Random(options.get("seed"))
            wait *= 1.0 + rng.uniform(-jitter, jitter)
        return max(0.0, wait)

    def wait_for_priority(self, options=None, sleep=time.sleep, now=time.time):
        """Drain the simulated queue, reporting position as it goes.

        Reports like a real vendor so the experiment can read the same series
        out of the scout output whichever backend it ran against.
        """
        options = options or {}
        depth = int(options.get("queue_depth") or 0)
        wait = self.queue_wait(options)
        started = now()
        per = wait / depth if depth else 0.0
        for ahead in range(depth, 0, -1):
            print("mock: queued at position {}".format(ahead), flush=True)
            sleep(per)
        remaining = wait - (now() - started)
        if remaining > 0:
            sleep(remaining)
        self._waited = wait
        return True, "queue drained after {:.1f}s at depth {}".format(wait, depth)

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
