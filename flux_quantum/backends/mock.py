"""Mock vendors for token free testing.

Only registered when FLUX_QUANTUM_MOCK is set, so production never sees them.
There are two so ranking policies can be exercised without credentials.

    mock       queue_depth 0, wins --select any
    mock_busy  queue_depth 9, so --select queue prefers mock

FLUX_QUANTUM_MOCK_QUEUE and FLUX_QUANTUM_MOCK_COST are what probe reports, and
the simulated wait is set from FLUX_QUANTUM_MOCK_* too, see scout_options.

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

from .base import Backend, Signals, register, tuning


@register
class MockBackend(Backend):
    name = "mock"

    holds = ("session", "probe")

    def scout_options(self, common):
        """The simulated queue is an experiment instrument, so its knobs are
        environment variables and not submit options:

            FLUX_QUANTUM_MOCK_SESSION        force this session id
            FLUX_QUANTUM_MOCK_LATENCY        seconds before opening a session
            FLUX_QUANTUM_MOCK_QUEUE          tasks ahead of us, also what
                                             probe reports
            FLUX_QUANTUM_MOCK_SERVICE_TIME   seconds per task ahead, 0.1
            FLUX_QUANTUM_MOCK_BASE_OVERHEAD  fixed wait at depth 0, 10, which
                                             is what ibm_marrakesh measured
            FLUX_QUANTUM_MOCK_JITTER         multiplicative noise, 0 for a
                                             deterministic run
            FLUX_QUANTUM_MOCK_SEED           seed for the jitter
        """
        self.check_hold(common.get("hold"))
        return {
            "device": common.get("device") or "mock",
            "session": tuning("mock_session"),
            "latency": tuning("mock_latency"),
            "queue_depth": int(tuning("mock_queue", 0)),
            "service_time": float(tuning("mock_service_time", 0.1)),
            "base_overhead": float(tuning("mock_base_overhead", 10.0)),
            "jitter": float(tuning("mock_jitter", 0.0)),
            "seed": tuning("mock_seed"),
            "dry_run": bool(common.get("dry_run")),
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

    def probe(self):
        return Signals(available=True, queue_depth=9, cost=5.0, detail={"mock": True})
