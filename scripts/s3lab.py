"""Shared helpers for load.py, verify.py and the benchmark: S3 client, sizes, manifest."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

import boto3
from botocore.config import Config

DEFAULT_ENDPOINT = os.environ.get("LAB_S3_ENDPOINT", "http://127.0.0.1:8333")
DEFAULT_BUCKET = os.environ.get("LAB_S3_BUCKET", "lab")
DEFAULT_MANIFEST = Path(".state/manifest.jsonl")

# Must match config/s3.json.
ACCESS_KEY = os.environ.get("LAB_S3_ACCESS_KEY", "lab-access-key")
SECRET_KEY = os.environ.get("LAB_S3_SECRET_KEY", "lab-secret-key")

_UNITS = {"": 1, "B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "KB": 1000, "MB": 1000**2, "GB": 1000**3}


def parse_size(text: str) -> int:
    """'16MiB' -> 16777216. Binary (KiB, MiB, GiB) and decimal (KB, MB, GB) units."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", text)
    if not match or match.group(2).upper() not in _UNITS:
        raise ValueError(f"invalid size: {text!r} (examples: 4KiB, 1MiB, 16MiB)")
    return int(float(match.group(1)) * _UNITS[match.group(2).upper()])


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} GiB"


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile on an already sorted list (q in 0..100)."""
    if not sorted_values:
        return math.nan
    rank = max(1, math.ceil(q / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def make_client(
    endpoint: str,
    *,
    pool: int = 16,
    connect_timeout: float = 5,
    read_timeout: float = 60,
    attempts: int = 3,
):
    """boto3 S3 client tuned for the lab.

    attempts=1 disables retries, so that faults show up as errors instead of
    being hidden behind longer latencies.
    """
    config = Config(
        s3={"addressing_style": "path"},
        signature_version="s3v4",
        max_pool_connections=pool,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retries={"max_attempts": attempts, "mode": "standard"},
        # Plain PUTs: no aws-chunked trailing checksums, which some
        # S3-compatible servers handle differently from AWS.
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="us-east-1",
        config=config,
    )


def ensure_bucket(client, bucket: str) -> None:
    existing = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
    if bucket not in existing:
        client.create_bucket(Bucket=bucket)


def error_label(exc: BaseException) -> str:
    """Short, groupable description of an exception (S3 error code or class name)."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code:
            return f"{code} ({status})" if status else str(code)
    return type(exc).__name__


def read_manifest(path: Path) -> dict[str, dict]:
    """Manifest = JSON lines {key, size, sha256}. The last line for a key wins."""
    entries: dict[str, dict] = {}
    if not path.exists():
        return entries
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                entry = json.loads(line)
                entries[entry["key"]] = entry
    return entries
