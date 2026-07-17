"""Flux CLI submission plugin for quantum coscheduling (prefix: quantum).

Runs in the user's process at submit time (flux submit/run/batch), BEFORE the
jobspec is signed -- so it may hold the user's vendor credentials, probe the
backends for live signals, pick a vendor, and stamp the choice onto the
jobspec. The credential never leaves userspace and never enters the jobspec.

CLI options (namespaced by the "quantum" prefix):
    --quantum-vendor VENDOR      request a specific vendor (e.g. ibm, braket)
    --quantum-select POLICY      pick automatically: any | queue | cost
    --quantum-rendezvous DIR     shared rendezvous dir for the session handoff

Discovery order for candidates:
    1. --quantum-vendor if given (single candidate)
    2. otherwise the vendors discovered in the fluxion qvendor_* registry
       (via the resource.find RPC), falling back to the vendors that have a
       registered backend if the registry is empty or unreachable

This plugin does policy/selection only. It sets attributes.system.hold=1 so the
job is held+reserved (our fluxion hold/reserve), tags the chosen vendor, and
records the rendezvous dir. The separate scout opens the session and unholds.
"""
import os
import sys

from flux.cli.plugin import CLIPlugin

from .selector import select_vendor, SelectionError
from .backends import get_backend

#: subcommands where quantum submission makes sense
_ACTIVE_PROGS = ("submit", "run", "batch", "bulksubmit", "alloc")


class QuantumCLIPlugin(CLIPlugin):
    """Select a quantum vendor at submit time and prepare the held job."""

    def __init__(self, prog):
        super().__init__(prog, prefix="quantum")
        if self.prog not in _ACTIVE_PROGS:
            return
        self.add_option("--vendor", metavar="VENDOR", default=None,
                        help="request a specific quantum vendor (e.g. ibm)")
        self.add_option("--select", metavar="POLICY", default=None,
                        help="auto-select vendor: any | queue | cost")
        self.add_option("--rendezvous", metavar="DIR", default=None,
                        help="shared rendezvous dir for the session handoff")
        # cache the choice from preinit for modify_jobspec/validate
        self._chosen = None
        self._rendezvous = None

    def _is_quantum(self, args):
        # activated when the user asks for a vendor or an auto-select policy
        return bool(getattr(args, "vendor", None) or getattr(args, "select", None))

    def preinit(self, args):
        """Probe backends with the user's creds and pick a vendor."""
        if not self._is_quantum(args):
            return
        policy = args.select or "any"
        disc_line = None
        if args.vendor:
            candidates = [args.vendor]
        else:
            # --quantum-select with no explicit vendor: discover candidates from
            # the fluxion qvendor_* registry; fall back to registered backends.
            candidates = self._discover_candidates()
            if candidates is not None:
                disc_line = "discovered from registry: " + " ".join(candidates)
        try:
            vendor, _sig, log = select_vendor(candidates=candidates, policy=policy)
        except SelectionError as e:
            raise SystemExit("flux quantum: {}".format(e))
        self._chosen = vendor
        self._rendezvous = args.rendezvous
        # IMPORTANT: diagnostics MUST go to stderr. flux submit prints the
        # jobid to stdout, and cid=$(flux submit ...) captures stdout -- any
        # plugin output on stdout corrupts the captured jobid.
        if disc_line:
            print("flux quantum: " + disc_line, file=sys.stderr)
        for line in log:
            print("flux quantum: " + line, file=sys.stderr)

    def _discover_candidates(self):
        """Discover vendor candidates from the live fluxion qvendor_* registry.

        Opens a flux handle and reads the registry via the selector helper.
        Returns a sorted list of vendor names, or None to signal "fall back to
        the registered backends" (no handle, no fluxion, or empty registry).
        """
        try:
            import flux
            from .selector import discover_registry_vendors
            vendors = discover_registry_vendors(flux.Flux())
            if vendors:
                return sorted(vendors)
        except Exception:
            pass
        return None

    def modify_jobspec(self, args, jobspec):
        """Stamp the choice + hold the job (no .resources rewrite needed)."""
        if not self._chosen:
            return
        jobspec.setattr("system.quantum.vendor", self._chosen)
        if self._rendezvous:
            jobspec.setattr("system.quantum.rendezvous", self._rendezvous)
        # hold + reserve-first via our fluxion mechanism
        jobspec.setattr("system.hold", 1)

    def validate(self, jobspec):
        """Fail before submission if the chosen vendor's creds are missing.

        The credential itself is never placed in the jobspec; we only confirm
        (in userspace) that it is present in the environment, so a job is not
        dispatched only to fail after consuming resources.
        """
        try:
            vendor = jobspec.getattr("system.quantum.vendor")
        except KeyError:
            return  # not a quantum job
        backend = get_backend(vendor)
        if backend is None:
            raise ValueError(
                "quantum: no backend for vendor '{}'".format(vendor))
        ok, msg = backend.credentials_present()
        if not ok:
            raise ValueError(msg)
