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
import errno
import json
import os
import sys

import flux
from flux.cli.plugin import CLIPlugin
from flux.job import cancel, submit

from . import graph, qresource
from .selector import select_vendor, SelectionError, discover_registry_vendors
from .backends import BackendError, get_backend
from .launch import build_scout_jobspec

# subcommands where quantum submission makes sense
_ACTIVE_PROGS = ("submit", "run", "batch", "bulksubmit", "alloc")


def _wrap_commands(jobspec_dict, wrap_path):
    """Wrap each task command so the job gets QUANTUM_SESSION_ID before exec."""
    for task in jobspec_dict.get("tasks", []):
        cmd = list(task.get("command", []))
        task["command"] = ["flux", "python", wrap_path, "--"] + cmd


def _check_pair_fits(handle, classical, scout, rpc_fn=None):
    """Refuse a pair the graph can never hold, before either half exists.

    Each half is checked alone against the whole graph through fluxion's
    feasibility.check, which is what the job manager asks before it accepts
    a job. This catches a missing qdevice or a half bigger than the machine,
    not a busy machine. Fit against what is reachable right now is the
    jobtap plugin's admission check.
    """
    try:
        for half in (scout, classical):
            payload = {"jobspec": half}
            if rpc_fn is None:
                handle.rpc("feasibility.check", payload).get()
            else:
                rpc_fn(handle, payload)
    except OSError as exc:
        # ENODEV and EINVAL from feasibility.check mean the graph can never
        # hold this half. Anything else means the question could not be
        # asked, and that must not block a submit that worked before the
        # check existed.
        if exc.errno not in (errno.EBUSY, errno.ENODEV, errno.EINVAL):
            return
        raise SystemExit(
            "flux quantum: this pair cannot be scheduled on this cluster. The "
            "classical half needs its cores and the scout needs one more plus "
            "the vendor device, and the graph cannot hold both. Submitting "
            "anyway would open a vendor session for a pair that cannot run. "
            "({})".format(exc)
        )
    except Exception:
        # no usable handle, so there is nothing to ask
        return


def _safe_cancel(cancel_fn, handle, jobid, reason):
    """Cancel the held job so a failed setup does not park it forever."""
    try:
        cancel_fn(handle, jobid, reason)
    except Exception as exc:
        print(
            "flux quantum: WARNING failed to cancel job {}: {}".format(jobid, exc),
            file=sys.stderr,
        )


def flux_dry_run(args):
    """True when flux's own --dry-run is set, as opposed to --quantum-dry-run.

    Inside a plugin callback flux hands us a proxy that aliases the bare
    option names to ours, so args.dry_run is --quantum-dry-run there. Flux's
    flag lives on the namespace underneath the proxy.
    """
    ns = getattr(args, "_ns", args)
    return bool(getattr(ns, "dry_run", False))


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
    dry_run=False,
):
    """Submit the user work held, then rewrite the jobspec in place into the
    scout that releases it. Returns the id of the held job.

    With dry_run nothing is submitted. The classical half is printed to
    stderr, the jobspec is still rewritten into the scout with a placeholder
    job id of 0, and flux prints that as it would any dry run. Without this,
    flux's --dry-run skipped only flux's own submit and left a held job
    parked with no scout to ever release it.

    flux submits whatever is left in the jobspec, so it submits the scout.
    The flux calls are injectable so this is testable without a broker.
    """
    submit_fn = submit_fn or submit
    cancel_fn = cancel_fn or cancel
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
    # the jobtap plugin keeps a core budget over every unfinished pair, and it
    # needs the classical size to do that. Counting here rather than walking the
    # jobspec in C.
    quantum["cores"] = qresource.count_cores(classical)

    if job_env:
        sysattr.setdefault("environment", {}).update(job_env)

    # Populate and ask fluxion before creating anything, so a pair the graph
    # can never hold is refused with no held job to clean up. The scout shape
    # comes from the live graph so the node to core path matches reality.
    try:
        populate_fn(handle, [vendor])
        live = get_graph_fn(handle)
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(
            "flux quantum: could not prepare the quantum graph: {}".format(exc)
        )

    # only the resources matter to the question, so the job id is a placeholder
    try:
        probe = build_scout_jobspec(
            vendor,
            0,
            ncores=scout_cores,
            duration=scout_duration,
            live_graph=live,
            scout_path=scout_path,
            options=options,
        )
    except Exception:
        probe = None
    if probe is not None:
        _check_pair_fits(handle, classical, probe)

    if dry_run:
        print(
            "flux quantum: dry run, nothing submitted. The held classical job "
            "would be:",
            file=sys.stderr,
        )
        print(json.dumps(classical, indent=2), file=sys.stderr)
        print(
            "flux quantum: and the scout, printed below by flux, would release "
            "it. Its --job 0 stands in for the classical job id.",
            file=sys.stderr,
        )
        main_id = 0
    else:
        # if feasibility validation is on, an unsatisfiable request is rejected
        # here and we abort before spending any quantum quota
        try:
            main_id = int(submit_fn(handle, json.dumps(classical)))
        except SystemExit:
            raise
        except Exception as exc:
            raise SystemExit(
                "flux quantum: classical request not accepted "
                "(unsatisfiable, or ingest error): {}".format(exc)
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
        if not dry_run:
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


def _opt(args, name):
    """A plugin option off the parsed args, by its prefixed name first.

    flux proxies args.device to args.quantum_device inside plugin callbacks,
    but flux has options of its own and a bare name could be one of them.
    """
    for attr in ("quantum_" + name, name):
        value = getattr(args, attr, None)
        if value is not None:
            return value
    return None


def common_options(args, jobspec=None, environ=None):
    """The COMMON_OPTIONS from the parsed args, with their defaults.

    The hold limit follows the job's duration when it has one, so setting a
    walltime sets it. FLUX_QUANTUM_MOCK makes every submit a dry run, since
    nothing in a mock run should reach a billed device.
    """
    environ = os.environ if environ is None else environ
    duration = 0
    if jobspec:
        duration = (
            jobspec.get("attributes", {}).get("system", {}).get("duration", 0) or 0
        )
    hold_max = _opt(args, "hold_max")
    wait = _opt(args, "wait")
    return {
        "device": _opt(args, "device") or None,
        "hold": _opt(args, "hold") or "session",
        "hold_max": (
            float(hold_max) if hold_max else float(duration + 300 if duration else 900)
        ),
        "wait": float(wait) if wait else 0.0,
        "dry_run": bool(_opt(args, "dry_run"))
        or bool(environ.get("FLUX_QUANTUM_MOCK")),
    }


def scout_options(backend, args, jobspec=None):
    """What the scout is given for this vendor: the common options, made a
    dry run if asked, then mapped by the backend. Raises BackendError for a
    hold the vendor cannot take."""
    common = common_options(args, jobspec)
    if common["dry_run"]:
        common = backend.dry_run(common)
    return backend.scout_options(common)


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
        # the same options whichever vendor it is. Each backend maps them to
        # its own terms, and operator tuning lives in FLUX_QUANTUM_* variables
        self.add_option(
            "--device",
            metavar="NAME",
            default=None,
            help="the vendor's name for the device: a Braket ARN, an IonQ "
            "backend, a QRMI resource id. Each vendor has a default",
        )
        self.add_option(
            "--hold",
            metavar="MODE",
            default=None,
            help="session takes the vendor's real hold, a hybrid job on "
            "Braket, a session on IonQ and IBM, the default. probe submits a "
            "front of queue job and holds nothing",
        )
        self.add_option(
            "--hold-max",
            metavar="SECONDS",
            default=None,
            help="give the hold up after this long, so a scout that is never "
            "released stops costing. Default is the job's duration plus "
            "300, or 900 when it has none",
        )
        self.add_option(
            "--wait",
            metavar="SECONDS",
            default=None,
            help="give up if the hold is not ours after this long, and cancel "
            "the held job. Default 0, as long as the scout may run",
        )
        self.add_option(
            "--dry-run",
            action="store_true",
            default=False,
            help="run on the vendor's simulator. FLUX_QUANTUM_MOCK implies it",
        )
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

        try:
            backend = get_backend(vendor)
            options = scout_options(backend, args, jobspec.jobspec) if backend else {}
        except BackendError as e:
            raise SystemExit("flux quantum: {}".format(e))
        job_env = backend.job_environment(options) if backend else {}
        handle = flux.Flux()
        dry_run = flux_dry_run(args)
        main_id = prepare_pair(
            handle,
            jobspec,
            vendor,
            scout_cores=1,
            options=options,
            job_env=job_env,
            dry_run=dry_run,
        )
        if dry_run:
            return
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
        try:
            backend = get_backend(vendor)
        except BackendError as e:
            raise ValueError(str(e))
        if backend is None:
            raise ValueError("quantum: no backend for vendor '{}'".format(vendor))
