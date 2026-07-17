"""IBM Quantum backend (via QRMI or the IBM Runtime API).

MOCK probe for now: real impl calls the vendor with the user's token to read
queue depth / cost. Credential check looks for the user's token in the env.
"""
import os
from .base import Backend, Signals, register


@register
class IBMBackend(Backend):
    name = "ibm"
    #: env vars the user must have exported for IBM
    required_env = ("QISKIT_IBM_TOKEN",)

    def credentials_present(self):
        missing = [v for v in self.required_env if not os.environ.get(v)]
        if missing:
            return False, "IBM: missing env var(s): " + ", ".join(missing)
        return True, "IBM: credentials present"

    def probe(self):
        # REAL IMPL: query IBM/QRMI with the user's token for live queue/cost.
        # e.g. depth = qrmi.queue_depth(backend); cost = qrmi.cost(backend)
        return Signals(available=True, queue_depth=None, cost=None,
                       detail={"note": "mock probe"})
