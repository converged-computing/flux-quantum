import importlib.util
import json
import os
import sys
import types

import pytest


@pytest.fixture
def stub_flux_cli(monkeypatch):
    """Provide a minimal flux.cli.plugin.CLIPlugin so cli.py imports w/o flux."""
    m_flux = types.ModuleType("flux")
    m_cli = types.ModuleType("flux.cli")
    m_p = types.ModuleType("flux.cli.plugin")

    class CLIPlugin:
        def __init__(self, prog, prefix="ex", version=None):
            self.prog = prog[5:] if prog.startswith("flux ") else prog
            self.prefix = prefix
            self.options = []

        def add_option(self, name, **kw):
            self.options.append((name, kw))

    m_p.CLIPlugin = CLIPlugin
    m_flux.cli = m_cli
    m_cli.plugin = m_p

    # cli.py imports submit and cancel at module level. The tests inject
    # their own, so these only have to exist.
    def _stub(*a, **k):
        raise AssertionError("flux.job is a stub in the unit tests")

    m_job = types.ModuleType("flux.job")
    m_job.submit = m_job.cancel = _stub
    m_flux.job = m_job
    monkeypatch.setitem(sys.modules, "flux", m_flux)
    monkeypatch.setitem(sys.modules, "flux.cli", m_cli)
    monkeypatch.setitem(sys.modules, "flux.cli.plugin", m_p)
    monkeypatch.setitem(sys.modules, "flux.job", m_job)
    monkeypatch.setenv("FLUX_QUANTUM_MOCK", "1")
    for name in [m for m in sys.modules if m.startswith("flux_quantum")]:
        del sys.modules[name]
    return CLIPlugin


def _load_shim():
    """Load the quantum plugin by path, the way the flux loader does."""
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "cli-plugins", "quantum.py"
    )
    spec = importlib.util.spec_from_file_location("quantum_shim", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shim_exposes_single_plugin(stub_flux_cli):
    shim = _load_shim()
    subclasses = [
        getattr(shim, a)
        for a in dir(shim)
        if isinstance(getattr(shim, a), type)
        and issubclass(getattr(shim, a), stub_flux_cli)
        and getattr(shim, a) is not stub_flux_cli
    ]
    assert [c.__name__ for c in subclasses] == ["QuantumCLIPlugin"]


def test_plugin_registers_options_with_quantum_prefix(stub_flux_cli):
    shim = _load_shim()
    plugin = shim.QuantumCLIPlugin("submit")  # flux instantiates as entry(prog)
    assert plugin.prefix == "quantum"
    names = {n for n, _ in plugin.options}
    # the same options whichever vendor it is
    assert {
        "--vendor",
        "--select",
        "--device",
        "--hold",
        "--hold-max",
        "--wait",
        "--dry-run",
    } <= names
    # and nothing vendor specific
    assert not [
        n for n in names if n.startswith(("--ibm", "--braket", "--ionq", "--mock"))
    ]


def test_plugin_inactive_for_other_progs(stub_flux_cli):
    shim = _load_shim()
    plugin = shim.QuantumCLIPlugin("jobs")  # not a submit-like subcommand
    assert plugin.options == []


def _make_args(**kw):
    class A:
        pass

    a = A()
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_preinit_uses_registry_discovery(stub_flux_cli, monkeypatch):
    # stub a flux handle so _discover_candidates can open one
    sys.modules["flux"].Flux = lambda *a, **k: object()
    cli = importlib.import_module("flux_quantum.cli")

    # only ibm and braket in the graph, and neither has creds under the mock
    # env, so nothing is usable
    monkeypatch.setattr(cli, "discover_registry_vendors", lambda h: {"ibm", "braket"})
    plugin = cli.QuantumCLIPlugin("submit")
    with pytest.raises(SystemExit):
        plugin.preinit(_make_args(vendor=None, select="any"))

    # mock in the graph, so mock is selected
    monkeypatch.setattr(cli, "discover_registry_vendors", lambda h: {"mock"})
    plugin2 = cli.QuantumCLIPlugin("submit")
    plugin2.preinit(_make_args(vendor=None, select="any"))
    assert plugin2._chosen == "mock"


class _FakeJS:
    """Minimal stand in for a flux Jobspec. Wraps a dict and supports the
    dotted setattr and getattr the plugin uses."""

    def __init__(self, resources):
        self.jobspec = {"resources": resources, "attributes": {}}

    def setattr(self, key, value):
        d = self.jobspec.setdefault("attributes", {})
        parts = key.split(".")
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        d[parts[-1]] = value

    def getattr(self, key):
        d = self.jobspec.get("attributes", {})
        for p in key.split("."):
            d = d[p]
        return d


def _fake_jobspec(command):
    js = _FakeJS(
        [
            {
                "type": "node",
                "count": 1,
                "with": [
                    {
                        "type": "slot",
                        "count": 1,
                        "label": "task",
                        "with": [{"type": "core", "count": 1}],
                    }
                ],
            }
        ]
    )
    js.jobspec["tasks"] = [
        {"command": list(command), "slot": "task", "count": {"per_slot": 1}}
    ]
    return js


def _live_graph():
    return {
        "nodes": [
            {
                "id": "0",
                "metadata": {
                    "type": "cluster",
                    "rank": -1,
                    "paths": {"containment": "/c0"},
                },
            },
            {
                "id": "1",
                "metadata": {
                    "type": "node",
                    "rank": -1,
                    "paths": {"containment": "/c0/n0"},
                },
            },
            {
                "id": "2",
                "metadata": {
                    "type": "core",
                    "rank": -1,
                    "paths": {"containment": "/c0/n0/c0"},
                },
            },
        ],
        "edges": [
            {"source": "0", "target": "1", "metadata": {"subsystem": "containment"}},
            {"source": "1", "target": "2", "metadata": {"subsystem": "containment"}},
        ],
    }


def _run_prepare(
    stub_flux_cli, submit=None, populate=None, get_graph=None, cancel=None
):
    from flux_quantum import cli

    calls = {"submitted": [], "populated": [], "cancelled": []}

    def default_submit(handle, jobspec_json):
        calls["submitted"].append(json.loads(jobspec_json))
        return 12345

    def default_populate(handle, vendors):
        calls["populated"].append(list(vendors))

    def default_cancel(handle, jobid, reason):
        calls["cancelled"].append((jobid, reason))

    js = _fake_jobspec(["myprog", "--flag"])
    main_id = cli.prepare_pair(
        handle=None,
        jobspec=js,
        vendor="mock",
        submit_fn=submit or default_submit,
        populate_fn=populate or default_populate,
        get_graph_fn=get_graph or (lambda h: _live_graph()),
        cancel_fn=cancel or default_cancel,
    )
    return main_id, js, calls


def test_prepare_pair_submits_held_wrapped_classical(stub_flux_cli):
    main_id, js, calls = _run_prepare(stub_flux_cli)
    assert main_id == 12345
    # held, wrapped, vendor stamped
    classical = calls["submitted"][0]
    sysattr = classical["attributes"]["system"]
    assert sysattr["hold"] == 1
    assert sysattr["quantum"]["vendor"] == "mock"
    cmd = classical["tasks"][0]["command"]
    assert cmd[:3] == ["flux", "python"] or "wrap.py" in cmd[2]
    assert "myprog" in cmd  # user command preserved
    assert calls["populated"] == [["mock"]]


def test_prepare_pair_rewrites_jobspec_into_scout(stub_flux_cli):
    main_id, js, calls = _run_prepare(stub_flux_cli)
    # what flux submits is now the scout
    types = [r["type"] for r in js.jobspec["resources"]]
    assert any(t.startswith("qdevice_") for t in types)
    assert "hold" not in js.jobspec.get("attributes", {}).get("system", {})
    scout_cmd = " ".join(js.jobspec["tasks"][0]["command"])
    assert "scout.py" in scout_cmd and str(main_id) in scout_cmd


def test_prepare_pair_gate_aborts_on_unsatisfiable(stub_flux_cli):
    def rejecting_submit(handle, jobspec_json):
        raise OSError("unsatisfiable request")

    with pytest.raises(SystemExit):
        _run_prepare(stub_flux_cli, submit=rejecting_submit)


def test_populate_failure_leaves_no_held_classical_behind(stub_flux_cli):
    """The graph is set up before either half is created, so a graph failure
    has nothing to clean up."""
    seen = {}

    def boom_populate(handle, vendors):
        raise RuntimeError("add_subgraph failed")

    def rec_cancel(handle, jobid, reason):
        seen["cancelled"] = jobid

    def rec_submit(handle, jobspec_json):
        seen["submitted"] = True
        return 12345

    with pytest.raises(SystemExit):
        _run_prepare(
            stub_flux_cli,
            populate=boom_populate,
            cancel=rec_cancel,
            submit=rec_submit,
        )

    assert (
        "submitted" not in seen
    ), "the classical was created before the graph was ready"
    assert "cancelled" not in seen, "nothing should need cancelling"


def test_scout_duration_covers_the_classical(stub_flux_cli):
    """The scout outlives the classical, so it needs at least its walltime."""
    from flux_quantum import cli

    js = _fake_jobspec(["myprog"])
    js.jobspec.setdefault("attributes", {}).setdefault("system", {})["duration"] = 3600

    cli.prepare_pair(
        handle=None,
        jobspec=js,
        vendor="mock",
        submit_fn=lambda h, j: 999,
        populate_fn=lambda h, v: None,
        get_graph_fn=lambda h: _live_graph(),
        cancel_fn=lambda *a: None,
    )
    assert js.jobspec["attributes"]["system"]["duration"] > 3600


def test_scout_duration_covers_the_wait_too(stub_flux_cli):
    """The scout can wait --quantum-wait seconds for the vendor before the
    classical even starts, so its walltime has to cover both, or flux kills
    it mid-wait every time."""
    from flux_quantum import cli

    js = _fake_jobspec(["myprog"])
    js.jobspec.setdefault("attributes", {}).setdefault("system", {})["duration"] = 60

    cli.prepare_pair(
        handle=None,
        jobspec=js,
        vendor="mock",
        submit_fn=lambda h, j: 999,
        populate_fn=lambda h, v: None,
        get_graph_fn=lambda h: _live_graph(),
        cancel_fn=lambda *a: None,
        wait=1800,
    )
    assert js.jobspec["attributes"]["system"]["duration"] == 60 + 1800 + 300


def test_unlimited_classical_keeps_scout_unlimited(stub_flux_cli):
    """duration 0 means no limit and must stay 0."""
    from flux_quantum import cli

    js = _fake_jobspec(["myprog"])
    js.jobspec.setdefault("attributes", {}).setdefault("system", {})["duration"] = 0

    cli.prepare_pair(
        handle=None,
        jobspec=js,
        vendor="mock",
        submit_fn=lambda h, j: 999,
        populate_fn=lambda h, v: None,
        get_graph_fn=lambda h: _live_graph(),
        cancel_fn=lambda *a: None,
    )
    assert js.jobspec["attributes"]["system"]["duration"] == 0


def test_job_env_reaches_the_classical(stub_flux_cli):
    """A backend can add env to the classical job, which is how the QPU
    assignment gets there."""
    from flux_quantum import cli

    js = _fake_jobspec(["myprog"])
    submitted = {}

    cli.prepare_pair(
        handle=None,
        jobspec=js,
        vendor="mock",
        job_env={"QRMI_JOB_QPU_RESOURCES": "ibm_kingston"},
        submit_fn=lambda h, j: submitted.setdefault("js", json.loads(j)) and 0 or 7,
        populate_fn=lambda h, v: None,
        get_graph_fn=lambda h: _live_graph(),
        cancel_fn=lambda *a: None,
    )
    env = submitted["js"]["attributes"]["system"]["environment"]
    assert env["QRMI_JOB_QPU_RESOURCES"] == "ibm_kingston"


def test_classical_carries_its_core_count(stub_flux_cli):
    """The jobtap plugin budgets on this, and only the classical carries it so
    the scout is not counted twice."""
    from flux_quantum import cli

    js = _fake_jobspec(["myprog"])
    js.jobspec["resources"] = [
        {"type": "slot", "count": 4, "with": [{"type": "core", "count": 1}]}
    ]
    submitted = {}

    cli.prepare_pair(
        handle=None,
        jobspec=js,
        vendor="mock",
        submit_fn=lambda h, j: submitted.setdefault("js", json.loads(j)) and 0 or 7,
        populate_fn=lambda h, v: None,
        get_graph_fn=lambda h: _live_graph(),
        cancel_fn=lambda *a: None,
    )
    quantum = submitted["js"]["attributes"]["system"]["quantum"]
    assert quantum["cores"] == 4
    assert quantum["vendor"] == "mock"

    # what flux submits is the scout, and it carries no quantum attributes
    scout_sys = js.jobspec["attributes"]["system"]
    assert "quantum" not in scout_sys or "cores" not in scout_sys.get("quantum", {})


def test_check_pair_fits_rejects_when_the_pair_cannot_be_placed(stub_flux_cli):
    """A pair that cannot be scheduled together is refused before either half
    is created, because the scout would open a metered session for a classical
    job that cannot start."""
    import errno

    from flux_quantum import cli

    def rpc(handle, payload):
        raise OSError(errno.ENODEV, "unsatisfiable request")

    try:
        cli._check_pair_fits(None, {"a": 1}, {"b": 2}, rpc_fn=rpc)
    except SystemExit as exc:
        assert "cannot be scheduled" in str(exc)
    else:
        raise AssertionError("a pair that cannot be placed was allowed through")


def test_check_pair_fits_asks_feasibility_for_each_half(stub_flux_cli):
    """Both jobspecs are checked, each on its own, through the same
    feasibility question the job manager asks. No jobids, since neither job
    exists yet."""
    from flux_quantum import cli

    seen = []

    def rpc(handle, payload):
        seen.append(payload)
        return {}

    cli._check_pair_fits(None, {"classical": 1}, {"scout": 1}, rpc_fn=rpc)
    assert [p["jobspec"] for p in seen] == [{"scout": 1}, {"classical": 1}]
    assert all(set(p) == {"jobspec"} for p in seen)


def test_check_pair_fits_is_permissive_when_it_cannot_ask(stub_flux_cli):
    """An older fluxion serves no such method, and none may be loaded at all.
    Admission then falls back to the plugin's job.validate hook, which is how
    this worked before the check existed, so do not block a submit."""
    import errno

    from flux_quantum import cli

    def missing(handle, payload):
        raise OSError(errno.ENOSYS, "Function not implemented")

    cli._check_pair_fits(None, {"a": 1}, {"b": 2}, rpc_fn=missing)

    def broken(handle, payload):
        raise RuntimeError("no usable handle")

    cli._check_pair_fits(None, {"a": 1}, {"b": 2}, rpc_fn=broken)


def test_prepare_pair_asks_before_submitting_anything(stub_flux_cli, monkeypatch):
    """The check must happen before the classical is created, or a rejected pair
    still leaves a held job behind."""
    from flux_quantum import cli

    order = []

    def fake_check(handle, classical, scout, rpc_fn=None):
        order.append(("checked", classical, scout))

    monkeypatch.setattr(cli, "_check_pair_fits", fake_check)

    def submit(handle, jobspec_json):
        order.append(("submitted", None, None))
        return 12345

    _run_prepare(stub_flux_cli, submit=submit)
    assert order[0][0] == "checked", order
    # both halves were described to the check
    assert order[0][1] is not None and order[0][2] is not None
    assert any(x[0] == "submitted" for x in order)


def test_flux_dry_run_submits_nothing(stub_flux_cli, capsys):
    """flux --dry-run skips only flux's own submit. The classical half must
    not be submitted either, or a held job is left parked with no scout."""
    from flux_quantum import cli

    def submit(handle, jobspec_json):
        raise AssertionError("submitted the classical on a dry run")

    def cancel(handle, jobid, reason):
        raise AssertionError("cancelled a job that was never submitted")

    js = _fake_jobspec(["myprog"])
    main_id = cli.prepare_pair(
        handle=None,
        jobspec=js,
        vendor="mock",
        submit_fn=submit,
        populate_fn=lambda h, v: None,
        get_graph_fn=lambda h: _live_graph(),
        cancel_fn=cancel,
        dry_run=True,
    )
    assert main_id == 0
    # the classical half is shown, and the jobspec is still the scout so flux
    # prints that as the dry run output
    err = capsys.readouterr().err
    assert "nothing submitted" in err and '"hold": 1' in err
    types = [r["type"] for r in js.jobspec["resources"]]
    assert any(t.startswith("qdevice_") for t in types)
    assert "--job" in js.jobspec["tasks"][0]["command"]


def test_flux_dry_run_is_read_from_under_the_proxy(stub_flux_cli):
    """Inside a callback args.dry_run is aliased to --quantum-dry-run, so
    flux's own flag has to come from the namespace the proxy wraps."""
    from flux_quantum import cli

    class Proxy:
        def __init__(self, ns):
            object.__setattr__(self, "_ns", ns)

        def __getattr__(self, name):
            # the alias flux installs: dry_run -> quantum_dry_run
            return getattr(self._ns, {"dry_run": "quantum_dry_run"}.get(name, name))

    assert cli.flux_dry_run(Proxy(_make_args(dry_run=True, quantum_dry_run=False)))
    assert not cli.flux_dry_run(Proxy(_make_args(dry_run=False, quantum_dry_run=True)))
    assert not cli.flux_dry_run(_make_args())


def test_validate_checks_the_vendor_and_the_jobs_environment(
    stub_flux_cli, monkeypatch
):
    """The hook also runs in the ingest validator, whose environment is the
    broker's. Credentials are not there, so it checks the job's environment,
    the one flux copied from the submitting shell."""
    monkeypatch.delenv("FLUX_QUANTUM_MOCK", raising=False)
    for name in [m for m in sys.modules if m.startswith("flux_quantum")]:
        del sys.modules[name]
    shim = _load_shim()
    plugin = shim.QuantumCLIPlugin("submit")

    class JS:
        def __init__(self, attrs):
            self.attrs = attrs

        def getattr(self, key):
            if key not in self.attrs:
                raise KeyError(key)
            return self.attrs[key]

    creds = {
        "QRMI_JOB_QPU_RESOURCES": "ibm_kingston",
        "QRMI_JOB_QPU_TYPES": "qiskit-runtime-service",
        "ibm_kingston_QRMI_IBM_QRS_ENDPOINT": "x",
        "ibm_kingston_QRMI_IBM_QRS_IAM_ENDPOINT": "x",
        "ibm_kingston_QRMI_IBM_QRS_IAM_APIKEY": "x",
        "ibm_kingston_QRMI_IBM_QRS_SERVICE_CRN": "x",
    }
    # ibm is registered but has no credentials in this process. The job's
    # environment has them, and that is what is checked.
    plugin.validate(JS({"system.quantum.vendor": "ibm", "system.environment": creds}))
    plugin.validate(JS({}))  # not a quantum job
    with pytest.raises(ValueError, match="no backend for vendor 'rigetti'"):
        plugin.validate(JS({"system.quantum.vendor": "rigetti"}))
    # a job whose environment lacks one is refused, naming it
    short = dict(creds)
    del short["ibm_kingston_QRMI_IBM_QRS_IAM_APIKEY"]
    with pytest.raises(ValueError, match="ibm_kingston_QRMI_IBM_QRS_IAM_APIKEY"):
        plugin.validate(
            JS({"system.quantum.vendor": "ibm", "system.environment": short})
        )
    # and one with no environment at all, as a job submitted with --env=-*
    with pytest.raises(ValueError, match="missing"):
        plugin.validate(
            JS(
                {
                    "system.quantum.vendor": "ibm",
                    "system.environment": {
                        "QRMI_JOB_QPU_RESOURCES": "ibm_kingston",
                        "QRMI_JOB_QPU_TYPES": "qiskit-runtime-service",
                    },
                }
            )
        )


def test_common_options_have_defaults_and_follow_the_duration(
    stub_flux_cli, monkeypatch
):
    """Setting a walltime sets the hold limit, with the scout's slack."""
    monkeypatch.delenv("FLUX_QUANTUM_MOCK", raising=False)
    cli = importlib.import_module("flux_quantum.cli")

    c = cli.common_options(_make_args(), {"attributes": {"system": {"duration": 600}}})
    assert c == {
        "device": None,
        "hold": "session",
        "hold_max": 900.0,
        "wait": 0.0,
        "dry_run": False,
    }
    assert cli.common_options(_make_args(), None)["hold_max"] == 900.0

    c = cli.common_options(
        _make_args(
            quantum_device="ibm_fez",
            quantum_hold="probe",
            quantum_hold_max="30",
            quantum_wait="5",
            quantum_dry_run=True,
        )
    )
    assert c["device"] == "ibm_fez" and c["hold"] == "probe"
    assert c["hold_max"] == 30.0 and c["wait"] == 5.0 and c["dry_run"]


def test_the_prefixed_name_wins_over_a_bare_one(stub_flux_cli):
    """flux has a --wait of its own. The plugin's is read by its prefixed
    name first."""
    cli = importlib.import_module("flux_quantum.cli")
    assert cli._opt(_make_args(wait=True, quantum_wait="7"), "wait") == "7"
    assert cli._opt(_make_args(device="d"), "device") == "d"


def test_mock_makes_every_submit_a_dry_run(stub_flux_cli, monkeypatch):
    monkeypatch.setenv("FLUX_QUANTUM_MOCK", "1")
    cli = importlib.import_module("flux_quantum.cli")
    assert cli.common_options(_make_args())["dry_run"]


def _vendor(simulator=None, holds=("session",)):
    from flux_quantum.backends.base import Backend

    class V(Backend):
        name = "v"

        def scout_options(self, common):
            self.check_hold(common.get("hold"))
            return dict(common, mapped=True)

        def probe(self):
            raise NotImplementedError

    V.simulator = simulator
    V.holds = holds
    return V()


def test_scout_options_apply_the_dry_run_before_the_vendor_maps(
    stub_flux_cli, monkeypatch
):
    monkeypatch.delenv("FLUX_QUANTUM_MOCK", raising=False)
    cli = importlib.import_module("flux_quantum.cli")
    opts = cli.scout_options(_vendor(simulator="sim"), _make_args(quantum_dry_run=True))
    assert opts["device"] == "sim" and opts["dry_run"] and opts["mapped"]
    opts = cli.scout_options(_vendor(simulator="sim"), _make_args(quantum_device="qpu"))
    assert opts["device"] == "qpu" and not opts["dry_run"]


def test_a_hold_the_vendor_lacks_fails_at_submit(stub_flux_cli, monkeypatch):
    """Before anything is held, with the vendor's explanation."""
    from flux_quantum.backends import BackendError

    monkeypatch.delenv("FLUX_QUANTUM_MOCK", raising=False)
    cli = importlib.import_module("flux_quantum.cli")
    with pytest.raises(BackendError, match="no probe hold"):
        cli.scout_options(_vendor(), _make_args(quantum_hold="probe"))
    with pytest.raises(BackendError, match="unknown hold"):
        cli.scout_options(_vendor(), _make_args(quantum_hold="grab"))
