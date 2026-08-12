"""Flux CLI submission plugin for quantum coscheduling (prefix: quantum).

Runs in the user's process at submit time (flux submit/run/batch), BEFORE the
jobspec is signed -- so it may hold the user's vendor credentials, probe the
backends for live signals, pick a vendor, and stamp the choice onto the
jobspec. The credential stays with the user's application.

CLI options (namespaced by the "quantum" prefix):
    --quantum-vendor VENDOR      request a specific vendor (e.g. ibm, braket)
    --quantum-select POLICY      pick automatically: any | queue | cost
    (each registered vendor backend contributes its own namespaced options)

Discovery order for candidates:
    1. --quantum-vendor if given (single candidate)
    2. otherwise the vendors discovered in the fluxion qdevice_* registry
       (via the resource.find RPC), falling back to the vendors that have a
       registered backend if the registry is empty or unreachable

This plugin owns the split: it submits the user's work as a HELD classical job
(attributes.system.hold=1 -- our fluxion hold/reserve) and rewrites the submit
into the scout. The scout opens the vendor session, posts it to the classical
job's eventlog as a memo, and releases it. The handoff needs no shared
filesystem -- see flux_quantum/scout.py.
"""

import os
import sys

from flux.cli.plugin import CLIPlugin

from .selector import select_vendor, SelectionError
from .backends import get_backend
from .launch import build_scout_jobspec

#: subcommands where quantum submission makes sense
_ACTIVE_PROGS = ("submit", "run", "batch", "bulksubmit", "alloc")


def _wrap_commands(jobspec_dict, wrap_path):
    """Wrap every task command so the classical job blocks for the session id
    (deposited by the scout) and exports it as QUANTUM_SESSION_ID before exec."""
    for task in jobspec_dict.get("tasks", []):
        cmd = list(task.get("command", []))
        task["command"] = ["flux", "python", wrap_path, "--"] + cmd


def _safe_cancel(cancel_fn, handle, jobid, reason):
    """Best-effort cancel of the held classical so a failed setup never leaves a
    job parked forever. Never raises."""
    try:
        cancel_fn(handle, jobid, reason)
    except Exception as exc:  # pragma: no cover - best effort
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
    submit_fn=None,
    populate_fn=None,
    get_graph_fn=None,
    cancel_fn=None,
    wrap_path=None,
    scout_path=None,
):
    """The production quantum-submit core: submit the user's work as a HELD
    classical job, gate on it entering the queue, then rewrite ``jobspec`` IN
    PLACE into the scout that will release the classical. Returns the classical
    (main) jobid.

    ``flux submit`` submits whatever this leaves in ``jobspec`` -- so it ends up
    submitting the scout, while the held classical (the user's real work) is
    submitted here and released later by the scout.

    All flux operations are injectable so this is unit-testable without a broker;
    ``modify_jobspec`` supplies the real ones.
    """
    import copy
    import json

    if submit_fn is None:
        from flux.job import submit as submit_fn
    if cancel_fn is None:
        from flux.job import cancel as cancel_fn
    if populate_fn is None or get_graph_fn is None:
        from . import graph as _graph

        populate_fn = populate_fn or _graph.populate
        get_graph_fn = get_graph_fn or _graph.get_live_graph
    if wrap_path is None:
        wrap_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wrap.py")

    # 1. CLASSICAL = the user's work, wrapped to wait for the session, born held.
    classical = copy.deepcopy(jobspec.jobspec)
    # The scout now outlives the classical (it holds the qpu allocation and the
    # vendor session for the whole run), so it needs at least the classical's
    # walltime plus headroom for the release + startup. 0 means "no limit" in a
    # jobspec, and must stay 0 rather than become a finite number.
    classical_duration = (
        classical.get("attributes", {}).get("system", {}).get("duration", 0) or 0
    )
    scout_duration = 0 if not classical_duration else classical_duration + 300
    _wrap_commands(classical, wrap_path)
    sysattr = classical.setdefault("attributes", {}).setdefault("system", {})
    sysattr["hold"] = 1
    quantum = sysattr.setdefault("quantum", {})
    quantum["vendor"] = vendor

    # 2. Submit held and GATE. If the cluster runs feasibility validation, an
    #    unsatisfiable request is rejected at ingest and submit raises -- abort
    #    the whole `flux submit` (no scout, no quantum quota). If feasibility is
    #    off, the job is guaranteed to enter the queue and we proceed.
    try:
        main_id = int(submit_fn(handle, json.dumps(classical)))
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(
            "flux quantum: classical request not accepted "
            "(unsatisfiable, or ingest error): {}".format(exc)
        )

    # 3. Ensure the vendor device is in the graph and derive the scout's
    #    classical foothold from the LIVE graph (so node->...->core matches the
    #    real hierarchy). On any failure, cancel the held classical.
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

    # 4. Rewrite THIS jobspec into the scout (foothold + exclusive qpu, running
    #    scout.py which opens the session and releases main_id).
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
    out.pop("hold", None)  # the scout must NOT be held; it runs immediately
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
        try:
            from .backends import backend_classes

            for backend_cls in backend_classes():
                backend_cls.add_options(self.add_option)
        except Exception:  # pragma: no cover - defensive
            pass
        # cache the choice from preinit for modify_jobspec/validate
        self._chosen = None

    def _is_quantum(self, args):
        # activated when the user asks for a vendor or an auto-select policy
        return bool(getattr(args, "vendor", None) or getattr(args, "select", None))

    def _resolve_vendor(self, args, quiet=False):
        """Resolve the vendor for this submit from ARGS (explicit
        --quantum-vendor, or --quantum-select policy), caching it on the
        instance. Returns the vendor name, or None if this is not a quantum
        submit.

        Called from BOTH preinit and modify_jobspec because flux runs those
        hooks on DIFFERENT plugin instances (jobspec.apply_options builds its
        own CLIPluginRegistry), so any vendor cached by preinit is NOT visible
        in modify_jobspec -- each instance must resolve from args. Diagnostics
        go to stderr (stdout carries the jobid that `cid=$(flux submit ...)`
        captures); pass quiet=True to suppress them on the second resolution.
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
            # --quantum-select with no explicit vendor: discover candidates from
            # the fluxion qdevice_* registry; fall back to registered backends.
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
        """Probe backends with the user's creds and pick a vendor (diagnostics
        only; modify_jobspec re-resolves and does the authoritative stamping)."""
        self._resolve_vendor(args)

    def _discover_candidates(self):
        """Discover vendor candidates from the live fluxion qdevice_* registry.

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
        """THE quantum submit path. When --quantum-* is present this submits the
        user's work as a HELD classical job, gates on it entering the queue, and
        rewrites `jobspec` into the scout that releases it -- so one `flux
        submit` produces the classical+scout pair.

        Resolves the vendor from ARGS (not preinit's cache): flux runs preinit
        and modify_jobspec on different plugin instances, so self._chosen may be
        None here even though preinit ran.
        """
        vendor = self._resolve_vendor(args, quiet=True)
        if not vendor:
            return  # not a quantum submit; leave the jobspec untouched

        from .backends import get_backend

        backend = get_backend(vendor)
        options = backend.scout_options(args) if backend else {}

        import flux

        handle = flux.Flux()
        main_id = prepare_pair(handle, jobspec, vendor, scout_cores=1, options=options)
        print(
            "flux quantum: held classical job {} (vendor={}); this submit "
            "launches its scout".format(main_id, vendor),
            file=sys.stderr,
        )

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
            raise ValueError("quantum: no backend for vendor '{}'".format(vendor))
        ok, msg = backend.credentials_present()
        if not ok:
            raise ValueError(msg)
