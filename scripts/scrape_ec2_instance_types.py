"""Regenerates `providers/aws/catalogs/aws_instances.csv` from AWS's own EC2 instance types documentation.

AWS splits instance type specs across one page per top-level category:
https://docs.aws.amazon.com/ec2/latest/instancetypes/{gp,co,mo,so,ac,hpc}.html (general purpose,
compute/memory/storage optimized, accelerated computing, HPC optimized). Each page has a
"Performance specifications" table -- one row per instance type, with family-name separator rows
(e.g. a single `<td colspan="8"><b>M5</b></td>` row) in between -- covering every instance type
AWS currently documents, not a hand-picked sample.

Only `instance_type` / `vcpu` / `memory_gib` / `family` are extracted, matching the existing
catalog schema (`providers/registry.py` doesn't yet model anything else about compute -- adding
columns like the same pages' network/EBS-bandwidth tables is a schema question for milestone 6.3,
not this script).

`family` isn't a column on the page -- it's derived from which page an instance type came from
(AWS's own top-level category, `_PAGE_FAMILY`), except general purpose (`gp.html`) bundles both
T-series (burstable-credit performance model) and M-series (fixed performance) together, and Mac
instances (dedicated Apple hardware) alongside them -- those three are genuinely different
performance models, so `gp` gets a name-prefix override to keep them apart.

Usage:
    pip install -e ".[scraper]"
    python3 scripts/scrape_ec2_instance_types.py [--out src/poliac/providers/aws/catalogs/aws_instances.csv]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://docs.aws.amazon.com/ec2/latest/instancetypes/{page}.html"

_PAGE_FAMILY = {
    "gp": "general",  # split further into burstable/mac by name prefix, see _family()
    "co": "compute",
    "mo": "memory",
    "so": "storage",
    "ac": "accelerated",
    "hpc": "hpc",
}

_EXPECTED_HEADERS = [
    "Instance type",
    "Memory (GiB)",
    "Processor",
    "vCPUs",
    "CPU cores",
    "Threads per core",
    "Accelerators",
    "Accelerator memory",
]

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/aws_instances.csv"


class ScrapeError(Exception):
    pass


def _family(page: str, instance_type: str) -> str:
    it = instance_type.lower()
    if page != "gp":
        return _PAGE_FAMILY[page]
    if it.startswith("mac"):
        return "mac"
    if it.startswith("t"):
        return "burstable"
    return "general"  # M-series -- fixed performance, not burstable


def _format_number(text: str) -> str:
    value = float(text.strip())
    return str(int(value)) if value.is_integer() else str(value)


def _performance_table(soup: BeautifulSoup) -> Tag:
    heading = soup.find(
        lambda tag: tag.name in ("h2", "h3") and tag.get_text(strip=True) == "Performance specifications"
    )
    if heading is None:
        raise ScrapeError("no 'Performance specifications' heading found")
    table = heading.find_next("table")
    if table is None:
        raise ScrapeError("no table found after 'Performance specifications' heading")
    return table


def _scrape_page(html: str, page: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = _performance_table(soup)

    header_cells = [th.get_text(strip=True) for th in table.find("tr").find_all(["th", "td"])]
    if header_cells != _EXPECTED_HEADERS:
        raise ScrapeError(f"unexpected table columns on {page}.html: {header_cells!r}")

    entries: list[dict[str, str]] = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) != len(_EXPECTED_HEADERS):
            continue  # family-name separator row (single colspan cell), not a data row
        for footnote in cells[0].find_all("sup"):
            footnote.decompose()  # e.g. "t3.nano <sup>1</sup>" -- the footnote marker, not part of the name
        instance_type = cells[0].get_text(strip=True)
        entries.append(
            {
                "instance_type": instance_type,
                "vcpu": _format_number(cells[3].get_text(strip=True)),
                "memory_gib": _format_number(cells[1].get_text(strip=True)),
                "family": _family(page, instance_type),
            }
        )
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = parser.parse_args()

    all_entries: list[dict[str, str]] = []
    for page in _PAGE_FAMILY:
        url = BASE_URL.format(page=page)
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        response.raise_for_status()
        response.encoding = "utf-8"
        try:
            entries = _scrape_page(response.text, page)
        except ScrapeError as e:
            print(f"error scraping {url}: {e}", file=sys.stderr)
            return 1
        if not entries:
            print(f"error: found zero instance types on {url}", file=sys.stderr)
            return 1
        print(f"{page}.html: {len(entries)} instance types")
        all_entries.extend(entries)

    seen = {e["instance_type"] for e in all_entries}
    if len(seen) != len(all_entries):
        raise ScrapeError("duplicate instance_type across pages -- investigate before trusting output")

    all_entries.sort(key=lambda e: e["instance_type"])
    fieldnames = ["instance_type", "vcpu", "memory_gib", "family"]
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_entries)

    print(f"wrote {len(all_entries)} instance types to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
