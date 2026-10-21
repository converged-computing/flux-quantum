##############################################################
# Copyright 2024 Lawrence Livermore National Security, LLC
# (c.f. AUTHORS, NOTICE.LLNS, COPYING)
#
# SPDX-License-Identifier: LGPL-3.0
##############################################################

"""Generate the complete static scheduling graph, once, before the broker.

    flux python -m flux_quantum.schedjgf ibm braket mock ionq --qpus 4 \
        --from-r /etc/flux/system/R > /etc/flux/system/scheduling.json

and in the broker's config:

    [resource]
    path = "/etc/flux/system/R"
    scheduling = "/etc/flux/system/scheduling.json"

Fluxion then receives the whole graph - the real nodes and cores plus the
qdevice vertices - through resource.acquire at startup, and the graph is
never mutated afterwards. There is no populate step, so both corruption
modes that mutation had (two populates racing one graph, and growing the
graph while jobs hold allocations, which permanently corrupts planner
spans) are unreachable rather than gated. The feasibility module also
bootstraps itself: notify carries the initial graph, qdevices included.

Layout notes, verified against the fluxion source and its own graphs:

- The vertex layout mirrors what fluxion builds from R by itself:
  /cluster0/<hostname>/core<i>, no socket level. Vertex metadata carries
  the full field set the JGF reader unpacks (the same shape as fluxion's
  in-tree t/data/resource/jgfs/power.json, which runs real jobs in
  t1028-rv1-partial-release-across-racks.t).
- Ranks must match the broker's R verbatim. remap_jgf_namespace only
  remaps for a subinstance whose parent R carries JGF; in a system
  instance the ranks we write are the ranks fluxion uses.
- The qdevice subtree is qdevice_<vendor> -> qpu, hung off the cluster
  root as a sibling of the nodes, exactly the shape jobspec_resource in
  qresource.py requests. The qpu count is the vendor's concurrency limit.

The generator is deterministic: the same R and vendor list produce the
same file, so every node of a cluster can generate its own copy.
"""

import argparse
import json
import re
import sys

from flux_quantum import qresource

try:  # the flux bindings expand any hostlist/idset form; fall back when absent
    from flux.hostlist import Hostlist as _Hostlist
    from flux.idset import IDset as _IDset
except Exception:  # pragma: no cover - exercised where flux is installed
    _Hostlist = None
    _IDset = None


def expand_idset(ids):
    """Expand an RFC 22 idset string like '0-3,7' into a list of ints."""
    if _IDset is not None:
        return [int(i) for i in _IDset(ids)]
    out = []
    for part in str(ids).split(","):
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def expand_hostlist(hosts):
    """Expand an RFC 29 hostlist string like 'queue-[1-2],login0'."""
    if _Hostlist is not None:
        return list(_Hostlist(hosts))
    out = []
    # split on commas outside brackets
    part, depth = "", 0
    parts = []
    for ch in str(hosts):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += ch
    if part:
        parts.append(part)
    for p in parts:
        m = re.match(r"^(.*?)\[([^\]]+)\](.*)$", p)
        if not m:
            out.append(p)
            continue
        prefix, body, suffix = m.groups()
        for item in body.split(","):
            if "-" in item:
                lo, hi = item.split("-", 1)
                width = len(lo) if lo.startswith("0") and len(lo) > 1 else 0
                for i in range(int(lo), int(hi) + 1):
                    out.append("{}{}{}".format(prefix, str(i).zfill(width), suffix))
            else:
                out.append("{}{}{}".format(prefix, item, suffix))
    return out


def expand_r(rv1):
    """(rank, hostname, [core ids]) per rank, in rank order, from an RV1 R.

    nodelist[i] names the host of the i-th rank of the union of R_lite
    ranks in ascending order, which is flux's own convention.
    """
    execution = rv1["execution"]
    rank_cores = {}
    for entry in execution["R_lite"]:
        cores = expand_idset(entry["children"]["core"])
        for rank in expand_idset(entry["rank"]):
            rank_cores[rank] = cores
    hosts = []
    for chunk in execution["nodelist"]:
        hosts.extend(expand_hostlist(chunk))
    ranks = sorted(rank_cores)
    if len(hosts) != len(ranks):
        raise ValueError(
            "R is inconsistent: {} hosts for {} ranks".format(len(hosts), len(ranks))
        )
    return [(rank, hosts[i], rank_cores[rank]) for i, rank in enumerate(ranks)]


def _split_host(host, fallback_id):
    """hostname -> (basename, id), the way fluxion's rv1exec reader does."""
    m = re.match(r"^(.*?)(\d+)$", host)
    if m:
        return m.group(1), int(m.group(2))
    return host, fallback_id


def _vertex(uniq_id, vtype, basename, name, iid, rank, path, properties=None):
    meta = {
        "type": vtype,
        "basename": basename,
        "name": name,
        "id": iid,
        "uniq_id": uniq_id,
        "rank": rank,
        "exclusive": False,
        "unit": "",
        "size": 1,
        "paths": {"containment": path},
    }
    if properties is not None:
        meta["properties"] = properties
    return {"id": str(uniq_id), "metadata": meta}


def _edge(src, tgt):
    return {
        "source": str(src),
        "target": str(tgt),
        "metadata": {"subsystem": "containment"},
    }


def generate(rv1, vendors, qpus=1):
    """The full scheduling JGF for an R, a vendor list and a qpu count."""
    if isinstance(vendors, str):
        vendors = [vendors]
    nodes, edges = [], []
    uid = 0

    root_path = "/cluster0"
    nodes.append(_vertex(uid, "cluster", "cluster", "cluster0", 0, -1, root_path))
    root_id, uid = uid, uid + 1

    for rank, host, cores in expand_r(rv1):
        basename, iid = _split_host(host, rank)
        node_path = "{}/{}".format(root_path, host)
        nodes.append(_vertex(uid, "node", basename, host, iid, rank, node_path))
        node_id, uid = uid, uid + 1
        edges.append(_edge(root_id, node_id))
        for core in cores:
            core_path = "{}/core{}".format(node_path, core)
            nodes.append(
                _vertex(
                    uid, "core", "core", "core{}".format(core), core, rank, core_path
                )
            )
            edges.append(_edge(node_id, uid))
            uid += 1

    for vendor in vendors:
        qd_type = qresource.qdevice_type(vendor)
        qd_path = "{}/{}0".format(root_path, qd_type)
        nodes.append(
            _vertex(uid, qd_type, qd_type, qd_type + "0", 0, -1, qd_path, {vendor: ""})
        )
        qd_id, uid = uid, uid + 1
        edges.append(_edge(root_id, qd_id))
        for q in range(qpus):
            qpu_path = "{}/{}{}".format(qd_path, qresource.QPU, q)
            nodes.append(
                _vertex(
                    uid,
                    qresource.QPU,
                    qresource.QPU,
                    "{}{}".format(qresource.QPU, q),
                    q,
                    -1,
                    qpu_path,
                    {vendor: ""},
                )
            )
            edges.append(_edge(qd_id, uid))
            uid += 1

    return {"graph": {"nodes": nodes, "edges": edges}}


def check(jgf, vendors=()):
    """Raise ValueError unless every vertex is reachable from a single root
    and every vendor's qdevice is present. The same invariant the old
    populate verified after mutating; here it runs before anything ships."""
    graph = jgf["graph"]
    root = qresource.find_root(graph)
    children = {}
    for edge in graph["edges"]:
        children.setdefault(edge["source"], []).append(edge["target"])
    seen, stack = set(), [root["id"]]
    while stack:
        vid = stack.pop()
        if vid in seen:
            continue
        seen.add(vid)
        stack.extend(children.get(vid, []))
    unreachable = [n["id"] for n in graph["nodes"] if n["id"] not in seen]
    if unreachable:
        raise ValueError("unreachable vertices: {}".format(unreachable))
    types = {n["metadata"]["type"] for n in graph["nodes"]}
    missing = [v for v in vendors if qresource.qdevice_type(v) not in types]
    if missing:
        raise ValueError("missing qdevices: {}".format(missing))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="flux-quantum-schedjgf",
        description="Generate the static scheduling graph for [resource] scheduling.",
    )
    parser.add_argument("vendors", nargs="+", help="vendor names, e.g. braket mock")
    parser.add_argument(
        "--qpus", type=int, default=1, help="qpus per vendor device (concurrency limit)"
    )
    parser.add_argument(
        "--from-r",
        default="/etc/flux/system/R",
        help="path to the broker's R, or - for stdin (default %(default)s)",
    )
    parser.add_argument(
        "--output", default="-", help="output path, or - for stdout (default)"
    )
    args = parser.parse_args(argv)

    if args.from_r == "-":
        rv1 = json.load(sys.stdin)
    else:
        with open(args.from_r) as fh:
            rv1 = json.load(fh)

    jgf = generate(rv1, args.vendors, qpus=args.qpus)
    check(jgf, args.vendors)
    text = json.dumps(jgf, indent=1)
    if args.output == "-":
        print(text)
    else:
        with open(args.output, "w") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
