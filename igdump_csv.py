"""Spreadsheet-safe CSV output. Cached and JSON values remain unmodified."""
from __future__ import annotations

import csv
from pathlib import Path


def csv_cell(value):
    if isinstance(value, str):
        leading = value.lstrip(" \t\r\n\v\f\ufeff\u200b")
        if value.startswith(("\t", "\r", "\n")) or leading.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


def write_csv(path: Path, fields: list[str], rows) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for row in rows:
            writer.writerow([csv_cell(row.get(field, "")) for field in fields])
