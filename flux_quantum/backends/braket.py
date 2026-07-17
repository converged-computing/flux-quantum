"""AWS Braket backend.

MOCK probe for now: real impl uses boto3/Braket with the user's AWS creds to
read device availability / queue / price. Different execution model from QRMI,
which is exactly why it is its own backend.
"""
import os
from .base import Backend, Signals, register


@register
class BraketBackend(Backend):
    name = "braket"
    required_env = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")

    def credentials_present(self):
        missing = [v for v in self.required_env if not os.environ.get(v)]
        if missing:
            return False, "Braket: missing env var(s): " + ", ".join(missing)
        return True, "Braket: credentials present"

    def probe(self):
        # REAL IMPL: braket device status / queue / pricing via the user's creds.
        return Signals(available=True, queue_depth=None, cost=None,
                       detail={"note": "mock probe"})
