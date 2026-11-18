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

# Constants for workload limits.
MAX_SLEEP_SECONDS = 24 * 60 * 60
MAX_CSV_MB = 500
MAX_FACTORIAL_N = 50_000
_GCS_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")

# this function is used to parse a Google Cloud Storage URI and extract the bucket name and object name. It validates the URI format and raises a ValueError if the URI is invalid.
def parse_gcs_uri(uri: str) -> tuple[str, str]:
    parsed = urlsplit(uri)
    bucket = parsed.netloc
    object_name = unquote(parsed.path.lstrip("/"))
    # Validate the GCS URI format and bucket name according to Google Cloud Storage naming rules.
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

# this function is used to run a sleep workload for a specified number of seconds. It validates the input and prints messages before and after sleeping.
def run_sleep(seconds: int) -> None:
    # Validate the input seconds and ensure it is within the allowed range. If not, raise a ValueError with an appropriate message.
    if not 1 <= seconds <= MAX_SLEEP_SECONDS:
        raise ValueError(f"sleep duration must be between 1 and {MAX_SLEEP_SECONDS} seconds")
    print(f"Sleep workload started for {seconds} seconds", flush=True)
    time.sleep(seconds)
    print("Sleep workload completed", flush=True)


# this function is used to run a factorial workload for a specified input size. It validates the input and prints the result.
def run_factorial(input_size_mb: int) -> None:
    # Validate the input size and ensure it is within the allowed range. If not, raise a ValueError with an appropriate message.
    if input_size_mb < 1:
        raise ValueError("input size must be at least 1 MB")
    n = min(input_size_mb * 1_000, MAX_FACTORIAL_N)
    value = math.factorial(n)
    print(json.dumps({"workload": "factorial", "n": n, "result_bits": value.bit_length()}), flush=True)


# this function is used to run a CSV summary workload for a specified Google Cloud Storage URI and maximum size. It validates the input, reads the CSV file, and generates a summary of the data.
def run_csv_summary(uri: str, max_size_mb: int) -> None:
    """Generate a generic CSV summary from Google Cloud Storage."""
    # Validate the input parameters and ensure they are within the allowed range. If not, raise a ValueError with an appropriate message.
    from decimal import Decimal, InvalidOperation

    if not 1 <= max_size_mb <= MAX_CSV_MB:
        raise ValueError("Invalid CSV size limit")

    bucket_name, object_name = parse_gcs_uri(uri)

    from google.api_core.exceptions import NotFound
    from google.cloud import storage

    blob = storage.Client().bucket(bucket_name).blob(object_name)

    # Validate that the CSV object exists and can be accessed. If not, raise a ValueError with an appropriate message.
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

    # Read the CSV file from Google Cloud Storage and generate a summary of the data, including row count, column count, missing values, duplicate rows, and data types for each column.
    with blob.open("rb") as binary_stream:
        text_stream = io.TextIOWrapper(
            binary_stream,
            encoding="utf-8-sig",
            newline=""
        )
        reader = csv.reader(text_stream, strict=True)
        header = next(reader, [])

        # Validate the CSV header and ensure it has valid column names and no duplicates. If not, raise a ValueError with an appropriate message.
        if not header or any(not name.strip() for name in header):
            raise ValueError("CSV must have valid column names")
        
        if len(set(header)) != len(header):
            raise ValueError("Duplicate column names")

        
        column_count = len(header)

        # Initialize counters for missing values, duplicate rows, and data types for each column.
        missing_by_column = {name: 0 for name in header}
        numeric_columns = {name: True for name in header}
        nonempty_counts = {name: 0 for name in header}

        # Read each row in the CSV file and update the counters for missing values, duplicate rows, and data types for each column. If a row has an inconsistent length, raise a ValueError with an appropriate message.
        for row in reader:
            if len(row) != column_count:
                raise ValueError("Inconsistent CSV row length")

            row_count += 1

            row_key = tuple(row)

            if row_key in seen_rows:
                duplicate_rows += 1

            seen_rows.add(row_key)

            # Update counters for missing values and data types for each column based on the values in the current row.
            for name, value in zip(header, row):
                value = value.strip()
                
                # If the value is empty
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

    # If a column has no non-empty values, its data type is "unknown". If a column has only numeric values, its data type is "numeric". Otherwise, its data type is "text". 
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

# this function builds an argument parser for the command-line interface (CLI) of the GreenScale workload executor.
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
