"""Metrics CSV loading. Raw strings only -- no coercion, no interpretation."""

from __future__ import annotations

import csv
from pathlib import Path

from pydantic import BaseModel


class MetricsError(Exception):
    """Raised on anything that makes the CSV unsafe to hand to the model: too big, malformed."""


class MetricsTable(BaseModel):
    path: str
    headers: list[str]
    rows: list[dict[str, str]]  # raw strings, no coercion

    def cell(self, row: int, column: str) -> str | None:
        if row < 0 or row >= len(self.rows):
            return None
        return self.rows[row].get(column)


def load_metrics(path: Path, max_rows: int = 300, max_bytes: int = 100_000) -> MetricsTable:
    if not path.is_file():
        raise MetricsError(f"{path}: no such file")

    size = path.stat().st_size
    if size > max_bytes:
        raise MetricsError(f"{path}: {size} bytes exceeds max_bytes={max_bytes}")

    with path.open(newline="") as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            raise MetricsError(f"{path}: empty CSV, no header row") from None

        duplicates = {h for h in headers if headers.count(h) > 1}
        if duplicates:
            raise MetricsError(f"{path}: duplicate column header(s): {sorted(duplicates)}")

        rows = [dict(zip(headers, record)) for record in reader]

    if len(rows) > max_rows:
        raise MetricsError(f"{path}: {len(rows)} rows exceeds max_rows={max_rows}")

    return MetricsTable(path=str(path), headers=headers, rows=rows)