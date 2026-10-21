#!/usr/bin/env python3
"""Plot two qubit gate error for a Braket device.

    python3 plot_specs.py                       # pull it live
    python3 plot_specs.py --device arn:...
    python3 plot_specs.py --json specs.json     # from a saved dump

Edges reporting a fidelity of 0.5 with an error of 1.0 are a sentinel, not a
measurement. They are counted and named but left off the axis, because a coin
flip on a two qubit gate is a dead coupler and plotting it as a bar implies it
is merely bad.
"""

import argparse
import json

CEPHEUS = "arn:aws:braket:us-west-1::device/qpu/rigetti/Cepheus-1-108Q"
DEAD = 0.6  # anything at or below this is the sentinel, not a real number


def edges(specs):
    """Every two qubit characteristic in the device specs, as
    (pair, fidelity, timestamp)."""
    out = []
    for group in (
        specs.get("2Q", {}).values()
        if isinstance(specs.get("2Q"), dict)
        else specs.get("instructions", [])
    ):
        out.extend(_from_group(group))
    if not out:
        # the shape varies by provider, so walk it rather than assume
        out = list(_walk(specs))
    return out


def _from_group(group):
    ids = tuple(group.get("node_ids", ()))
    for c in group.get("characteristics", []):
        if c.get("name") == "fCZ":
            yield ids, c.get("value"), c.get("timestamp")


def _walk(node, ids=None):
    """Find fCZ entries wherever the provider put them."""
    if isinstance(node, dict):
        if "node_ids" in node and "characteristics" in node:
            yield from _from_group(node)
            return
        for v in node.values():
            yield from _walk(v, ids)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, ids)


def _local_span(first, last):
    """Two UTC timestamps in the reader's own timezone.

    Braket reports these in UTC. Nobody reading a plot wants to do the
    arithmetic, and getting it wrong is how you conclude a device was
    calibrated at a time it was shut.
    """
    import datetime

    def here(t):
        return datetime.datetime.fromisoformat(t).astimezone()

    a, b = here(first), here(last)
    zone = a.strftime("%Z") or "local"
    same_day = a.date() == b.date()
    return (
        a.strftime("%H:%M:%S"),
        b.strftime("%H:%M:%S") if same_day else b.strftime("%b %d %H:%M:%S"),
        zone,
    )


def load(args):
    if args.json:
        with open(args.json) as fh:
            return json.load(fh)
    from braket.aws import AwsDevice

    return AwsDevice(args.device).properties.provider.specs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=CEPHEUS)
    ap.add_argument("--json", help="a saved specs dump instead of a live pull")
    ap.add_argument("--out", default="gate-error.png")
    args = ap.parse_args()

    specs = load(args)
    if not isinstance(specs, dict):
        specs = json.loads(json.dumps(specs, default=lambda o: o.__dict__))

    found = [e for e in edges(specs) if e[1] is not None]
    if not found:
        raise SystemExit("no fCZ characteristics found in these specs")

    live = sorted([e for e in found if e[1] > DEAD], key=lambda e: 1 - e[1])
    dead = [e for e in found if e[1] <= DEAD]

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    errs = [1 - v for _, v, _ in live]
    names = ["%d-%d" % p for p, _, _ in live]

    # A bar per edge is unreadable past about thirty of them, and this device
    # has a hundred and sixty. Sorted against rank keeps every point, shows
    # the clusters and the tail, and does not care how many there are.
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    ax.plot(
        range(1, len(errs) + 1),
        errs,
        marker="o",
        markersize=3,
        linewidth=1.4,
        color="#1C7293",
    )
    ax.set_yscale("log")
    ax.set_ylabel("1 - fidelity")
    ax.set_xlabel("edge, best to worst")

    mid = errs[len(errs) // 2]
    ax.axhline(mid, color="#9AA5B1", linestyle=":", linewidth=1)
    ax.annotate(
        "median %.4f" % mid,
        xy=(0.02, mid),
        xycoords=("axes fraction", "data"),
        va="bottom",
        fontsize=8,
        color="#5C6672",
    )

    # name the few that matter, since those are the ones you would place on
    ax.annotate(
        names[0],
        xy=(1, errs[0]),
        textcoords="offset points",
        xytext=(8, 4),
        fontsize=8,
        color="#5C6672",
    )
    ax.annotate(
        names[-1],
        xy=(len(errs), errs[-1]),
        textcoords="offset points",
        xytext=(-8, 4),
        ha="right",
        fontsize=8,
        color="#5C6672",
    )
    ax.margins(x=0.06)

    stamps = sorted(t for _, _, t in found if t)
    title = "Two qubit gate error, %s" % args.device.split("/")[-1]
    if stamps:
        first, last, zone = _local_span(stamps[0], stamps[-1])
        title += "\ncalibrated %s to %s %s" % (first, last, zone)
    ax.set_title(title, fontsize=11)

    note = "%d live edges, error %.4f to %.4f" % (len(live), errs[0], errs[-1])
    if dead:
        note += "\n%d dead and unusable" % len(dead)
    ax.annotate(
        note,
        xy=(0.02, 0.97),
        xycoords="axes fraction",
        va="top",
        fontsize=9,
        color="#5C6672",
    )
    ax.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(args.out, dpi=150)
    print("wrote", args.out)
    print(
        "live %d, dead %d, error %.4f to %.4f"
        % (len(live), len(dead), min(errs), max(errs))
    )


if __name__ == "__main__":
    main()
