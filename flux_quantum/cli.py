"""Flux CLI submission plugin for quantum coscheduling, prefix quantum.

Runs in the user process at submit time, before the jobspec is signed, so it
can use the user vendor credentials to probe backends and pick a vendor.

Adds --quantum-vendor and --quantum-select, plus whatever each backend adds.

Candidates come from --quantum-vendor when given, otherwise from the qdevice_*
types in the fluxion graph, falling back to the registered backends when the
graph has none.

This plugin does the split. It submits the user work as a held job and rewrites
the submit into the scout. See scout.py for the rest.
"""

import copy
import json
import os
import sys

import flux
from flux.cli.plugin import CLIPlugin

from . import graph
from .selector import select_vendor, SelectionError, discover_registry_vendors
from .backends import get_backend, backend_classes
from .launch import build_scout_jobspec

# subcommands where quantum submission makes sense
_ACTIVE_PROGS = ("submit", "run", "batch", "bulksubmit", "alloc")


def _wrap_commands(jobspec_dict, wrap_path):
    """Wrap each task command so the job gets QUANTUM_SESSION_ID before exec."""
    for task in jobspec_dict.get("tasks", []):
        cmd = list(task.get("command", []))
        task["command"] = ["flux", "python", wrap_path, "--"] + cmd


def _safe_cancel(cancel_fn, handle, jobid, reason):
    """Cancel the held job so a failed setup does not park it forever."""
    try:
        cancel_fn(handle, jobid, reason)
    except Exception as exc:
        print(
            "flux quantum: WARNING failed to cancel job {}: {}".format(jobid, exc),
            file=sys.stderr,
        )


def prepare_pair(
    handle,
    jobspec,
    vendor,
    scout_cores=1,
    options=None,
    job_env=None,
    submit_fn=None,
    populate_fn=None,
    get_graph_fn=None,
    cancel_fn=None,
    wrap_path=None,
    scout_path=None,
):
    """Submit the user work held, then rewrite the jobspec in place into the
    scout that releases it. Returns the id of the held job.

    flux submits whatever is left in the jobspec, so it submits the scout.
    The flux calls are injectable so this is testable without a broker.
    """
    # flux.job is imported here and not at the top so the unit tests can drive
    # this without flux installed
    if submit_fn is None:
        from flux.job import submit as submit_fn
    if cancel_fn is None:
        from flux.job import cancel as cancel_fn
    populate_fn = populate_fn or graph.populate
    get_graph_fn = get_graph_fn or graph.get_live_graph
    if wrap_path is None:
        wrap_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wrap.py")

    classical = copy.deepcopy(jobspec.jobspec)
    # the scout outlives the classical, so give it the same walltime plus slack.
    # 0 means no limit and has to stay 0.
    classical_duration = (
        classical.get("attributes", {}).get("system", {}).get("duration", 0) or 0
    )
    scout_duration = 0 if not classical_duration else classical_duration + 300
    _wrap_commands(classical, wrap_path)
    sysattr = classical.setdefault("attributes", {}).setdefault("system", {})
    sysattr["hold"] = 1
    quantum = sysattr.setdefault("quantum", {})
    quantum["vendor"] = vendor
    if job_env:
        sysattr.setdefault("environment", {}).update(job_env)

    # if feasibility validation is on, an unsatisfiable request is rejected here
    # and we abort before spending any quantum quota
    try:
        main_id = int(submit_fn(handle, json.dumps(classical)))
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(
            "flux quantum: classical request not accepted "
            "(unsatisfiable, or ingest error): {}".format(exc)
        )

    # the foothold comes from the live graph so node->...->core matches reality
    try:
        populate_fn(handle, [vendor])
        live = get_graph_fn(handle)
    except SystemExit:
        raise
    except Exception as exc:
        _safe_cancel(
            cancel_fn,
            handle,
            main_id,
            "flux quantum: aborting held classical (graph setup failed)",
        )
        raise SystemExit(
            "flux quantum: could not prepare the quantum graph: {}".format(exc)
        )

    try:
        scout = build_scout_jobspec(
            vendor,
            main_id,
            ncores=scout_cores,
            duration=scout_duration,
            live_graph=live,
            scout_path=scout_path,
            options=options,
        )
    except SystemExit:
        raise
    except Exception as exc:
        _safe_cancel(
            cancel_fn,
            handle,
            main_id,
            "flux quantum: aborting held classical (scout build failed)",
        )
        raise SystemExit(
            "flux quantum: could not build the scout jobspec: {}".format(exc)
        )

    jobspec.jobspec["resources"] = scout["resources"]
    jobspec.jobspec["tasks"] = scout["tasks"]
    out = jobspec.jobspec.setdefault("attributes", {}).setdefault("system", {})
    out.pop("hold", None)  # the scout runs immediately
    out["duration"] = scout["attributes"]["system"]["duration"]
    return main_id


class QuantumCLIPlugin(CLIPlugin):
    """Select a quantum vendor at submit time and prepare the held job."""

    def __init__(self, prog):
        super().__init__(prog, prefix="quantum")
        if self.prog not in _ACTIVE_PROGS:
            return
        self.add_option(
            "--vendor",
            metavar="VENDOR",
            default=None,
            help="request a specific quantum vendor (e.g. ibm)",
        )
        self.add_option(
            "--select",
            metavar="POLICY",
            default=None,
            help="auto-select vendor: any | queue | cost",
        )
        # let each registered vendor backend contribute its own options
        # (namespaced, e.g. --quantum-ibm-backend, --quantum-mock-session), so a
        # quantum submit can carry vendor-specific parameters for the scout.
        for backend_cls in backend_classes():
            backend_cls.add_options(self.add_option)
        self._chosen = None

    def _is_quantum(self, args):
        return bool(getattr(args, "vendor", None) or getattr(args, "select", None))

    def _resolve_vendor(self, args, quiet=False):
        """Resolve the vendor from args, or None when this is not a quantum
        submit.

        Both preinit and modify_jobspec call this, because flux runs them on
        different plugin instances, so anything cached by preinit is not
        visible in modify_jobspec. Diagnostics go to stderr because stdout
        carries the jobid.
        """
        if not self._is_quantum(args):
            return None
        if self._chosen is not None:
            return self._chosen
        policy = args.select or "any"
        disc_line = None
        if args.vendor:
            candidates = [args.vendor]
        else:
            candidates = self._discover_candidates()
            if candidates is not None:
                disc_line = "discovered from registry: " + " ".join(candidates)
        try:
            vendor, _sig, log = select_vendor(candidates=candidates, policy=policy)
        except SelectionError as e:
            raise SystemExit("flux quantum: {}".format(e))
        self._chosen = vendor
        if not quiet:
            if disc_line:
                print("flux quantum: " + disc_line, file=sys.stderr)
            for line in log:
                print("flux quantum: " + line, file=sys.stderr)
        return vendor

    def preinit(self, args):
        """Pick a vendor for diagnostics. modify_jobspec resolves it again."""
        self._resolve_vendor(args)

    def _discover_candidates(self):
        """Return vendors found in the fluxion graph, or None to fall back to
        the registered backends (no handle, no fluxion, or empty graph)."""
        try:
            vendors = discover_registry_vendors(flux.Flux())
            if vendors:
                return sorted(vendors)
        except Exception:
            pass
        return None

    def modify_jobspec(self, args, jobspec):
        """Submit the user work held and rewrite this submit into the scout,
        so one flux submit produces the pair."""
        vendor = self._resolve_vendor(args, quiet=True)
        if not vendor:
            return  # not a quantum submit

        backend = get_backend(vendor)
        options = backend.scout_options(args) if backend else {}
        job_env = backend.job_environment(options) if backend else {}
        handle = flux.Flux()
        main_id = prepare_pair(
            handle, jobspec, vendor, scout_cores=1, options=options, job_env=job_env
        )
        print(
            "flux quantum: held classical job {} (vendor={}); this submit "
            "launches its scout".format(main_id, vendor),
            file=sys.stderr,
        )

    def validate(self, jobspec):
        """Fail before submission when the vendor credentials are missing, so a
        job is not dispatched only to fail after taking resources."""
        try:
            vendor = jobspec.getattr("system.quantum.vendor")
        except KeyError:
            return  # not a quantum job
        backend = get_backend(vendor)
        if backend is None:
            raise ValueError("quantum: no backend for vendor '{}'".format(vendor))
        ok, msg = backend.credentials_present()
        if not ok:
            raise ValueError(msg)
