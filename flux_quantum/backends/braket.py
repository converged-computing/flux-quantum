"""AWS Braket backend.

The probe is a stub. The real one uses boto3 with the user AWS credentials.
Braket has no acquire and release like QRMI, so it needs its own backend.
"""

import os
from .base import Backend, Signals, register


@register
class BraketBackend(Backend):
    name = "braket"
    required_env = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")

    @classmethod
    def add_options(cls, add_option):
        add_option(
            "--braket-device",
            metavar="ARN",
            default=None,
            help="Braket: device ARN to target",
        )
        add_option(
            "--braket-region",
            metavar="REGION",
            default=None,
            help="Braket: AWS region for the device",
        )
        add_option(
            "--braket-shots",
            metavar="N",
            default=None,
            help="Braket: number of shots for the session workload",
        )

    def scout_options(self, args):
        return {
            "device": getattr(args, "braket_device", None),
            "region": getattr(args, "braket_region", None),
            "shots": getattr(args, "braket_shots", None),
        }

    def open_session(self, options):
        # TODO validate the reservation ARN for the requested device
        raise NotImplementedError(
            "Braket open_session not yet implemented; use --quantum-vendor mock "
            "with FLUX_QUANTUM_MOCK for now (options captured: {})".format(options)
        )

    def credentials_present(self):
        missing = [v for v in self.required_env if not os.environ.get(v)]
        if missing:
            return False, "Braket: missing env var(s): " + ", ".join(missing)
        return True, "Braket: credentials present"

    def probe(self):
        # TODO braket device status, queue and pricing with the user creds
        return Signals(
            available=True, queue_depth=None, cost=None, detail={"note": "mock probe"}
        )
