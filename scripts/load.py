#!/usr/bin/env python3
"""Write objects to the cluster through the S3 gateway.

Two modes:

  fill (default)   write --count objects of random content, sizes log-uniform
                   between --min-size and --max-size, and record each object's
                   SHA-256 in the manifest (overwritten).

  --continuous     closed-loop read/write traffic for --duration seconds while a
                   fault is injected. Every operation is logged to
                   results/raw/<label>-<UTC time>.csv; successful writes are
                   appended to the manifest so verify.py checks them afterwards.
                   Retries are off by default so that faults show up as errors.

Examples:
  python scripts/load.py --count 1000
  python scripts/load.py --continuous --duration 180 --label s1-node-kill
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import signal
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from s3lab import (
    DEFAULT_BUCKET,
    DEFAULT_ENDPOINT,
    DEFAULT_MANIFEST,
    ensure_bucket,
    error_label,
    human_bytes,
    make_client,
    parse_size,
    percentile,
    read_manifest,
)


def log_uniform_size(rng: random.Random, low: int, high: int) -> int:
    """Log-uniform: as many objects between 1 and 16 KiB as between 1 and 16 MiB."""
    if low >= high:
        return low
    return round(low * (high / low) ** rng.random())


# --------------------------------------------------------------------------- fill


def run_fill(args: argparse.Namespace) -> int:
    low, high = parse_size(args.min_size or "1KiB"), parse_size(args.max_size or "16MiB")
    rng = random.Random(args.seed)
    plan = [(f"{args.prefix}{i:06d}", log_uniform_size(rng, low, high)) for i in range(args.count)]
    total = sum(size for _, size in plan)

    client = make_client(args.endpoint, pool=args.workers, attempts=args.retries + 1)
    ensure_bucket(client, args.bucket)
    print(
        f"fill: {args.count} objects, {human_bytes(low)} to {human_bytes(high)}, "
        f"{human_bytes(total)} total, {args.workers} workers -> s3://{args.bucket}/{args.prefix}"
    )

    def put(key: str, size: int) -> dict:
        body = os.urandom(size)
        client.put_object(Bucket=args.bucket, Key=key, Body=body)
        return {"key": key, "size": size, "sha256": hashlib.sha256(body).hexdigest()}

    written: list[dict] = []
    errors: list[tuple[str, str]] = []
    done_bytes = 0
    start = time.monotonic()
    step = max(1, args.count // 10)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(put, key, size): (key, size) for key, size in plan}
        for i, future in enumerate(as_completed(futures), start=1):
            key, size = futures[future]
            try:
                written.append(future.result())
                done_bytes += size
            except Exception as exc:
                errors.append((key, error_label(exc)))
            if i % step == 0 or i == args.count:
                elapsed = time.monotonic() - start
                print(
                    f"  {i:>6}/{args.count}  {human_bytes(done_bytes)}  "
                    f"{human_bytes(done_bytes / elapsed)}/s  errors={len(errors)}",
                    flush=True,
                )

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("w") as fh:
        for entry in sorted(written, key=lambda e: e["key"]):
            fh.write(json.dumps(entry) + "\n")

    elapsed = time.monotonic() - start
    print(
        f"fill done: {len(written)}/{args.count} written, {human_bytes(done_bytes)} in {elapsed:.1f} s "
        f"({human_bytes(done_bytes / elapsed)}/s), {len(errors)} errors -> manifest {args.manifest}"
    )
    for key, label in errors[:20]:
        print(f"  error {key}: {label}")
    return 0 if not errors else 1


# --------------------------------------------------------------------- continuous


class Traffic:
    """Closed-loop workers: each one issues its next operation as soon as the previous ends."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.low = parse_size(args.min_size or "4KiB")
        self.high = parse_size(args.max_size or "1MiB")
        self.client = make_client(
            args.endpoint,
            pool=args.workers,
            connect_timeout=args.timeout,
            read_timeout=args.timeout,
            attempts=args.retries + 1,
        )
        self.read_keys = list(read_manifest(args.manifest))
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.rows: list[tuple] = []  # (t, op, ok, latency_ms) for the periodic report
        self.counter = 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.csv_path = Path("results/raw") / f"{args.label}-{stamp}.csv"
        self.t0_wall = time.time()
        self.t0 = time.monotonic()

    def next_key(self) -> str:
        with self.lock:
            self.counter += 1
            return f"live/{self.args.label}/{os.getpid()}-{self.counter:07d}"

    def one_op(self, rng: random.Random) -> tuple:
        write = not self.read_keys or rng.random() < self.args.write_ratio
        if write:
            key = self.next_key()
            size = log_uniform_size(rng, self.low, self.high)
            body = os.urandom(size)
        else:
            key = rng.choice(self.read_keys)
            size = 0
        start = time.monotonic()
        ok, err = True, ""
        try:
            if write:
                self.client.put_object(Bucket=self.args.bucket, Key=key, Body=body)
            else:
                resp = self.client.get_object(Bucket=self.args.bucket, Key=key)
                size = len(resp["Body"].read())
        except Exception as exc:
            ok, err = False, error_label(exc)
        end = time.monotonic()
        if ok and write:
            entry = {"key": key, "size": size, "sha256": hashlib.sha256(body).hexdigest()}
            with self.lock, self.args.manifest.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
        return (
            round(start - self.t0, 4),
            datetime.fromtimestamp(self.t0_wall + (start - self.t0), timezone.utc).isoformat(timespec="milliseconds"),
            "write" if write else "read",
            key,
            size,
            round((end - start) * 1000, 2),
            int(ok),
            err,
        )

    def worker(self, seed: int, writer) -> None:
        rng = random.Random(seed)
        while not self.stop.is_set():
            row = self.one_op(rng)
            with self.lock:
                writer.writerow(row)
                self.rows.append((row[0], row[2], row[6], row[5]))

    def report(self, since: float, until: float) -> str:
        with self.lock:
            window = [r for r in self.rows if since <= r[0] < until]
        parts = []
        for op in ("write", "read"):
            ops = [r for r in window if r[1] == op]
            errs = sum(1 for r in ops if not r[2])
            parts.append(f"{op} {len(ops) - errs:>4} ok {errs:>3} err")
        lat = sorted(r[3] for r in window if r[2])
        p50, p99 = percentile(lat, 50), percentile(lat, 99)
        return f"[t=+{until:5.0f}s] " + " | ".join(parts) + f" | p50 {p50:7.1f} ms  p99 {p99:7.1f} ms"

    def run(self) -> int:
        args = self.args
        ensure_bucket(self.client, args.bucket)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        print(
            f"traffic '{args.label}': {args.workers} workers, {args.duration:.0f} s, "
            f"write ratio {args.write_ratio:.0%}, writes {human_bytes(self.low)}-{human_bytes(self.high)}, "
            f"{len(self.read_keys)} readable keys, retries={args.retries}, timeout={args.timeout:g} s"
        )
        print(f"started at {datetime.now(timezone.utc).isoformat(timespec='seconds')} -> {self.csv_path}")
        print("Ctrl-C stops early and still writes the summary.", flush=True)

        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        with self.csv_path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["t_s", "wall_utc", "op", "key", "size", "latency_ms", "ok", "error"])
            threads = [
                threading.Thread(target=self.worker, args=(args.seed + i, writer), daemon=True)
                for i in range(args.workers)
            ]
            for t in threads:
                t.start()
            last = 0.0
            while not self.stop.is_set():
                remaining = args.duration - (time.monotonic() - self.t0)
                if remaining <= 0:
                    break
                self.stop.wait(min(args.report_every, remaining))
                now = time.monotonic() - self.t0
                print(self.report(last, now), flush=True)
                last = now
            self.stop.set()
            for t in threads:
                t.join(timeout=args.timeout * (args.retries + 1) + 5)
        return self.summary()

    def summary(self) -> int:
        with self.lock:
            rows = list(self.rows)
        elapsed = time.monotonic() - self.t0
        print(f"\nsummary '{self.args.label}' over {elapsed:.0f} s ({self.csv_path})")
        with self.csv_path.open() as fh:
            errors_by_label = Counter(f"{rec['op']}: {rec['error']}" for rec in csv.DictReader(fh) if rec["ok"] == "0")
        for op in ("write", "read"):
            ops = [r for r in rows if r[1] == op]
            if not ops:
                continue
            errs = sum(1 for r in ops if not r[2])
            lat = sorted(r[3] for r in ops if r[2])
            print(
                f"  {op:<5} {len(ops):>6} ops  {errs:>5} errors ({errs / len(ops):6.2%})  "
                f"p50 {percentile(lat, 50):7.1f} ms  p99 {percentile(lat, 99):7.1f} ms"
            )
        for label, count in errors_by_label.most_common():
            print(f"  {count:>6} x {label}")
        print("next: make verify")
        return 0


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--workers", type=int, help="default 8 (fill), 4 (continuous)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-size", help="default 1KiB (fill), 4KiB (continuous)")
    parser.add_argument("--max-size", help="default 16MiB (fill), 1MiB (continuous)")
    parser.add_argument("--retries", type=int, help="retries per request, default 2 (fill), 0 (continuous)")

    fill = parser.add_argument_group("fill mode")
    fill.add_argument("--count", type=int, default=1000)
    fill.add_argument("--prefix", default="obj/")

    cont = parser.add_argument_group("continuous mode")
    cont.add_argument("--continuous", action="store_true")
    cont.add_argument("--duration", type=float, default=180, help="seconds (default 180)")
    cont.add_argument("--label", default="run", help="scenario label used in file names and keys")
    cont.add_argument("--write-ratio", type=float, default=0.5)
    cont.add_argument("--timeout", type=float, default=5, help="connect and read timeout in seconds")
    cont.add_argument("--report-every", type=float, default=5)

    args = parser.parse_args()
    if args.continuous:
        args.workers = args.workers or 4
        args.retries = 0 if args.retries is None else args.retries
        return Traffic(args).run()
    args.workers = args.workers or 8
    args.retries = 2 if args.retries is None else args.retries
    return run_fill(args)


if __name__ == "__main__":
    sys.exit(main())
