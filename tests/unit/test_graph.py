"""Unit tests for the live-graph populator (fake flux handle)."""

import json
from flux_quantum import graph as qgraph


class _FakeRPC:
    def __init__(self, result):
        self._result = result

    def get(self):
        return self._result


class _FakeHandle:
    """Records add_subgraph payloads; returns a canned graph for find."""

    def __init__(self, graph):
        self._graph = graph
        self.added = []

    def rpc(self, topic, payload):
        if topic == "sched-fluxion-resource.find":
            return _FakeRPC({"R": json.dumps({"graph": self._graph})})
        if topic == "sched-fluxion-resource.add_subgraph":
            self.added.append(json.loads(payload["subgraph"]))
            return _FakeRPC({})
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


def test_get_live_graph_parses_find():
    h = _FakeHandle(_base_graph())
    g = qgraph.get_live_graph(h)
    assert g["nodes"][0]["metadata"]["type"] == "cluster"


def test_populate_adds_missing_vendor():
    h = _FakeHandle(_base_graph())
    added = qgraph.populate(h, ["ibm"])
    assert added == {"ibm"}
    # one add_subgraph call carrying a qdevice_ibm vertex
    types = [n["metadata"]["type"] for n in h.added[0]["graph"]["nodes"]]
    assert "qdevice_ibm" in types and "qpu" in types


def test_populate_is_idempotent():
    g = _base_graph()
    g["nodes"].append(
        {
            "id": "1",
            "metadata": {
                "type": "qdevice_ibm",
                "rank": -1,
                "paths": {"containment": "/c0/qdevice_ibm0"},
            },
        }
    )
    g["edges"].append(
        {"source": "0", "target": "1", "metadata": {"subsystem": "containment"}}
    )
    h = _FakeHandle(g)
    added = qgraph.populate(h, ["ibm"])
    assert added == set()  # already present
    assert h.added == []  # no add_subgraph call
