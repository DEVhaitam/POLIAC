"""Produces `providers/aws/catalogs/fsx_windows_throughput.csv` -- the valid `throughput_capacity` values for
`aws_fsx_windows_file_system` and the network/memory specs each one implies, scraped from AWS's
own performance table (docs.aws.amazon.com/fsx/latest/WindowsGuide/performance.html).

New catalog -- `aws_fsx_windows_file_system` isn't in today's provider vocab yet. Like the ECS
Fargate table, this is a "valid value -> implied specs" lookup, not a resource-attribute catalog
in the EC2/RDS sense: `throughput_capacity` is the one thing you actually set, and AWS's own
table is the enum of what's legal.

The source table's "Network IOPS" column uses HTML `rowspan` (one text value like "Tens of
thousands" spans several `throughput_capacity` rows), which a naive per-row cell scrape mis-reads
as fewer columns on the spanned-over rows. `_fill_rowspans` reconstructs the full grid before
parsing, rather than silently shifting columns.

Usage:
    pip install -e ".[scraper]"
    python3 scripts/scrape_fsx_windows_throughput.py [--out src/poliac/providers/aws/catalogs/fsx_windows_throughput.csv]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

URL = "https://docs.aws.amazon.com/fsx/latest/WindowsGuide/performance.html"

_NUM_COLS = 5  # throughput_capacity, network baseline, network burst, network IOPS, memory

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/fsx_windows_throughput.csv"


class ScrapeError(Exception):
    pass


def _fill_rowspans(rows: list[Tag]) -> list[list[str]]:
    pending: dict[int, list] = {}  # col -> [remaining_rows, text]
    grid: list[list[str]] = []
    for row in rows:
        cells = iter(row.find_all(["th", "td"]))
        out = [""] * _NUM_COLS
        for col in range(_NUM_COLS):
            if col in pending and pending[col][0] > 0:
                out[col] = pending[col][1]
                pending[col][0] -= 1
                if pending[col][0] == 0:
                    del pending[col]
                continue
            try:
                cell = next(cells)
            except StopIteration:
                raise ScrapeError(f"row ran out of cells before column {col}: {out!r}")
            out[col] = cell.get_text(" ", strip=True)
            rowspan = int(cell.get("rowspan", 1))
            if rowspan > 1:
                pending[col] = [rowspan - 1, out[col]]
        grid.append(out)
    return grid


def _int(text: str) -> str:
    return text.replace(",", "").strip()


def scrape(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for candidate in soup.find_all("table"):
        header_text = candidate.find("tr").get_text(" ", strip=True)
        if "FSx throughput capacity" in header_text and "Network IOPS" in header_text:
            table = candidate
            break
    if table is None:
        raise ScrapeError("no table found with the expected FSx throughput headers")

    all_rows = table.find_all("tr")
    if len(all_rows) < 3:
        raise ScrapeError(f"expected a header row + sub-header row + data rows, got {len(all_rows)} rows total")
    data_rows = all_rows[2:]  # row 0: main headers (colspan), row 1: Baseline/Burst sub-headers

    entries = []
    for row_cells in _fill_rowspans(data_rows):
        throughput, baseline, burst, iops, memory = row_cells
        entries.append(
            {
                "throughput_capacity_mbps": _int(throughput),
                "network_baseline_mbps": _int(baseline),
                "network_burst_mbps": None if burst.strip() in ("–", "-", "") else _int(burst),
                "network_iops": iops.strip(),  # AWS gives an order-of-magnitude label, not a number
                "memory_gb": _int(memory),
            }
        )
    if not entries:
        raise ScrapeError("found zero throughput capacity rows")
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--url", default=URL)
    args = parser.parse_args()

    response = requests.get(args.url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    response.raise_for_status()
    response.encoding = "utf-8"

    try:
        entries = scrape(response.text)
    except ScrapeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    entries.sort(key=lambda e: int(e["throughput_capacity_mbps"]))
    fieldnames = [
        "throughput_capacity_mbps",
        "network_baseline_mbps",
        "network_burst_mbps",
        "network_iops",
        "memory_gb",
    ]
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entries)

    print(f"wrote {len(entries)} throughput capacity tiers to {args.out} (source: {args.url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
