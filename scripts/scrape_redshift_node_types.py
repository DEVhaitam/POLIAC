"""Produces `providers/aws/catalogs/redshift_node_types.csv` -- `aws_redshift_cluster`'s `node_type` catalog,
scraped from AWS's own node-type tables (docs.aws.amazon.com/redshift/latest/mgmt/
working-with-clusters.html). New catalog -- `aws_redshift_cluster` isn't in today's provider
vocab yet.

The page has one table per node family (RA3-successor "rg", RA3, DC2 at the time this was
written), each row-labelled by node type with a "(single-node)"/"(multi-node)" suffix -- that
suffix describes a *cluster configuration*, not the node's own vCPU/RAM, which are identical
between the single- and multi-node listing for the same node type (verified: e.g. `ra3.large`
lists 2 vCPU / 16 GiB RAM under both). Terraform's `node_type` argument only ever takes the bare
name (`"ra3.large"`, not `"ra3.large (multi-node)"`), so that suffix is stripped and the two rows
collapse into one, with a warning (not a hard failure) if they ever disagree.

Storage figures (`storage_per_node`, `total_capacity`) are kept as AWS's own descriptive text
(e.g. "1 TB", "160 GB NVMe-SSD") rather than normalized to one unit -- RA3/current-gen nodes use
decoupled "managed storage" (a limit + scalable total) while DC2 uses fixed local NVMe-SSD, which
aren't the same kind of quantity to begin with. Superscript footnote markers AWS appends to some
cells (e.g. "128 TB 4") are stripped; the footnote text itself isn't captured.

Usage:
    pip install -e ".[scraper]"
    python3 scripts/scrape_redshift_node_types.py [--out src/poliac/providers/aws/catalogs/redshift_node_types.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

URL = "https://docs.aws.amazon.com/redshift/latest/mgmt/working-with-clusters.html"

_REQUIRED_COLUMNS = ["Node type", "vCPU", "RAM (GiB)"]

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/redshift_node_types.csv"


class ScrapeError(Exception):
    pass


def _strip_footnote(text: str) -> str:
    return re.sub(r"\s+\d+$", "", text.strip())


def _strip_header_footnote(text: str) -> str:
    # footnote digits may or may not have a space before them depending on how BeautifulSoup
    # joins adjacent text nodes (e.g. "...per node1" or "...per node 1") -- no real header name
    # in this table ends in a bare digit, so stripping any trailing one is unambiguous.
    return re.sub(r"\s*\d+$", "", text.strip()).strip()


def _column(headers: list[str], *candidates: str) -> int | None:
    for name in candidates:
        if name in headers:
            return headers.index(name)
    return None


def _parse_table(table: Tag) -> list[dict[str, str]] | None:
    headers = [_strip_header_footnote(c.get_text(" ", strip=True)) for c in table.find("tr").find_all(["th", "td"])]
    if not all(col in headers for col in _REQUIRED_COLUMNS):
        return None

    idx_type = headers.index("Node type")
    idx_vcpu = headers.index("vCPU")
    idx_ram = headers.index("RAM (GiB)")
    idx_storage = _column(headers, "Managed storage limit per node", "Storage per node")
    idx_range = _column(headers, "Node range with create cluster", "Node range")
    idx_total = _column(headers, "Total managed storage capacity", "Total capacity")

    entries = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) != len(headers):
            continue
        raw_type = cells[idx_type].get_text(" ", strip=True)
        variant_match = re.search(r"\((single|multi)-node\)\s*$", raw_type)
        node_type = re.sub(r"\s*\((single|multi)-node\)\s*$", "", raw_type)
        entries.append(
            {
                "node_type": node_type,
                "variant": variant_match.group(1) if variant_match else None,
                "vcpu": _strip_footnote(cells[idx_vcpu].get_text(" ", strip=True)),
                "memory_gib": _strip_footnote(cells[idx_ram].get_text(" ", strip=True)),
                "storage_per_node": _strip_footnote(cells[idx_storage].get_text(" ", strip=True)) if idx_storage is not None else "",
                "node_range": _strip_footnote(cells[idx_range].get_text(" ", strip=True)) if idx_range is not None else "",
                "total_capacity": _strip_footnote(cells[idx_total].get_text(" ", strip=True)) if idx_total is not None else "",
            }
        )
    return entries


def scrape(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")

    by_node_type: dict[str, dict[str, str]] = {}
    matched_tables = 0
    for table in tables:
        entries = _parse_table(table)
        if entries is None:
            continue
        matched_tables += 1
        for entry in entries:
            nt = entry["node_type"]
            existing = by_node_type.get(nt)
            if existing is None:
                by_node_type[nt] = entry
                continue
            if existing["vcpu"] != entry["vcpu"] or existing["memory_gib"] != entry["memory_gib"]:
                print(
                    f"warning: {nt!r} has conflicting vcpu/memory across rows: {existing} vs {entry} -- keeping the first",
                    file=sys.stderr,
                )
                continue
            # single-node vs multi-node rows for the same node type -- expected, and not a
            # conflict: multi-node is the more general case, so its storage/range figures win.
            if entry["variant"] == "multi":
                by_node_type[nt] = entry

    if matched_tables == 0:
        raise ScrapeError(f"no table matched expected columns {_REQUIRED_COLUMNS!r}")
    if not by_node_type:
        raise ScrapeError("found zero node types")
    for entry in by_node_type.values():
        del entry["variant"]
    return list(by_node_type.values())


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

    entries.sort(key=lambda e: e["node_type"])
    fieldnames = ["node_type", "vcpu", "memory_gib", "storage_per_node", "node_range", "total_capacity"]
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entries)

    print(f"wrote {len(entries)} node types to {args.out} (source: {args.url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
