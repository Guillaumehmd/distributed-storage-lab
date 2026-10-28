#!/usr/bin/env python3
"""Check where every replicated volume lives, as seen by the master (standard library only).

For each volume, the expected number of copies comes from its replica
placement xyz (x: other data centers, y: other racks, z: same rack), so
010 -> 2 copies on 2 different racks. A volume is reported as:

  ok                 expected number of copies, rack constraint met
  under-replicated   fewer copies than expected
  misplaced          enough copies but the rack/data-center spread is wrong
  over-replicated    more copies than expected

Erasure-coded volumes (ec.encode) are not covered: they leave this list.

Examples:
  python3 scripts/placement.py             # one report, exit 1 if anything is not ok
  python3 scripts/placement.py --watch 5   # one line every 5 s, to time a re-replication
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone


def fetch(master: str) -> dict:
    with urllib.request.urlopen(f"{master}/vol/status", timeout=5) as resp:
        return json.load(resp)


def analyze(vol_status: dict) -> dict:
    """Group volume replicas and classify each volume. Pure function, easy to test."""
    replicas: dict[tuple[str, int], list[tuple[str, str, str]]] = defaultdict(list)
    placement: dict[tuple[str, int], dict] = {}
    nodes_by_rack: dict[str, set[str]] = defaultdict(set)
    datacenters = (vol_status.get("Volumes") or {}).get("DataCenters") or {}
    for dc, racks in datacenters.items():
        for rack, nodes in (racks or {}).items():
            for node, volumes in (nodes or {}).items():
                nodes_by_rack[rack].add(node)
                for vol in volumes or []:
                    vid = (vol.get("Collection", ""), int(vol["Id"]))
                    replicas[vid].append((dc, rack, node))
                    placement[vid] = vol.get("ReplicaPlacement") or {}

    report = {"ok": [], "under-replicated": [], "misplaced": [], "over-replicated": []}
    for vid, copies in sorted(replicas.items()):
        rp = placement[vid]
        same_rack, other_rack, other_dc = rp.get("node", 0), rp.get("rack", 0), rp.get("dc", 0)
        expected = 1 + same_rack + other_rack + other_dc
        n_dcs = len({c[0] for c in copies})
        n_racks = len({(c[0], c[1]) for c in copies})
        code = f"{other_dc}{other_rack}{same_rack}"
        where = ", ".join(f"{rack}/{node}" for _, rack, node in sorted(copies))
        line = {
            "collection": vid[0],
            "id": vid[1],
            "placement": code,
            "copies": len(copies),
            "expected": expected,
            "where": where,
        }
        if len(copies) < expected:
            report["under-replicated"].append(line)
        elif len(copies) > expected:
            report["over-replicated"].append(line)
        elif n_dcs < 1 + other_dc or n_racks < 1 + other_rack + other_dc:
            report["misplaced"].append(line)
        else:
            report["ok"].append(line)
    report["racks"] = {rack: sorted(nodes) for rack, nodes in sorted(nodes_by_rack.items())}
    return report


UNREACHABLE = (urllib.error.URLError, ConnectionError, TimeoutError)


def one_report(master: str) -> int:
    try:
        report = analyze(fetch(master))
    except UNREACHABLE as exc:
        print(f"master unreachable at {master}: {exc}", file=sys.stderr)
        return 2
    for rack, nodes in report["racks"].items():
        print(f"{rack}: {', '.join(nodes)}")
    total = sum(len(report[k]) for k in ("ok", "under-replicated", "misplaced", "over-replicated"))
    print(
        f"{total} volumes: {len(report['ok'])} ok, {len(report['under-replicated'])} under-replicated, "
        f"{len(report['misplaced'])} misplaced, {len(report['over-replicated'])} over-replicated"
    )
    for status in ("under-replicated", "misplaced", "over-replicated", "ok"):
        for v in report[status]:
            copies = f"{v['copies']}/{v['expected']} copies"
            print(f"  {status:<16} {v['collection']}:{v['id']:<4} {v['placement']}  {copies}  {v['where']}")
    if total == 0:
        print("no volume yet: they are created on the first write (make load)")
        return 1
    return 0 if len(report["ok"]) == total else 1


def watch(master: str, every: float) -> int:
    print("time (UTC)    volumes  ok  under  misplaced  over  volume servers")
    try:
        while True:
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
            try:
                r = analyze(fetch(master))
                n_nodes = sum(len(n) for n in r["racks"].values())
                total = sum(len(r[k]) for k in ("ok", "under-replicated", "misplaced", "over-replicated"))
                print(
                    f"{stamp}  {total:>9} {len(r['ok']):>3} {len(r['under-replicated']):>6} "
                    f"{len(r['misplaced']):>10} {len(r['over-replicated']):>5}  {n_nodes}",
                    flush=True,
                )
            except UNREACHABLE as exc:
                print(f"{stamp}  master unreachable ({exc.__class__.__name__})", flush=True)
            time.sleep(every)
    except KeyboardInterrupt:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--master", default="http://127.0.0.1:9333")
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="print one summary line every SECONDS")
    args = parser.parse_args()
    if args.watch:
        return watch(args.master, args.watch)
    return one_report(args.master)


if __name__ == "__main__":
    sys.exit(main())
