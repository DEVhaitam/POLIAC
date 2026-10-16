"""Produces `providers/aws/catalogs/aurora_serverless_v2_capacity.csv` -- the valid Aurora Serverless v2
maximum-ACU settings for `aws_rds_cluster` (`serverlessv2_scaling_configuration.max_capacity`),
scraped from AWS's own reference table (docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/
aurora-serverless-v2.setting-capacity.html).

New catalog -- `aws_rds_cluster` isn't in today's provider vocab at all yet. AWS actually allows
`max_capacity` at any 0.5 ACU increment, not just the values in this table; what the table gives
that nowhere else does is the *default connection limit* each documented ceiling implies, split
by engine -- directly performance-relevant (it bounds concurrent connections, not just compute).

Usage:
    pip install -e ".[scraper]"
    python3 scripts/scrape_aurora_serverless_capacity.py [--out src/poliac/providers/aws/catalogs/aurora_serverless_v2_capacity.csv]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.setting-capacity.html"

_EXPECTED_HEADERS = [
    "Maximum ACUs",
    "Default maximum connections on Aurora MySQL",
    "Default maximum connections on Aurora PostgreSQL",
]

_DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/aurora_serverless_v2_capacity.csv"
)


class ScrapeError(Exception):
    pass


def scrape(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for candidate in soup.find_all("table"):
        headers = [c.get_text(strip=True) for c in candidate.find("tr").find_all(["th", "td"])]
        if headers == _EXPECTED_HEADERS:
            table = candidate
            break
    if table is None:
        raise ScrapeError(f"no table found with headers {_EXPECTED_HEADERS!r}")

    entries = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) != 3:
            continue
        entries.append(
            {
                "max_acu": cells[0].get_text(strip=True).replace(",", ""),
                "max_connections_mysql": cells[1].get_text(strip=True).replace(",", ""),
                "max_connections_postgresql": cells[2].get_text(strip=True).replace(",", ""),
            }
        )
    if not entries:
        raise ScrapeError("found zero ACU rows")
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

    entries.sort(key=lambda e: float(e["max_acu"]))
    fieldnames = ["max_acu", "max_connections_mysql", "max_connections_postgresql"]
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entries)

    print(f"wrote {len(entries)} ACU tiers to {args.out} (source: {args.url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
