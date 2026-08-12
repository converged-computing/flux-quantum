"""The scout jobspec must request classical AND quantum, so fluxion co-allocates
a core and the vendor's qpu. Because a qpu is a root-level device (a sibling of
node), the two are requested as TWO top-level resources -- a node-level slot and
a separate qdevice_<v> -> qpu -- the proven issue1284 co-allocation pattern.
"""

import json
import importlib


def _find(resources, rtype):
    for r in resources:
        if r["type"] == rtype:
            return r
    return None


def _cores_under_node(node):
    slot = _find(node["with"], "slot")
    core = _find(slot["with"], "core")
    return core["count"]


def test_scout_jobspec_requests_core_and_qpu():
    launch = importlib.import_module("flux_quantum.launch")
    js = launch.build_scout_jobspec("ibm", 123456789, ncores=1)

    res = js["resources"]
    # classical: a node with a labeled slot holding a core
    node = _find(res, "node")
    assert node is not None, res
    assert _cores_under_node(node) == 1
    slot = _find(node["with"], "slot")
    assert slot["label"] == "scout"

    # quantum: a SEPARATE top-level qdevice_<v> -> qpu (root-level device)
    qv = _find(res, "qdevice_ibm")
    assert qv is not None, res
    assert qv["with"][0]["type"] == "qpu"

    # the task runs scout.py, on the scout slot, against the held (main) job
    cmd = " ".join(js["tasks"][0]["command"])
    assert "scout.py" in cmd and "123456789" in cmd
    assert js["tasks"][0]["slot"] == "scout"

    json.dumps(js)  # submit path encodes it


def test_scout_vendor_scopes_the_type():
    launch = importlib.import_module("flux_quantum.launch")
    for vendor in ("ibm", "braket", "mock"):
        js = launch.build_scout_jobspec(vendor, 1, ncores=2)
        qv = _find(js["resources"], "qdevice_" + vendor)
        assert qv is not None and qv["with"][0]["type"] == "qpu"
        node = _find(js["resources"], "node")
        assert _cores_under_node(node) == 2


def test_build_does_not_import_flux():
    launch = importlib.import_module("flux_quantum.launch")
    js = launch.build_scout_jobspec("mock", 42)
    assert js["version"] == 1
