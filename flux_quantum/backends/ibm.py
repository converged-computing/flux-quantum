"""IBM Quantum backend, via QRMI or the IBM Runtime API.

The probe is a stub. The real one reads queue depth and cost with the user
token.
"""

import os
from .base import Backend, Signals, register


@register
class IBMBackend(Backend):
    name = "ibm"
    # env vars the user must have exported for IBM
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
        # TODO open a QRMI or Runtime session on the requested backend with
        # the user token and return the session id
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
        # TODO query IBM or QRMI for live queue depth and cost
        return Signals(
            available=True, queue_depth=None, cost=None, detail={"note": "mock probe"}
        )
