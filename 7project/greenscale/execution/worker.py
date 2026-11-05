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
    """Generate a generic CSV summary from Google Cloud Storage."""
    from decimal import Decimal, InvalidOperation

    if not 1 <= max_size_mb <= MAX_CSV_MB:
        raise ValueError("Invalid CSV size limit")

    bucket_name, object_name = parse_gcs_uri(uri)

    from google.api_core.exceptions import NotFound
    from google.cloud import storage

    blob = storage.Client().bucket(bucket_name).blob(object_name)

    try:
        blob.reload()
    except NotFound as exc:
        raise ValueError("CSV object not found") from exc

    if blob.size is None:
        raise ValueError("CSV object size is unknown")

    if blob.size > max_size_mb * 1024 * 1024:
        raise ValueError("CSV exceeds size limit")

    row_count = 0
    missing_values = 0
    duplicate_rows = 0
    seen_rows = set()

    with blob.open("rb") as binary_stream:
        text_stream = io.TextIOWrapper(
            binary_stream,
            encoding="utf-8-sig",
            newline=""
        )
        reader = csv.reader(text_stream, strict=True)
        header = next(reader, [])

        if not header or any(not name.strip() for name in header):
            raise ValueError("CSV must have valid column names")

        if len(set(header)) != len(header):
            raise ValueError("Duplicate column names")

        column_count = len(header)
        missing_by_column = {name: 0 for name in header}
        numeric_columns = {name: True for name in header}
        nonempty_counts = {name: 0 for name in header}

        for row in reader:
            if len(row) != column_count:
                raise ValueError("Inconsistent CSV row length")

            row_count += 1

            row_key = tuple(row)

            if row_key in seen_rows:
                duplicate_rows += 1

            seen_rows.add(row_key)

            for name, value in zip(header, row):
                value = value.strip()

                if value == "":
                    missing_values += 1
                    missing_by_column[name] += 1
                    continue

                nonempty_counts[name] += 1

                try:
                    number = Decimal(value)
                    if not number.is_finite():
                        numeric_columns[name] = False
                except InvalidOperation:
                    numeric_columns[name] = False

    data_types = {}

    for name in header:
        if nonempty_counts[name] == 0:
            data_types[name] = "unknown"
        elif numeric_columns[name]:
            data_types[name] = "numeric"
        else:
            data_types[name] = "text"

    result = {
        "workload": "csv_summary",
        "file": uri,
        "rows": row_count,
        "columns": column_count,
        "column_names": header,
        "missing_values": missing_values,
        "missing_by_column": missing_by_column,
        "duplicate_rows": duplicate_rows,
        "data_types": data_types,
        "status": "SUCCESS",
    }

    print(json.dumps(result, indent=2), flush=True)


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
