"""Unit tests for the read-only graph helpers (fake flux handle)."""

import json
from flux_quantum import graph as qgraph


class _FakeRPC:
    def __init__(self, result):
        self._result = result

    def get(self):
        return self._result


class _FakeHandle:
    """Returns a canned graph for find. There is no write path any more:
    the qdevices arrive in the static scheduling graph the broker loads."""

    def __init__(self, graph):
        self._graph = graph

    def rpc(self, topic, payload):
        if topic == "sched-fluxion-resource.find":
            return _FakeRPC({"R": json.dumps({"graph": self._graph})})
        raise AssertionError("unexpected rpc: " + topic)


def _base_graph():
    return {
        "nodes": [
            {
                "id": "0",
                "metadata": {
                    "type": "cluster",
                    "rank": -1,
                    "paths": {"containment": "/c0"},
                },
            }
        ],
        "edges": [],
    }


def _qdevice(vid, vendor):
    return {
        "id": str(vid),
        "metadata": {
            "type": "qdevice_" + vendor,
            "rank": -1,
            "paths": {"containment": "/c0/qdevice_{}0".format(vendor)},
        },
    }


def test_get_live_graph_parses_find():
    h = _FakeHandle(_base_graph())
    g = qgraph.get_live_graph(h)
    assert g["nodes"][0]["metadata"]["type"] == "cluster"


def test_vendors_present_reads_qdevice_types():
    g = _base_graph()
    g["nodes"].append(_qdevice(1, "ibm"))
    g["edges"].append(
        {"source": "0", "target": "1", "metadata": {"subsystem": "containment"}}
    )
    assert qgraph.vendors_present(g) == {"ibm"}
    assert qgraph.vendors_present(_base_graph()) == set()


def test_orphaned_vendors_detects_an_unreachable_qdevice():
    """A qdevice vertex with no containment edge from the root: find reports
    it, the matcher cannot reach it, and every scout for it is refused as
    unsatisfiable. The probe must name it."""
    g = _base_graph()
    g["nodes"].append(_qdevice(9, "braket"))  # deliberately no edge
    assert qgraph.orphaned_vendors(g) == {"braket"}


def test_orphaned_vendors_empty_on_a_sound_graph():
    g = _base_graph()
    g["nodes"].append(_qdevice(1, "ibm"))
    g["edges"].append(
        {"source": "0", "target": "1", "metadata": {"subsystem": "containment"}}
    )
    assert qgraph.orphaned_vendors(g) == set()


def test_orphaned_vendors_counts_bare_edges_as_containment():
    """fluxion's find writer emits bare {source, target} edges with no
    metadata; those are containment and must not look like orphans."""
    g = _base_graph()
    g["nodes"].append(_qdevice(1, "ibm"))
    g["edges"].append({"source": "0", "target": "1"})
    assert qgraph.orphaned_vendors(g) == set()
