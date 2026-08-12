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

    @classmethod
    def add_options(cls, add_option):
        add_option(
            "--ibm-backend",
            metavar="NAME",
            default=None,
            help="IBM: target backend/device name (e.g. ibm_brisbane)",
        )
        add_option(
            "--ibm-instance",
            metavar="HUB/GROUP/PROJECT",
            default=None,
            help="IBM: instance (hub/group/project)",
        )
        add_option(
            "--ibm-shots",
            metavar="N",
            default=None,
            help="IBM: number of shots for the session workload",
        )

    def scout_options(self, args):
        return {
            "backend": getattr(args, "ibm_backend", None),
            "instance": getattr(args, "ibm_instance", None),
            "shots": getattr(args, "ibm_shots", None),
        }

    def open_session(self, options):
        # REAL IMPL (vendor-API phase): using the user's QISKIT_IBM_TOKEN, open a
        # QRMI/Runtime session on options["backend"] (with instance/shots) and
        # return its id. Deferred so the mock pipeline can ship first.
        raise NotImplementedError(
            "IBM open_session not yet implemented; use --quantum-vendor mock "
            "with FLUX_QUANTUM_MOCK for now (options captured: {})".format(options)
        )

    def credentials_present(self):
        missing = [v for v in self.required_env if not os.environ.get(v)]
        if missing:
            return False, "IBM: missing env var(s): " + ", ".join(missing)
        return True, "IBM: credentials present"

    def probe(self):
        # REAL IMPL: query IBM/QRMI with the user's token for live queue/cost.
        # e.g. depth = qrmi.queue_depth(backend); cost = qrmi.cost(backend)
        return Signals(
            available=True, queue_depth=None, cost=None, detail={"note": "mock probe"}
        )
