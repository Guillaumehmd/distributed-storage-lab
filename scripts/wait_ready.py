#!/usr/bin/env python3
"""Block until the cluster can serve traffic (standard library only).

Ready means: the master is leader, at least --volumes volume servers are
registered across at least --racks racks, the filer answers, and the S3
gateway answers (an anonymous 403 counts as alive).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def http_status(url: str, timeout: float = 2) -> int | None:
    """HTTP status code, or None if nothing answers."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def get_json(url: str, timeout: float = 2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def topology(master: str) -> dict[str, list[str]]:
    """{rack: [volume server urls]} as seen by the master."""
    status = get_json(f"{master}/dir/status") or {}
    racks: dict[str, list[str]] = {}
    for dc in (status.get("Topology") or {}).get("DataCenters") or []:
        for rack in dc.get("Racks") or []:
            nodes = [n["Url"] for n in rack.get("DataNodes") or []]
            if nodes:
                racks[rack["Id"]] = sorted(nodes)
    return racks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", default="http://127.0.0.1:9333")
    parser.add_argument("--filer", default="http://127.0.0.1:8888")
    parser.add_argument("--s3", default="http://127.0.0.1:8333")
    parser.add_argument("--volumes", type=int, default=4)
    parser.add_argument("--racks", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    start = time.monotonic()
    pending = ""
    while time.monotonic() - start < args.timeout:
        leader = (get_json(f"{args.master}/cluster/status") or {}).get("IsLeader")
        racks = topology(args.master) if leader else {}
        n_nodes = sum(len(nodes) for nodes in racks.values())
        checks = {
            "master leader": bool(leader),
            f"{args.volumes} volume servers on {args.racks} racks": n_nodes >= args.volumes
            and len(racks) >= args.racks,
            "filer": http_status(args.filer) in (200, 404),
            "s3 gateway": http_status(args.s3) is not None,
        }
        missing = [name for name, ok in checks.items() if not ok]
        if not missing:
            layout = "; ".join(f"{rack}: {', '.join(nodes)}" for rack, nodes in sorted(racks.items()))
            print(f"cluster ready in {time.monotonic() - start:.1f} s -> {n_nodes} volume servers ({layout})")
            return 0
        now = ", ".join(missing)
        if now != pending:
            print(f"  waiting for: {now}", flush=True)
            pending = now
        time.sleep(1)

    print(f"cluster NOT ready after {args.timeout:.0f} s, still missing: {pending}", file=sys.stderr)
    print("look at `make ps` and `make logs S=<service>`", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
