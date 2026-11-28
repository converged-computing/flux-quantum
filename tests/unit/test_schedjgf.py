"""Unit tests for the static scheduling-graph generator."""

import json

import pytest

from flux_quantum import qresource as qr
from flux_quantum import schedjgf


def _r(nodelist, rank="0-1", cores="0-3"):
    return {
        "version": 1,
        "execution": {
            "R_lite": [{"rank": rank, "children": {"core": cores}}],
            "nodelist": nodelist,
        },
    }


def test_expand_idset():
    assert schedjgf.expand_idset("0-3,7") == [0, 1, 2, 3, 7]
    assert schedjgf.expand_idset("5") == [5]


def test_expand_hostlist_forms():
    assert schedjgf.expand_hostlist("a,b") == ["a", "b"]
    assert schedjgf.expand_hostlist("queue-blue-[1-2]") == [
        "queue-blue-1",
        "queue-blue-2",
    ]
    # flux collapses a repeated host into a bracketed repeat
    assert schedjgf.expand_hostlist("74838e63e[870,870]") == [
        "74838e63e870",
        "74838e63e870",
    ]
    assert schedjgf.expand_hostlist("n[01-03]") == ["n01", "n02", "n03"]


def test_expand_r_pairs_ranks_with_hosts():
    out = schedjgf.expand_r(_r(["queue-blue-[1-2]"]))
    assert out == [(0, "queue-blue-1", [0, 1, 2, 3]), (1, "queue-blue-2", [0, 1, 2, 3])]


def test_expand_r_refuses_a_host_rank_mismatch():
    with pytest.raises(ValueError):
        schedjgf.expand_r(_r(["onlyonehost"]))


def test_generate_mirrors_fluxions_own_layout():
    jgf = schedjgf.generate(_r(["queue-blue-[1-2]"]), ["braket"], qpus=4)
    g = jgf["graph"]
    bytype = {}
    for n in g["nodes"]:
        bytype.setdefault(n["metadata"]["type"], []).append(n)
    assert len(bytype["cluster"]) == 1
    assert len(bytype["node"]) == 2
    assert len(bytype["core"]) == 8
    assert len(bytype["qdevice_braket"]) == 1
    assert len(bytype["qpu"]) == 4
    # fluxion's own layout: /cluster0/<hostname>/core<i>, ranks from R
    node0 = bytype["node"][0]
    assert node0["metadata"]["paths"]["containment"] == "/cluster0/queue-blue-1"
    assert node0["metadata"]["rank"] == 0
    core = bytype["core"][0]
    assert core["metadata"]["paths"]["containment"].startswith(
        "/cluster0/queue-blue-1/core"
    )
    assert core["metadata"]["rank"] == 0
    # qdevices hang off the root, rank -1, as a sibling of the nodes
    qd = bytype["qdevice_braket"][0]
    assert qd["metadata"]["paths"]["containment"] == "/cluster0/qdevice_braket0"
    assert qd["metadata"]["rank"] == -1


def test_generate_vertices_carry_the_full_reader_field_set():
    """The shape power.json uses, which fluxion's JGF reader is tested on."""
    jgf = schedjgf.generate(_r(["a1,b2"]), ["mock"])
    required = {
        "type",
        "basename",
        "name",
        "id",
        "uniq_id",
        "rank",
        "exclusive",
        "unit",
        "size",
        "paths",
    }
    for n in jgf["graph"]["nodes"]:
        assert required <= set(n["metadata"]), n
        assert n["id"] == str(n["metadata"]["uniq_id"])
    for e in jgf["graph"]["edges"]:
        assert e["metadata"]["subsystem"] == "containment"


def test_generate_agrees_with_the_jobspec_shape():
    """The graph and the scout jobspec must use the same type strings, or
    every scout is unsatisfiable."""
    jgf = schedjgf.generate(_r(["a1,b2"]), ["ibm"])
    jr = qr.jobspec_resource("ibm")
    types = {n["metadata"]["type"] for n in jgf["graph"]["nodes"]}
    assert jr["type"] in types
    assert jr["with"][0]["type"] in types


def test_generate_is_deterministic():
    a = schedjgf.generate(_r(["a1,b2"]), ["ibm", "braket"], qpus=2)
    b = schedjgf.generate(_r(["a1,b2"]), ["ibm", "braket"], qpus=2)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_check_passes_generated_graphs_and_catches_orphans():
    jgf = schedjgf.generate(_r(["a1,b2"]), ["ibm"])
    schedjgf.check(jgf, ["ibm"])
    with pytest.raises(ValueError):
        schedjgf.check(jgf, ["braket"])  # vendor missing
    broken = schedjgf.generate(_r(["a1,b2"]), ["ibm"])
    broken["graph"]["edges"].pop()  # orphan the last qpu
    with pytest.raises(ValueError):
        schedjgf.check(broken, ["ibm"])


def test_cli_writes_a_checked_file(tmp_path):
    rfile = tmp_path / "R"
    rfile.write_text(json.dumps(_r(["queue-blue-[1-2]"])))
    out = tmp_path / "scheduling.json"
    rc = schedjgf.main(
        ["braket", "mock", "--qpus", "2", "--from-r", str(rfile), "--output", str(out)]
    )
    assert rc == 0
    jgf = json.loads(out.read_text())
    types = {n["metadata"]["type"] for n in jgf["graph"]["nodes"]}
    assert {"qdevice_braket", "qdevice_mock", "qpu", "node", "core"} <= types
