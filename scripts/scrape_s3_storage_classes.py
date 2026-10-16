"""Produces `providers/aws/catalogs/s3_storage_classes.csv` -- performance-relevant characteristics of S3
storage classes, scraped from AWS's own comparison table (docs.aws.amazon.com/AmazonS3/latest/
userguide/storage-class-intro.html). New catalog -- `aws_s3_bucket` isn't in today's provider
vocab yet; wiring it in is a milestone 6.2/6.3 question, not this script.

Durability/availability are published as "designed for" percentages (not SLAs), kept as floats.
`min_storage_duration_days` and `min_billable_object_size_kb` are 0 where the page says "None".
`availability_zones_min` parses AWS's ">= 3" / "1" notation to the integer floor.

Usage:
    pip install -e ".[scraper]"
    python3 scripts/scrape_s3_storage_classes.py [--out src/poliac/providers/aws/catalogs/s3_storage_classes.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-class-intro.html"

_EXPECTED_HEADERS = [
    "Storage class",
    "Designed for",
    "Durability (designed for)",
    "Availability (designed for)",
    "Availability Zones",
    "Min storage duration",
    "Min billable object size",
    "Other considerations",
]

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/s3_storage_classes.csv"


class ScrapeError(Exception):
    pass


def _percent(text: str) -> str:
    match = re.search(r"[\d.]+", text)
    if match is None:
        raise ScrapeError(f"no percentage found in {text!r}")
    return match.group()


def _days(text: str) -> str:
    if text.strip().lower() == "none":
        return "0"
    match = re.search(r"\d+", text)
    if match is None:
        raise ScrapeError(f"no day count found in {text!r}")
    return match.group()


def _kb(text: str) -> str:
    text = text.strip().lower()
    if text == "none" or text.startswith("na"):  # Glacier classes: "NA*"/"NA**" (footnoted)
        return "0"
    match = re.search(r"([\d.]+)\s*kb", text)
    if match is None:
        raise ScrapeError(f"no KB size found in {text!r}")
    return match.group(1)


def _az_min(text: str) -> str:
    match = re.search(r"\d+", text)
    if match is None:
        raise ScrapeError(f"no AZ count found in {text!r}")
    return match.group()


def scrape(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ScrapeError("no table found on the page")

    header_cells = [th.get_text(strip=True) for th in table.find("tr").find_all(["th", "td"])]
    if header_cells != _EXPECTED_HEADERS:
        raise ScrapeError(f"unexpected table columns: {header_cells!r}")

    entries = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) != len(_EXPECTED_HEADERS):
            continue
        # "S3 Standard (STANDARD)" -- the code in parens is the actual API/Terraform value.
        name_cell = cells[0].get_text(" ", strip=True)
        code_match = re.search(r"\(\s*([A-Z_]+)\s*\)", name_cell)
        if code_match is None:
            raise ScrapeError(f"couldn't find a storage class code in {name_cell!r}")
        entries.append(
            {
                "storage_class": code_match.group(1),
                "durability_pct": _percent(cells[2].get_text(strip=True)),
                "availability_pct": _percent(cells[3].get_text(strip=True)),
                "availability_zones_min": _az_min(cells[4].get_text(strip=True)),
                "min_storage_duration_days": _days(cells[5].get_text(strip=True)),
                "min_billable_object_size_kb": _kb(cells[6].get_text(strip=True)),
            }
        )
    if not entries:
        raise ScrapeError("found zero storage classes")
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

    entries.sort(key=lambda e: e["storage_class"])
    fieldnames = [
        "storage_class",
        "durability_pct",
        "availability_pct",
        "availability_zones_min",
        "min_storage_duration_days",
        "min_billable_object_size_kb",
    ]
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entries)

    print(f"wrote {len(entries)} storage classes to {args.out} (source: {args.url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
