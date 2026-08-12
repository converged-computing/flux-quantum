"""Ingest-side job validator for quantum jobs (server-side backstop).

The primary credential check runs userspace in the CLI plugin (QuantumCLIPlugin
.validate), because only there is the user's real environment visible and the
secret stays out of the jobspec. This module is the *server-side* backstop: it
validates non-secret required attributes on the ingested jobspec (vendor is set
and is one we know), rejecting before the job consumes resources.

Wire-up (flux-config-ingest): add this module to [ingest.validator] plugins.
It follows the flux job-validator plugin convention: a validate(args) callable
that raises ValueError (or returns an error) on an invalid jobspec.
"""

from .backends import known_vendors


def _vendor_of(jobspec):
    try:
        return (
            jobspec.get("attributes", {})
            .get("system", {})
            .get("quantum", {})
            .get("vendor")
        )
    except AttributeError:
        return None


def validate(jobspec):
    """Return None if OK, else an error string. Non-secret checks only."""
    vendor = _vendor_of(jobspec)
    if vendor is None:
        return None  # not a quantum job
    if vendor not in known_vendors():
        return "quantum: unknown/unpermitted vendor '{}'".format(vendor)
    return None
