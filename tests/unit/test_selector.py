import importlib
import pytest


def _selector():
    return importlib.import_module("flux_quantum.selector")


def test_no_usable_vendor_raises(backends_real):
    sel = _selector()
    with pytest.raises(sel.SelectionError) as e:
        sel.select_vendor()
    # error lists the missing creds for each vendor
    assert "QISKIT_IBM_TOKEN" in str(e.value)


def test_select_ibm_with_token(fresh):
    fresh(mock=False, env={"QISKIT_IBM_TOKEN": "x"})
    sel = _selector()
    vendor, sig, log = sel.select_vendor(policy="any")
    assert vendor == "ibm"


def test_explicit_braket_without_creds_raises(backends_real):
    sel = _selector()
    with pytest.raises(sel.SelectionError):
        sel.select_vendor(candidates=["braket"])


def test_mock_selection_token_free(backends_mock):
    sel = _selector()
    vendor, sig, log = sel.select_vendor(candidates=["mock"], policy="any")
    assert vendor == "mock"


def test_ranking_by_queue(backends_mock):
    sel = _selector()
    vendor, sig, log = sel.select_vendor(
        candidates=["mock_busy", "mock"], policy="queue"
    )
    assert vendor == "mock"  # queue_depth 0 beats 9


def test_discover_registry_vendors_parses_jgf(backends_real):
    sel = _selector()

    class FakeRPC:
        def __init__(self, payload):
            self._p = payload

        def get(self):
            return self._p

    class FakeHandle:
        def rpc(self, topic, payload):
            graph = {
                "graph": {
                    "nodes": [
                        {"metadata": {"type": "qdevice_ibm"}},
                        {"metadata": {"type": "qdevice_braket"}},
                        {"metadata": {"type": "core"}},
                    ]
                }
            }
            return FakeRPC({"R": graph})

    vendors = sel.discover_registry_vendors(FakeHandle())
    assert vendors == {"ibm", "braket"}


def test_discover_registry_vendors_survives_error(backends_real):
    sel = _selector()

    class BadHandle:
        def rpc(self, *a, **k):
            raise RuntimeError("no fluxion")

    assert sel.discover_registry_vendors(BadHandle()) == set()
