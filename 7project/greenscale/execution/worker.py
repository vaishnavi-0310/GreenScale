"""Execute the allowlisted GreenScale batch workloads inside Kubernetes."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import time
from urllib.parse import unquote, urlsplit

MAX_SLEEP_SECONDS = 24 * 60 * 60
MAX_CSV_MB = 500
MAX_FACTORIAL_N = 50_000
_GCS_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    parsed = urlsplit(uri)
    bucket = parsed.netloc
    object_name = unquote(parsed.path.lstrip("/"))
    if (
        parsed.scheme != "gs"
        or not _GCS_BUCKET_RE.fullmatch(bucket)
        or not object_name
        or parsed.query
        or parsed.fragment
        or "\x00" in object_name
    ):
        raise ValueError("CSV input must be a gs://bucket/object URI")
    return bucket, object_name


def run_sleep(seconds: int) -> None:
    if not 1 <= seconds <= MAX_SLEEP_SECONDS:
        raise ValueError(f"sleep duration must be between 1 and {MAX_SLEEP_SECONDS} seconds")
    print(f"Sleep workload started for {seconds} seconds", flush=True)
    time.sleep(seconds)
    print("Sleep workload completed", flush=True)


def run_factorial(input_size_mb: int) -> None:
    if input_size_mb < 1:
        raise ValueError("input size must be at least 1 MB")
    n = min(input_size_mb * 1_000, MAX_FACTORIAL_N)
    value = math.factorial(n)
    print(json.dumps({"workload": "factorial", "n": n, "result_bits": value.bit_length()}), flush=True)


def run_csv_summary(uri: str, max_size_mb: int) -> None:
    if not 1 <= max_size_mb <= MAX_CSV_MB:
        raise ValueError(f"CSV size limit must be between 1 and {MAX_CSV_MB} MB")
    bucket_name, object_name = parse_gcs_uri(uri)

    from google.api_core.exceptions import NotFound
    from google.cloud import storage

    blob = storage.Client().bucket(bucket_name).blob(object_name)
    try:
        blob.reload()
    except NotFound as exc:
        raise ValueError("CSV object was not found in Cloud Storage") from exc

    if blob.size is not None and blob.size > max_size_mb * 1024 * 1024:
        raise ValueError(f"CSV object is larger than the declared {max_size_mb} MB limit")

    with blob.open("rb") as binary_stream:
        text_stream = io.TextIOWrapper(binary_stream, encoding="utf-8-sig", newline="")
        rows = csv.reader(text_stream, strict=True)
        try:
            header = next(rows)
        except StopIteration:
            header = []
            row_count = 0
        else:
            row_count = sum(1 for _ in rows)

    print(json.dumps({
        "workload": "csv_summary",
        "rows": row_count,
        "columns": len(header),
        "column_names": header,
    }), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an allowlisted GreenScale workload")
    subparsers = parser.add_subparsers(dest="workload", required=True)

    sleep = subparsers.add_parser("sleep")
    sleep.add_argument("--seconds", type=int, required=True)

    factorial = subparsers.add_parser("factorial")
    factorial.add_argument("--input-size-mb", type=int, required=True)

    csv_summary = subparsers.add_parser("csv-summary")
    csv_summary.add_argument("--gcs-uri", required=True)
    csv_summary.add_argument("--max-size-mb", type=int, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.workload == "sleep":
            run_sleep(args.seconds)
        elif args.workload == "factorial":
            run_factorial(args.input_size_mb)
        else:
            run_csv_summary(args.gcs_uri, args.max_size_mb)
    except Exception as exc:  # Job failure is reported through Kubernetes Job status.
        print(f"Workload failed ({type(exc).__name__}): {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
