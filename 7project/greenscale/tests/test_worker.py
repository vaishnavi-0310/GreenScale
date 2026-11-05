"""Tests for GreenScale's generic CSV worker."""

import io
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from execution.worker import (
    parse_gcs_uri,
    run_csv_summary,
)


def run_with_csv(csv_content, capsys):
    """Run the worker against a mocked GCS CSV file."""
    blob = MagicMock()
    blob.size = len(csv_content.encode("utf-8"))
    blob.open.return_value.__enter__.return_value = io.BytesIO(
        csv_content.encode("utf-8")
    )

    storage = types.ModuleType("google.cloud.storage")
    storage.Client = MagicMock()
    storage.Client.return_value.bucket.return_value.blob.return_value = blob

    with patch.dict(sys.modules, {"google.cloud.storage": storage}):
        run_csv_summary("gs://test-bucket/data.csv", 1)

    return json.loads(capsys.readouterr().out)


def test_sales_csv(capsys):
    csv_data = (
        "order_id,product,quantity\n"
        "1001,Laptop,2\n"
        "1002,Mouse,5\n"
    )

    result = run_with_csv(csv_data, capsys)

    assert result["rows"] == 2
    assert result["columns"] == 3
    assert result["missing_values"] == 0
    assert result["duplicate_rows"] == 0
    assert result["data_types"]["quantity"] == "numeric"
    assert result["data_types"]["product"] == "text"
    assert result["status"] == "SUCCESS"


def test_different_column_names(capsys):
    csv_data = (
        "student,marks,attendance\n"
        "Alice,85,90\n"
        "Bob,92,95\n"
    )

    result = run_with_csv(csv_data, capsys)

    assert result["rows"] == 2
    assert result["column_names"] == [
        "student", "marks", "attendance"
    ]
    assert result["data_types"]["marks"] == "numeric"
    assert result["data_types"]["attendance"] == "numeric"


def test_missing_values(capsys):
    csv_data = (
        "name,age,city\n"
        "Alice,22,\n"
        "Bob,,Oslo\n"
    )

    result = run_with_csv(csv_data, capsys)

    assert result["missing_values"] == 2
    assert result["missing_by_column"]["age"] == 1
    assert result["missing_by_column"]["city"] == 1


def test_duplicate_rows(capsys):
    csv_data = (
        "product,quantity\n"
        "Laptop,2\n"
        "Laptop,2\n"
        "Mouse,5\n"
    )

    result = run_with_csv(csv_data, capsys)

    assert result["duplicate_rows"] == 1
    assert result["rows"] == 3


def test_mixed_data_types(capsys):
    csv_data = (
        "item,value\n"
        "A,10\n"
        "B,Unknown\n"
        "C,30\n"
    )

    result = run_with_csv(csv_data, capsys)

    assert result["data_types"]["value"] == "text"


def test_invalid_row_length(capsys):
    csv_data = (
        "name,age\n"
        "Alice,22\n"
        "Bob\n"
    )

    with pytest.raises(ValueError, match="Inconsistent"):
        run_with_csv(csv_data, capsys)


def test_invalid_gcs_uri():
    with pytest.raises(ValueError):
        parse_gcs_uri("https://example.com/data.csv")
