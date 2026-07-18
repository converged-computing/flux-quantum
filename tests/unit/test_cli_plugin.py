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
    path = os.path.join(os.path.dirname(__file__), "..", "..",
                        "cli-plugins", "quantum.py")
    spec = importlib.util.spec_from_file_location("quantum_shim", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shim_exposes_single_plugin(stub_flux_cli):
    shim = _load_shim()
    subclasses = [
        getattr(shim, a) for a in dir(shim)
        if isinstance(getattr(shim, a), type)
        and issubclass(getattr(shim, a), stub_flux_cli)
        and getattr(shim, a) is not stub_flux_cli
    ]
    assert [c.__name__ for c in subclasses] == ["QuantumCLIPlugin"]


def test_plugin_registers_options_with_quantum_prefix(stub_flux_cli):
    shim = _load_shim()
    plugin = shim.QuantumCLIPlugin("submit")   # flux instantiates as entry(prog)
    assert plugin.prefix == "quantum"
    names = {n for n, _ in plugin.options}
    assert names == {"--vendor", "--select", "--rendezvous"}


def test_plugin_inactive_for_other_progs(stub_flux_cli):
    shim = _load_shim()
    plugin = shim.QuantumCLIPlugin("jobs")   # not a submit-like subcommand
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
    monkeypatch.setattr(cli, "discover_registry_vendors",
                        lambda h: {"ibm", "braket"}, raising=False)
    # discover_registry_vendors is imported lazily inside the method, so patch
    # it at the selector module too
    import flux_quantum.selector as sel
    monkeypatch.setattr(sel, "discover_registry_vendors",
                        lambda h: {"ibm", "braket"})
    plugin = cli.QuantumCLIPlugin("submit")
    import pytest
    with pytest.raises(SystemExit):
        plugin.preinit(_make_args(vendor=None, select="any", rendezvous=None))

    # registry reports mock -> selected mock
    monkeypatch.setattr(sel, "discover_registry_vendors", lambda h: {"mock"})
    plugin2 = cli.QuantumCLIPlugin("submit")
    plugin2.preinit(_make_args(vendor=None, select="any", rendezvous=None))
    assert plugin2._chosen == "mock"


def test_explicit_vendor_skips_discovery(stub_flux_cli, monkeypatch):
    import sys
    sys.modules["flux"].Flux = lambda *a, **k: object()
    cli = importlib.import_module("flux_quantum.cli")
    import flux_quantum.selector as sel
    # if discovery were consulted it would raise; explicit vendor must skip it
    monkeypatch.setattr(sel, "discover_registry_vendors",
                        lambda h: (_ for _ in ()).throw(AssertionError("should not discover")))
    plugin = cli.QuantumCLIPlugin("submit")
    plugin.preinit(_make_args(vendor="mock", select=None, rendezvous=None))
    assert plugin._chosen == "mock"


class _FakeJS:
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


def test_split_and_submit_makes_held_main_then_quantum_scout(stub_flux_cli):
    """ONE path: split the user's jobspec into a held classical MAIN and a
    classical+quantum SCOUT, and submit BOTH (main first, for its id)."""
    import json
    cli = importlib.import_module("flux_quantum.cli")

    order = []
    def fake_submit(handle, js):
        order.append(js)
        return len(order)                 # main -> 1, scout -> 2

    main = _FakeJS([{"type": "slot", "with": [
        {"type": "node", "count": 4, "with": [{"type": "core", "count": 8}]}]}])

    main_id, scout_id = cli.split_and_submit(
        None, main, "ibm", "/tmp/rdv", submit_fn=fake_submit)

    assert (main_id, scout_id) == (1, 2)          # two jobs, main first
    assert order[0] is main
    assert main.getattr("system.hold") == 1        # main held classical
    assert main.getattr("system.quantum.vendor") == "ibm"
    scout = json.loads(order[1])                   # scout classical + quantum
    rtypes = [r["type"] for r in scout["resources"]]
    assert "node" in rtypes and "qvendor_ibm" in rtypes    # two top-level resources
    node = next(r for r in scout["resources"] if r["type"] == "node")
    slot = next(c for c in node["with"] if c["type"] == "slot")
    assert any(c["type"] == "core" for c in slot["with"])
    assert "1" in " ".join(scout["tasks"][0]["command"])   # references main id
