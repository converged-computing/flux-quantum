import sys
import types
import importlib
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
    monkeypatch.setitem(sys.modules, "flux", m_flux)
    monkeypatch.setitem(sys.modules, "flux.cli", m_cli)
    monkeypatch.setitem(sys.modules, "flux.cli.plugin", m_p)
    monkeypatch.setenv("FLUX_QUANTUM_MOCK", "1")
    for name in [m for m in sys.modules if m.startswith("flux_quantum")]:
        del sys.modules[name]
    return CLIPlugin


def _load_shim():
    """Load cli-plugins/quantum.py by path, exactly as flux's loader does."""
    import importlib.util
    import os

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
    # core options always present
    assert {"--vendor", "--select"} <= names
    # each registered vendor backend contributes its own namespaced options
    assert "--ibm-backend" in names and "--braket-device" in names


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
    # stub a flux handle so _discover_candidates can "open" one
    import sys

    sys.modules["flux"].Flux = lambda *a, **k: object()
    cli = importlib.import_module("flux_quantum.cli")

    # registry reports only ibm+braket (no creds under mock env) -> no usable
    monkeypatch.setattr(
        cli, "discover_registry_vendors", lambda h: {"ibm", "braket"}, raising=False
    )
    # discover_registry_vendors is imported lazily inside the method, so patch
    # it at the selector module too
    import flux_quantum.selector as sel

    monkeypatch.setattr(sel, "discover_registry_vendors", lambda h: {"ibm", "braket"})
    plugin = cli.QuantumCLIPlugin("submit")
    import pytest

    with pytest.raises(SystemExit):
        plugin.preinit(_make_args(vendor=None, select="any"))

    # registry reports mock -> selected mock
    monkeypatch.setattr(sel, "discover_registry_vendors", lambda h: {"mock"})
    plugin2 = cli.QuantumCLIPlugin("submit")
    plugin2.preinit(_make_args(vendor=None, select="any"))
    assert plugin2._chosen == "mock"


class _FakeJS:
    """Minimal stand-in for flux.job.Jobspec: wraps a dict, supports the dotted
    setattr/getattr under attributes that the plugin uses."""

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
        import json

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
    # the CLASSICAL that was submitted: held, wrapped, vendor stamped
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
    # the jobspec flux will submit is now the SCOUT: has the qpu, is NOT held,
    # and runs scout.py referencing the held classical id
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


def test_prepare_pair_cancels_held_classical_on_populate_failure(stub_flux_cli):
    calls_seen = {}

    def boom_populate(handle, vendors):
        raise RuntimeError("add_subgraph failed")

    def rec_cancel(handle, jobid, reason):
        calls_seen["cancelled"] = jobid

    with pytest.raises(SystemExit):
        _run_prepare(stub_flux_cli, populate=boom_populate, cancel=rec_cancel)
    # the held classical must be cancelled so it does not sit forever
    assert calls_seen.get("cancelled") == 12345


def test_scout_duration_covers_the_classical(stub_flux_cli):
    """The scout now outlives the classical (it holds the qpu allocation), so it
    must not be given a shorter walltime than the job it is holding it for."""
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


def test_unlimited_classical_keeps_scout_unlimited(stub_flux_cli):
    """duration 0 means no limit; it must stay 0, not become a finite number."""
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
