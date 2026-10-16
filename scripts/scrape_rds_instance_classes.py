"""Regenerates `providers/aws/catalogs/rds_instance_classes.csv` from AWS's own RDS DB instance class
documentation (docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.DBInstanceClass.Summary.html).

That page has one HTML `<table>` per instance family (db.m9g, db.m8g, db.r6g, db.t3, ...) --
46 of them at the time this was written -- and the columns aren't identical across tables (some
metal classes add "Processor cores"/"Sockets" columns), so columns are located by header name
per table rather than assumed to be in a fixed position. Only `instance_class` / `vcpu` /
`memory_gib` / `family` are extracted, matching the existing catalog schema.

`family` is derived from the instance class name prefix (`db.t*` -> burstable-credit performance
model, `db.m*` -> general purpose, `db.c*` -> compute optimized, `db.r*`/`db.x*`/`db.z*` ->
memory optimized).

Usage:
    pip install -e ".[scraper]"
    python3 scripts/scrape_rds_instance_classes.py [--out src/poliac/providers/aws/catalogs/rds_instance_classes.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

URL = "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.DBInstanceClass.Summary.html"

_FAMILY_PREFIX = {
    "t": "burstable",
    "m": "general",
    "c": "compute",
    "r": "memory",
    "x": "memory",
    "z": "memory",
}

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/rds_instance_classes.csv"


class ScrapeError(Exception):
    pass


def _family(instance_class: str) -> str:
    # "db.m8i.metal-96xl" -> "m"
    match = re.match(r"db\.([a-z]+)", instance_class)
    letter = match.group(1)[0] if match else ""
    return _FAMILY_PREFIX.get(letter, "other")


def _column_index(header_cells: list[str], name: str) -> int:
    try:
        return header_cells.index(name)
    except ValueError:
        raise ScrapeError(f"column {name!r} not found in headers {header_cells!r}")


def _parse_table(table: Tag) -> list[dict[str, str]]:
    header_row = table.find("tr")
    header_cells = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]
    class_col = _column_index(header_cells, "Instance class")
    vcpu_col = _column_index(header_cells, "vCPU")
    memory_col = _column_index(header_cells, "Memory (GiB)")

    entries = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) != len(header_cells):
            continue
        # trailing "*"/"**" are footnote markers (e.g. "db.m5.large*", "db.r3.8xlarge**"), not
        # part of the class name.
        instance_class = re.sub(r"\*+$", "", cells[class_col].get_text(strip=True))
        entries.append(
            {
                "instance_class": instance_class,
                "vcpu": cells[vcpu_col].get_text(strip=True),
                "memory_gib": cells[memory_col].get_text(strip=True).replace(",", ""),
                "family": _family(instance_class),
            }
        )
    return entries


def scrape(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if len(tables) < 10:
        raise ScrapeError(
            f"expected dozens of per-family tables, found {len(tables)} -- page structure may "
            "have changed, check manually before trusting this output"
        )

    entries: list[dict[str, str]] = []
    for table in tables:
        try:
            entries.extend(_parse_table(table))
        except ScrapeError:
            continue  # a table without "Instance class"/"vCPU"/"Memory (GiB)" isn't a class table
    if not entries:
        raise ScrapeError("found zero instance classes across all tables")
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--url", default=URL)
    args = parser.parse_args()

    response = requests.get(args.url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    response.raise_for_status()
    response.encoding = "utf-8"

    try:
        entries = scrape(response.text)
    except ScrapeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    seen = {e["instance_class"] for e in entries}
    if len(seen) != len(entries):
        raise ScrapeError("duplicate instance_class across tables -- investigate before trusting output")

    entries.sort(key=lambda e: e["instance_class"])
    fieldnames = ["instance_class", "vcpu", "memory_gib", "family"]
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entries)

    print(f"wrote {len(entries)} instance classes to {args.out} (source: {args.url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
