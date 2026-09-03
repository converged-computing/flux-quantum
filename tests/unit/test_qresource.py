"""Unit tests for the single quantum-resource shape source."""

from flux_quantum import qresource as qr


def test_qdevice_type():
    assert qr.qdevice_type("ibm") == "qdevice_ibm"
    assert qr.qdevice_type("mock") == "qdevice_mock"


def test_jobspec_resource_requests_exclusive_qpu():
    r = qr.jobspec_resource("ibm")
    assert r["type"] == "qdevice_ibm"
    assert r["count"] == 1
    qpu = r["with"][0]
    assert qpu["type"] == "qpu"
    # exclusive is required or the leaf device is dropped from R
    assert qpu["exclusive"] is True


def test_jobspec_resource_nqpus():
    r = qr.jobspec_resource("ibm", nqpus=3)
    assert r["with"][0]["count"] == 3


def _live(root_path="/cluster0"):
    return {
        "nodes": [
            {
                "id": "0",
                "metadata": {
                    "type": "cluster",
                    "rank": -1,
                    "paths": {"containment": root_path},
                },
            },
            {
                "id": "1",
                "metadata": {
                    "type": "rack",
                    "rank": -1,
                    "paths": {"containment": root_path + "/rack0"},
                },
            },
        ],
        "edges": [
            {"source": "0", "target": "1", "metadata": {"subsystem": "containment"}}
        ],
    }


def test_graph_subgraph_attaches_qdevice_at_root():
    sg = qr.graph_subgraph(_live(), ["ibm"])
    g = sg["graph"]
    types = [n["metadata"]["type"] for n in g["nodes"]]
    assert "qdevice_ibm" in types and types.count("qpu") == 1
    # the root is copied verbatim so fluxion matches (path, rank) and attaches
    assert any(
        n["id"] == "0" and n["metadata"]["type"] == "cluster" for n in g["nodes"]
    )
    # qdevice is a child of the root (id 0) -> a sibling of rack
    qd = next(n for n in g["nodes"] if n["metadata"]["type"] == "qdevice_ibm")
    assert any(e["source"] == "0" and e["target"] == qd["id"] for e in g["edges"])


def test_graph_subgraph_multi_vendor_unique_ids():
    sg = qr.graph_subgraph(_live(), ["ibm", "mock"], qpus=2)
    ids = [n["id"] for n in sg["graph"]["nodes"]]
    assert len(ids) == len(set(ids))  # no id collisions
    types = [n["metadata"]["type"] for n in sg["graph"]["nodes"]]
    assert types.count("qpu") == 4  # 2 vendors x 2 qpus


def test_graph_subgraph_and_jobspec_agree_on_type():
    """Graph and jobspec have to use the same type string."""
    sg = qr.graph_subgraph(_live(), ["ibm"])
    jr = qr.jobspec_resource("ibm")
    graph_types = {n["metadata"]["type"] for n in sg["graph"]["nodes"]}
    assert jr["type"] in graph_types  # qdevice_ibm present in both


def _live_with_sockets():
    # cluster -> node -> socket -> core (hwloc-style)
    return {
        "nodes": [
            {
                "id": "0",
                "metadata": {"type": "cluster", "paths": {"containment": "/c0"}},
            },
            {
                "id": "1",
                "metadata": {"type": "node", "paths": {"containment": "/c0/n0"}},
            },
            {
                "id": "2",
                "metadata": {"type": "socket", "paths": {"containment": "/c0/n0/s0"}},
            },
            {
                "id": "3",
                "metadata": {"type": "core", "paths": {"containment": "/c0/n0/s0/c0"}},
            },
        ],
        "edges": [
            {"source": "0", "target": "1", "metadata": {"subsystem": "containment"}},
            {"source": "1", "target": "2", "metadata": {"subsystem": "containment"}},
            {"source": "2", "target": "3", "metadata": {"subsystem": "containment"}},
        ],
    }


def _live_no_sockets():
    # cluster -> rack -> node -> core (issue1284-style)
    return {
        "nodes": [
            {
                "id": "0",
                "metadata": {"type": "cluster", "paths": {"containment": "/c0"}},
            },
            {
                "id": "1",
                "metadata": {"type": "rack", "paths": {"containment": "/c0/r0"}},
            },
            {
                "id": "2",
                "metadata": {"type": "node", "paths": {"containment": "/c0/r0/n0"}},
            },
            {
                "id": "3",
                "metadata": {"type": "core", "paths": {"containment": "/c0/r0/n0/c0"}},
            },
        ],
        "edges": [
            {"source": "0", "target": "1", "metadata": {"subsystem": "containment"}},
            {"source": "1", "target": "2", "metadata": {"subsystem": "containment"}},
            {"source": "2", "target": "3", "metadata": {"subsystem": "containment"}},
        ],
    }


def test_classical_derivation_socketed_vs_socketless():
    assert qr._node_to_core_intermediates(_live_with_sockets()) == ["socket"]
    assert qr._node_to_core_intermediates(_live_no_sockets()) == []


def test_classical_resource_mirrors_hierarchy():
    # socketed -> node -> socket -> slot -> core
    r = qr.classical_resource(_live_with_sockets())
    assert r["type"] == "node"
    socket = r["with"][0]
    assert socket["type"] == "socket"
    slot = socket["with"][0]
    assert slot["type"] == "slot" and slot["with"][0]["type"] == "core"
    # socketless -> node -> slot -> core
    r2 = qr.classical_resource(_live_no_sockets())
    assert r2["with"][0]["type"] == "slot"


def test_classical_resource_fallback_no_core():
    # a graph with no core -> falls back to node -> slot -> core
    empty = {
        "nodes": [
            {"id": "0", "metadata": {"type": "cluster", "paths": {"containment": "/c"}}}
        ],
        "edges": [],
    }
    r = qr.classical_resource(empty)
    assert r["type"] == "node" and r["with"][0]["type"] == "slot"


def test_count_cores_walks_the_resource_tree():
    """The jobtap plugin budgets on this number, so it has to be right for the
    shapes the plugin will actually see."""
    from flux_quantum.qresource import count_cores

    node_slot_core = {
        "resources": [
            {
                "type": "node",
                "count": 1,
                "with": [
                    {"type": "slot", "count": 8, "with": [{"type": "core", "count": 1}]}
                ],
            }
        ]
    }
    assert count_cores(node_slot_core) == 8

    # counts multiply down the tree
    socketed = {
        "resources": [
            {
                "type": "node",
                "count": 2,
                "with": [
                    {
                        "type": "socket",
                        "count": 2,
                        "with": [
                            {
                                "type": "slot",
                                "count": 4,
                                "with": [{"type": "core", "count": 1}],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    assert count_cores(socketed) == 16

    # the qpu is not a core, so a scout jobspec counts as one
    scout = {
        "resources": [
            {"type": "slot", "count": 1, "with": [{"type": "core", "count": 1}]},
            {
                "type": "qdevice_ibm",
                "count": 1,
                "with": [{"type": "qpu", "count": 1, "exclusive": True}],
            },
        ]
    }
    assert count_cores(scout) == 1

    assert count_cores({"resources": []}) == 0
    assert count_cores({}) == 0


def test_count_cores_takes_the_minimum_of_a_range():
    """A range means the job may get more, but only the minimum is promised."""
    from flux_quantum.qresource import count_cores

    ranged = {
        "resources": [
            {
                "type": "slot",
                "count": {"min": 2, "max": 8},
                "with": [{"type": "core", "count": 1}],
            }
        ]
    }
    assert count_cores(ranged) == 2
