"""Ingest side validator that checks the vendor is set and known.

The credential check runs in the CLI plugin, where the user environment is
visible. This is the server side backstop for attributes that are not secret,
rejecting before the job takes resources.

Add this module to the ingest validator plugins to wire it up.
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
    """Return None when ok, otherwise an error string. No secret checks."""
    vendor = _vendor_of(jobspec)
    if vendor is None:
        return None  # not a quantum job
    if vendor not in known_vendors():
        return "quantum: unknown/unpermitted vendor '{}'".format(vendor)
    return None
