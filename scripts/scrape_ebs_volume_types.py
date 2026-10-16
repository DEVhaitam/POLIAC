"""Regenerates `providers/aws/catalogs/ebs_volume_types.csv` from AWS's own EBS volume types documentation,
instead of hand-maintaining it.

Source: https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volume-types.html -- AWS publishes
three small HTML tables there (SSD volumes, HDD volumes, previous-generation Magnetic), each
row-oriented: one row per spec ("Volume type", "Max IOPS", "Max throughput", ...), one column per
volume type. That shape is parsed directly (colspan-aware) rather than via any AWS API -- there's
no API that exposes per-volume-type IOPS/throughput ceilings; that data only exists in this
documentation.

`iops_configurable` (whether you can provision IOPS independent of volume size) isn't a labelled
row on the page in a form that's safe to parse programmatically, so it stays a small curated
constant here -- it's a fact about EBS's product design (gp3/io1/io2 support provisioned IOPS,
gp2/st1/sc1/standard don't), not something that changes with the numbers scraped above it.

Usage:
    pip install -e ".[scraper]"
    python3 scripts/scrape_ebs_volume_types.py [--out src/poliac/providers/aws/catalogs/ebs_volume_types.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

SOURCE_URL = "https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volume-types.html"

# Not derivable from the page's tables -- see module docstring.
IOPS_CONFIGURABLE = {"gp3", "io1", "io2"}

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/ebs_volume_types.csv"


class ScrapeError(Exception):
    pass


def _row_cells(row: Tag) -> list[str]:
    """One text cell per column, colspan cells repeated to fill their span."""
    cells: list[str] = []
    for cell in row.find_all(["td", "th"]):
        span = int(cell.get("colspan", 1))
        cells.extend([cell.get_text(" ", strip=True)] * span)
    return cells


def _table_rows_by_label(table: Tag) -> dict[str, list[str]]:
    """First cell of each row is its label (e.g. "Volume type", "Max IOPS"); the rest are the
    per-column values, in column order. The header row (volume-type names / doc links) has no
    usable label so it's skipped here -- the "Volume type" body row gives us that instead."""
    rows: dict[str, list[str]] = {}
    for tr in table.find_all("tr"):
        cells = _row_cells(tr)
        if len(cells) < 2:
            continue
        label, values = cells[0], cells[1:]
        if label:
            rows[label] = values
    return rows


def _leading_number(text: str, *, take_max_of_range: bool) -> int:
    """"80,000 3 (25.6 KiB I/O 4)" -> 80000. "40-200" -> 200 if take_max_of_range else 40."""
    numbers = re.findall(r"[\d,]+", text)
    if not numbers:
        raise ScrapeError(f"no number found in {text!r}")
    chosen = numbers[-1] if (take_max_of_range and len(numbers) > 1) else numbers[0]
    return int(chosen.replace(",", ""))


def _row_starting_with(rows: dict[str, list[str]], *prefixes: str) -> list[str] | None:
    for label, values in rows.items():
        if label.startswith(prefixes):
            return values
    return None


def _parse_table(table: Tag, *, category: str) -> list[dict[str, str]]:
    rows = _table_rows_by_label(table)
    volume_types = rows.get("Volume type")
    iops_row = _row_starting_with(rows, "Max IOPS")
    throughput_row = _row_starting_with(rows, "Max throughput")
    if not volume_types or not iops_row or not throughput_row:
        raise ScrapeError(
            f"expected 'Volume type'/'Max IOPS'/'Max throughput' rows, got labels {list(rows)!r}"
        )
    if not (len(volume_types) == len(iops_row) == len(throughput_row)):
        raise ScrapeError(
            f"column count mismatch: types={volume_types!r} iops={iops_row!r} "
            f"throughput={throughput_row!r}"
        )

    entries = []
    for volume_type_cell, iops_text, throughput_text in zip(volume_types, iops_row, throughput_row):
        # cell text can carry a footnote marker or trailing qualifier, e.g. "gp3 6" or
        # "io2 Block Express" -- the volume type code itself is always the leading token.
        match = re.match(r"[a-z0-9]+", volume_type_cell.strip())
        if match is None:
            raise ScrapeError(f"couldn't extract a volume type code from {volume_type_cell!r}")
        volume_type = match.group()
        is_range = bool(re.search(r"\d\s*[-–]\s*\d", iops_text))
        entries.append(
            {
                "volume_type": volume_type,
                "category": category,
                "max_iops": str(_leading_number(iops_text, take_max_of_range=is_range)),
                "max_throughput_mbps": str(
                    _leading_number(throughput_text, take_max_of_range=is_range)
                ),
                "iops_configurable": str(volume_type in IOPS_CONFIGURABLE).lower(),
            }
        )
    return entries


def scrape(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if len(tables) != 3:
        raise ScrapeError(
            f"expected exactly 3 tables (SSD, HDD, Magnetic) on the page, found {len(tables)} -- "
            "the page structure may have changed, check manually before trusting this output"
        )
    ssd_table, hdd_table, magnetic_table = tables
    entries = (
        _parse_table(ssd_table, category="ssd")
        + _parse_table(hdd_table, category="hdd")
        + _parse_table(magnetic_table, category="hdd")  # "standard" is HDD-backed, per the doc text
    )
    seen = {e["volume_type"] for e in entries}
    expected = {"gp2", "gp3", "io1", "io2", "st1", "sc1", "standard"}
    if seen != expected:
        raise ScrapeError(f"expected volume types {expected}, got {seen}")
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--url", default=SOURCE_URL)
    args = parser.parse_args()

    response = requests.get(args.url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    response.raise_for_status()
    response.encoding = "utf-8"  # AWS doesn't send a charset in Content-Type; requests defaults
    # to ISO-8859-1 without one, which mangles the en dashes in the Magnetic volume's ranges.

    try:
        entries = scrape(response.text)
    except ScrapeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    entries.sort(key=lambda e: e["volume_type"])
    fieldnames = ["volume_type", "category", "max_iops", "max_throughput_mbps", "iops_configurable"]
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entries)

    print(f"wrote {len(entries)} volume types to {args.out} (source: {args.url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
