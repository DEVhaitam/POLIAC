"""Produces `providers/aws/catalogs/ecs_fargate_sizes.csv` -- the valid Fargate `cpu`/`memory` combinations
for `aws_ecs_task_definition`, scraped from AWS's own reference table (docs.aws.amazon.com/
AmazonECS/latest/developerguide/task-cpu-memory-error.html). Fargate only accepts specific
(cpu, memory) pairs, not arbitrary values -- unlike EC2/RDS/EBS, this isn't a per-resource
attribute catalog but a per-*pair* validity table, one row per valid combination.

New catalog, not a replacement of a hand-maintained file -- `aws_ecs_task_definition` isn't in
today's provider vocab at all yet. Wiring this in needs `providers/registry.py`'s `AttributeSpec`
to support checking a *pair* of attributes against a catalog together, not just one column
against one attribute (all it supports today) -- a milestone 6.3 schema question, not this
script's job. This just gets the real data collected.

The source table mixes units inconsistently even within one cell (e.g. "512 MiB, 1 GB, 2 GB"),
and describes some rows as a range-with-increment ("Between 4 GB and 16 GB in 1 GB increments")
rather than an explicit list -- both forms are expanded here into one row per concrete
(cpu_units, memory_mib) pair, with memory normalized to MiB throughout (AWS's own GB figures in
this table are already binary GiB-sized steps, e.g. 1024/2048/3072 MiB, not decimal GB).

Usage:
    pip install -e ".[scraper]"
    python3 scripts/scrape_ecs_fargate_sizes.py [--out src/poliac/providers/aws/catalogs/ecs_fargate_sizes.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html"

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/ecs_fargate_sizes.csv"

_UNIT_TO_MIB = {"MiB": 1, "GB": 1024, "GiB": 1024}


class ScrapeError(Exception):
    pass


def _to_mib(value: str, unit: str) -> int:
    return round(float(value) * _UNIT_TO_MIB[unit])


def _parse_memory_cell(text: str) -> list[int]:
    range_match = re.match(
        r"Between\s+([\d.]+)\s*(MiB|GB|GiB)\s+and\s+([\d.]+)\s*(MiB|GB|GiB)\s+in\s+([\d.]+)\s*(MiB|GB|GiB)\s+increments",
        text,
    )
    if range_match:
        lo = _to_mib(range_match.group(1), range_match.group(2))
        hi = _to_mib(range_match.group(3), range_match.group(4))
        step = _to_mib(range_match.group(5), range_match.group(6))
        return list(range(lo, hi + 1, step))

    values = []
    for token in text.split(","):
        match = re.search(r"([\d.]+)\s*(MiB|GB|GiB)", token)
        if match is None:
            raise ScrapeError(f"couldn't parse memory token {token!r} in cell {text!r}")
        values.append(_to_mib(match.group(1), match.group(2)))
    return values


def scrape(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ScrapeError("no table found on the page")

    header_cells = [th.get_text(strip=True) for th in table.find("tr").find_all(["th", "td"])]
    if header_cells[:2] != ["CPU value", "Memory value"]:
        raise ScrapeError(f"unexpected table columns: {header_cells!r}")

    entries: list[dict[str, str]] = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        cpu_match = re.match(r"(\d+)\s*\(([\d.]+)\s*vCPU\)", cells[0].get_text(strip=True))
        if cpu_match is None:
            raise ScrapeError(f"couldn't parse CPU cell {cells[0].get_text(strip=True)!r}")
        cpu_units = cpu_match.group(1)
        vcpu_value = float(cpu_match.group(2))
        vcpu = str(int(vcpu_value)) if vcpu_value.is_integer() else str(vcpu_value)  # ".25" -> "0.25"
        for memory_mib in _parse_memory_cell(cells[1].get_text(strip=True)):
            entries.append({"cpu_units": cpu_units, "vcpu": vcpu, "memory_mib": str(memory_mib)})

    if not entries:
        raise ScrapeError("found zero cpu/memory combinations")
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

    entries.sort(key=lambda e: (int(e["cpu_units"]), int(e["memory_mib"])))
    fieldnames = ["cpu_units", "vcpu", "memory_mib"]
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entries)

    print(f"wrote {len(entries)} cpu/memory combinations to {args.out} (source: {args.url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
