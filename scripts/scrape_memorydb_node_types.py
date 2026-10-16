"""Regenerates `providers/aws/catalogs/memorydb_node_types.csv` for `aws_memorydb_cluster`, from AWS's own
node-type documentation (docs.aws.amazon.com/memorydb/latest/devguide/nodes.supportedtypes.html).
New catalog -- `aws_memorydb_cluster` isn't in today's provider vocab yet.

MemoryDB node types are named like RDS classes (`db.r7g.large`, not `cache.r7g.large` the way
ElastiCache names them) but are the *same underlying EC2 hardware* -- AWS's own table only
publishes network bandwidth and minimum engine version here, not memory. `memory_gib` is instead
looked up from `providers/aws/catalogs/aws_instances.csv` (this project's own already-scraped EC2 catalog) by
stripping the `db.` prefix, e.g. `db.r7g.large` -> `r7g.large`. **Run
`scrape_ec2_instance_types.py` first** (or otherwise have an up-to-date `aws_instances.csv`
checked in) -- any MemoryDB node type whose base EC2 type isn't found there is dropped with a
warning, not silently given a blank/wrong memory figure.

Usage:
    pip install -e ".[scraper]"
    python3 scripts/scrape_memorydb_node_types.py [--out src/poliac/providers/aws/catalogs/memorydb_node_types.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://docs.aws.amazon.com/memorydb/latest/devguide/nodes.supportedtypes.html"

_REQUIRED_COLUMNS = ["Instance type", "Baseline bandwidth (Gbps)", "Burst bandwidth (Gbps)"]

_FAMILY_PREFIX = {"t": "burstable", "m": "general", "c": "compute", "r": "memory"}

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/memorydb_node_types.csv"
_EC2_CATALOG = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/aws_instances.csv"


class ScrapeError(Exception):
    pass


def _family(instance_type: str) -> str:
    match = re.match(r"db\.([a-z]+)", instance_type)
    letter = match.group(1)[0] if match else ""
    return _FAMILY_PREFIX.get(letter, "other")


def _load_ec2_memory(path: Path) -> dict[str, str]:
    with path.open(newline="") as f:
        return {row["instance_type"]: row["memory_gib"] for row in csv.DictReader(f)}


def scrape(html: str, ec2_memory: dict[str, str]) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")

    by_instance_type: dict[str, dict[str, str]] = {}
    matched_tables = 0
    for table in soup.find_all("table"):
        headers = [c.get_text(strip=True) for c in table.find("tr").find_all(["th", "td"])]
        if not all(col in headers for col in _REQUIRED_COLUMNS):
            continue
        matched_tables += 1
        idx = {col: headers.index(col) for col in _REQUIRED_COLUMNS}
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) != len(headers):
                continue
            instance_type = cells[idx["Instance type"]].get_text(strip=True)
            base_type = re.sub(r"^db\.", "", instance_type)
            memory_gib = ec2_memory.get(base_type)
            if memory_gib is None:
                print(
                    f"warning: {instance_type!r} -> base EC2 type {base_type!r} not found in "
                    f"aws_instances.csv, skipping",
                    file=sys.stderr,
                )
                continue
            by_instance_type[instance_type] = {
                "instance_type": instance_type,
                "memory_gib": memory_gib,
                "baseline_bandwidth_gbps": cells[idx["Baseline bandwidth (Gbps)"]].get_text(strip=True),
                "burst_bandwidth_gbps": cells[idx["Burst bandwidth (Gbps)"]].get_text(strip=True),
                "family": _family(instance_type),
            }

    if matched_tables == 0:
        raise ScrapeError(f"no table matched expected columns {_REQUIRED_COLUMNS!r}")
    if not by_instance_type:
        raise ScrapeError("found zero node types with a resolvable memory figure")
    return list(by_instance_type.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--ec2-catalog", type=Path, default=_EC2_CATALOG)
    parser.add_argument("--url", default=URL)
    args = parser.parse_args()

    if not args.ec2_catalog.is_file():
        print(f"error: EC2 catalog not found at {args.ec2_catalog} -- run scrape_ec2_instance_types.py first", file=sys.stderr)
        return 1
    ec2_memory = _load_ec2_memory(args.ec2_catalog)

    response = requests.get(args.url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    response.raise_for_status()
    response.encoding = "utf-8"

    try:
        entries = scrape(response.text, ec2_memory)
    except ScrapeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    entries.sort(key=lambda e: e["instance_type"])
    fieldnames = ["instance_type", "memory_gib", "baseline_bandwidth_gbps", "burst_bandwidth_gbps", "family"]
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entries)

    print(f"wrote {len(entries)} node types to {args.out} (source: {args.url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
