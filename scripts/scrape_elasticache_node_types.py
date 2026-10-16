"""Regenerates `providers/aws/catalogs/elasticache_node_types.csv` from AWS's own supported-node-types
documentation (docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/CacheNodes.SupportedTypes.html).

New catalog -- neither `aws_elasticache_cluster` nor `aws_elasticache_replication_group` is in
today's provider vocab yet.

The page has 12 tables (at the time this was written): one set each for Memcached, Valkey, and
Redis OSS engines, split further by node family, plus one for burstable T-family nodes. Only the
11 non-burstable tables are scraped here -- they share four columns this script actually needs
(`Instance type`, `Memory (GiB)`, `Baseline bandwidth (Gbps)`, `Burst bandwidth (Gbps)`), located
by header name so engine-specific extra columns (TLS offloading, enhanced I/O, ...) don't matter.
The T-family table has an entirely different column set (CPU credits, vCPUs, "Network
performance" as prose instead of a Gbps figure) and isn't scraped -- it would need its own parser
for what's a small slice of the catalog; noted here rather than silently dropped.

The same underlying hardware (e.g. `cache.r5.12xlarge`) is listed once per engine it supports, so
duplicates are expected -- and one real discrepancy exists in AWS's own numbers (`cache.r5.12xlarge`
baseline bandwidth is documented as 20 Gbps for Memcached and 12 Gbps for Valkey/Redis, most likely
an engine-side network optimization difference, not a copy error). Duplicates are deduplicated by
first occurrence, and any actual value conflict is a warning printed to stderr, not a hard
failure -- one documented AWS quirk shouldn't block collecting the other ~99% of the table.

Usage:
    pip install -e ".[scraper]"
    python3 scripts/scrape_elasticache_node_types.py [--out src/poliac/providers/aws/catalogs/elasticache_node_types.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

URL = "https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/CacheNodes.SupportedTypes.html"

_REQUIRED_COLUMNS = ["Instance type", "Memory (GiB)", "Baseline bandwidth (Gbps)", "Burst bandwidth (Gbps)"]

_FAMILY_PREFIX = {
    "t": "burstable",
    "m": "general",
    "c": "compute",
    "r": "memory",
}

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/elasticache_node_types.csv"


class ScrapeError(Exception):
    pass


def _family(instance_type: str) -> str:
    match = re.match(r"cache\.([a-z]+)", instance_type)
    letter = match.group(1)[0] if match else ""
    return _FAMILY_PREFIX.get(letter, "other")


def _parse_table(table: Tag) -> list[dict[str, str]] | None:
    headers = [c.get_text(strip=True) for c in table.find("tr").find_all(["th", "td"])]
    if not all(col in headers for col in _REQUIRED_COLUMNS):
        return None  # the burstable T-family table, or anything else with a different shape
    idx = {col: headers.index(col) for col in _REQUIRED_COLUMNS}

    entries = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) != len(headers):
            continue
        instance_type = cells[idx["Instance type"]].get_text(strip=True)
        entries.append(
            {
                "instance_type": instance_type,
                "memory_gib": cells[idx["Memory (GiB)"]].get_text(strip=True).replace(",", ""),
                "baseline_bandwidth_gbps": cells[idx["Baseline bandwidth (Gbps)"]].get_text(strip=True),
                "burst_bandwidth_gbps": cells[idx["Burst bandwidth (Gbps)"]].get_text(strip=True),
                "family": _family(instance_type),
            }
        )
    return entries


def scrape(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if len(tables) < 10:
        raise ScrapeError(f"expected around a dozen tables, found {len(tables)} -- page structure may have changed")

    by_instance_type: dict[str, dict[str, str]] = {}
    matched_tables = 0
    for table in tables:
        entries = _parse_table(table)
        if entries is None:
            continue
        matched_tables += 1
        for entry in entries:
            it = entry["instance_type"]
            existing = by_instance_type.get(it)
            if existing is None:
                by_instance_type[it] = entry
            elif existing != entry:
                print(
                    f"warning: {it!r} has conflicting specs across engine tables: "
                    f"{existing} vs {entry} -- keeping the first",
                    file=sys.stderr,
                )

    if matched_tables == 0:
        raise ScrapeError(f"no table matched expected columns {_REQUIRED_COLUMNS!r}")
    if not by_instance_type:
        raise ScrapeError("found zero node types")
    return list(by_instance_type.values())


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
