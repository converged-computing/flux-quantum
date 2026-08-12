import sys
import importlib
import pytest


def _fresh(mock, monkeypatch):
    """Import flux_quantum.backends fresh with FLUX_QUANTUM_MOCK set/unset."""
    if mock:
        monkeypatch.setenv("FLUX_QUANTUM_MOCK", "1")
    else:
        monkeypatch.delenv("FLUX_QUANTUM_MOCK", raising=False)
    # also clear real vendor creds so tests are deterministic
    for v in ("QISKIT_IBM_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(v, raising=False)
    for name in [m for m in sys.modules if m.startswith("flux_quantum")]:
        del sys.modules[name]
    return importlib.import_module("flux_quantum.backends")


@pytest.fixture
def backends_real(monkeypatch):
    return _fresh(False, monkeypatch)


@pytest.fixture
def backends_mock(monkeypatch):
    return _fresh(True, monkeypatch)


@pytest.fixture
def fresh(monkeypatch):
    """Return the _fresh callable so a test can choose creds/env itself."""

    def _make(mock=False, env=None):
        b = _fresh(mock, monkeypatch)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        return b

    return _make
