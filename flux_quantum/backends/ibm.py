"""IBM Quantum backend, through QRMI.

Defaults to the qiskit-runtime-service resource type. That type opens a real
session, so the account has to be one that supports sessions. Use
--quantum-ibm-type ibm-quantum-system for a direct access system.
"""

from .base import register
from .qrmi import QRMIBackend


@register
class IBMBackend(QRMIBackend):
    name = "ibm"
    default_type = "qiskit-runtime-service"
