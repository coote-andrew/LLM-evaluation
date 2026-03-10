"""
CSV/Excel ingestion with input_/output_ column convention.

Columns prefixed with input_ are fed into the prompt.
Columns prefixed with output_ are used for scoring.
"""

import csv
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


def _normalize_columns(columns: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split columns into input_, output_, and other."""
    input_cols = [c for c in columns if c.startswith("input_")]
    output_cols = [c for c in columns if c.startswith("output_")]
    all_cols = list(columns)
    return all_cols, input_cols, output_cols


def parse_csv(content: bytes | str, filename: str = "") -> dict[str, Any]:
    """
    Parse CSV content. Returns dict with:
    - column_names: list of all column names
    - input_columns: columns starting with input_
    - output_columns: columns starting with output_
    - rows: list of dicts, each with input_fields and expected_output_fields
    - row_count: number of rows
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    reader = csv.DictReader(StringIO(content))
    columns = reader.fieldnames or []
    all_cols, input_cols, output_cols = _normalize_columns(columns)

    rows = []
    for i, row in enumerate(reader, start=1):
        input_fields = {k: row.get(k, "") for k in input_cols if k in row}
        expected_output_fields = {k: row.get(k, "") for k in output_cols if k in row}
        rows.append({
            "row_number": i,
            "input_fields": input_fields,
            "expected_output_fields": expected_output_fields,
        })

    return {
        "column_names": all_cols,
        "input_columns": input_cols,
        "output_columns": output_cols,
        "rows": rows,
        "row_count": len(rows),
        "original_filename": filename or "upload.csv",
    }


def parse_excel(content: bytes, filename: str = "") -> dict[str, Any]:
    """
    Parse Excel (.xlsx) content. Uses first sheet.
    Returns same structure as parse_csv.
    """
    wb = load_workbook(filename=BytesIO(content), read_only=True, data_only=True)
    sheet = wb.active
    if not sheet:
        return {
            "column_names": [],
            "input_columns": [],
            "output_columns": [],
            "rows": [],
            "row_count": 0,
            "original_filename": filename or "upload.xlsx",
        }

    rows_iter = sheet.iter_rows(values_only=True)
    header = next(rows_iter, None)
    if not header:
        return {
            "column_names": [],
            "input_columns": [],
            "output_columns": [],
            "rows": [],
            "row_count": 0,
            "original_filename": filename or "upload.xlsx",
        }

    columns = [str(c) if c is not None else "" for c in header]
    all_cols, input_cols, output_cols = _normalize_columns(columns)

    rows = []
    for i, row_values in enumerate(rows_iter, start=1):
        row_dict = dict(zip(columns, row_values or []))
        input_fields = {k: str(row_dict.get(k, "") or "") for k in input_cols if k in row_dict}
        expected_output_fields = {k: str(row_dict.get(k, "") or "") for k in output_cols if k in row_dict}
        rows.append({
            "row_number": i,
            "input_fields": input_fields,
            "expected_output_fields": expected_output_fields,
        })

    wb.close()
    return {
        "column_names": all_cols,
        "input_columns": input_cols,
        "output_columns": output_cols,
        "rows": rows,
        "row_count": len(rows),
        "original_filename": filename or "upload.xlsx",
    }


def parse_upload(file_content: bytes, filename: str) -> dict[str, Any]:
    """
    Parse uploaded file (CSV or Excel) based on extension.
    """
    path = Path(filename)
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return parse_excel(file_content, filename)
    return parse_csv(file_content, filename)
