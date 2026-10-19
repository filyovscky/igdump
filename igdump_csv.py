"""Spreadsheet-safe CSV output. Cached and JSON values remain unmodified."""
from __future__ import annotations

import csv
import tempfile
from pathlib import Path


def csv_cell(value):
    if isinstance(value, str):
        leading = value.lstrip(" \t\r\n\v\f\ufeff\u200b")
        if value.startswith(("\t", "\r", "\n")) or leading.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


def write_csv(path: Path, fields: list[str], rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='', prefix=path.name+'.', suffix='.tmp', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            writer = csv.writer(handle)
            writer.writerow(fields)
            for row in rows:
                writer.writerow([csv_cell(row.get(field, "")) for field in fields])
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
