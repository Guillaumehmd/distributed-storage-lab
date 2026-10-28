#!/usr/bin/env python3
"""Re-read every object listed in the manifest and compare size and SHA-256.

Each object ends up in exactly one class:
  intact      size and hash match
  corrupted   object readable but size or hash differs
  missing     the gateway answers that the key does not exist
  error       no usable answer (timeout, 5xx, connection refused...)

Exit code 0 only if every object is intact.

Examples:
  python scripts/verify.py
  python scripts/verify.py --json results/raw/s3-verify.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from s3lab import DEFAULT_BUCKET, DEFAULT_ENDPOINT, DEFAULT_MANIFEST, error_label, make_client, read_manifest

MISSING_CODES = {"NoSuchKey", "404", "NotFound"}


def check(client, bucket: str, entry: dict) -> tuple[str, str]:
    try:
        resp = client.get_object(Bucket=bucket, Key=entry["key"])
        digest, size = hashlib.sha256(), 0
        for chunk in resp["Body"].iter_chunks(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    except Exception as exc:
        label = error_label(exc)
        return ("missing" if label.split(" ")[0] in MISSING_CODES else "error"), label
    if size != entry["size"]:
        return "corrupted", f"size {size} != {entry['size']}"
    if digest.hexdigest() != entry["sha256"]:
        return "corrupted", "sha256 mismatch"
    return "intact", ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--json", type=Path, help="also write the full report to this file")
    args = parser.parse_args()

    entries = read_manifest(args.manifest)
    if not entries:
        print(f"verify: manifest {args.manifest} is empty or missing, run `make load` first", file=sys.stderr)
        return 2

    client = make_client(
        args.endpoint,
        pool=args.workers,
        connect_timeout=min(args.timeout, 5),
        read_timeout=args.timeout,
        attempts=args.retries + 1,
    )
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda e: (e, *check(client, args.bucket, e)), entries.values()))
    elapsed = time.monotonic() - start

    counts = {k: 0 for k in ("intact", "corrupted", "missing", "error")}
    problems = []
    for entry, status, detail in results:
        counts[status] += 1
        if status != "intact":
            problems.append({"key": entry["key"], "status": status, "detail": detail})

    total = len(entries)
    print(
        f"verify: {counts['intact']}/{total} intact "
        f"({counts['corrupted']} corrupted, {counts['missing']} missing, {counts['error']} errors) "
        f"in {elapsed:.1f} s"
    )
    for p in problems[:20]:
        print(f"  {p['status']:<9} {p['key']}  {p['detail']}")
    if len(problems) > 20:
        print(f"  ... {len(problems) - 20} more (use --json for the full list)")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"total": total, **counts, "seconds": round(elapsed, 2), "problems": problems}, indent=2)
        )
    return 0 if counts["intact"] == total else 1


if __name__ == "__main__":
    sys.exit(main())
