"""Regenerates `providers/aws/catalogs/opensearch_instance_types.csv` for `aws_opensearch_domain`
(`cluster_config.instance_type`), from AWS's own supported-instance-types documentation
(docs.aws.amazon.com/opensearch-service/latest/developerguide/supported-instance-types.html).
New catalog -- `aws_opensearch_domain` isn't in today's provider vocab yet.

Unlike the other per-family tables scraped so far, this page doesn't list vCPU/memory at all --
it just enumerates which EC2-family-based names are valid OpenSearch instance types (each one is
literally `<ec2 instance type>.search`, e.g. `m5.large.search`, `r7g.4xlarge.search`), grouped by
family with a free-text "Restrictions" column (EBS support, minimum engine version, ...) that
isn't captured here since it's prose, not a value to validate against.

`vcpu`/`memory_gib` are looked up from `providers/aws/catalogs/aws_instances.csv` (this project's own
already-scraped EC2 catalog) by stripping the `.search` suffix. **Run
`scrape_ec2_instance_types.py` first** -- any OpenSearch instance type whose base EC2 type isn't
found there is dropped with a warning, not silently given a blank/wrong spec.

Usage:
    pip install -e ".[scraper]"
    python3 scripts/scrape_opensearch_instance_types.py [--out src/poliac/providers/aws/catalogs/opensearch_instance_types.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://docs.aws.amazon.com/opensearch-service/latest/developerguide/supported-instance-types.html"

_REQUIRED_COLUMNS = ["Instance type", "Instances"]

_FAMILY_PREFIX = {"t": "burstable", "m": "general", "c": "compute", "r": "memory", "i": "storage"}

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/opensearch_instance_types.csv"
_EC2_CATALOG = Path(__file__).resolve().parent.parent / "src/poliac/providers/aws/catalogs/aws_instances.csv"


class ScrapeError(Exception):
    pass


def _family(base_type: str) -> str:
    letter = base_type[0] if base_type else ""
    return _FAMILY_PREFIX.get(letter, "other")


def _load_ec2_specs(path: Path) -> dict[str, tuple[str, str]]:
    with path.open(newline="") as f:
        return {row["instance_type"]: (row["vcpu"], row["memory_gib"]) for row in csv.DictReader(f)}


def scrape(html: str, ec2_specs: dict[str, tuple[str, str]]) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")

    by_instance_type: dict[str, dict[str, str]] = {}
    matched_tables = 0
    for table in soup.find_all("table"):
        headers = [c.get_text(strip=True) for c in table.find("tr").find_all(["th", "td"])]
        if not all(col in headers for col in _REQUIRED_COLUMNS):
            continue
        matched_tables += 1
        idx_instances = headers.index("Instances")
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) != len(headers):
                continue
            for opensearch_type in cells[idx_instances].get_text(" ", strip=True).split():
                base_type = re.sub(r"\.search$", "", opensearch_type)
                specs = ec2_specs.get(base_type)
                if specs is None:
                    print(
                        f"warning: {opensearch_type!r} -> base EC2 type {base_type!r} not found "
                        f"in aws_instances.csv, skipping",
                        file=sys.stderr,
                    )
                    continue
                vcpu, memory_gib = specs
                by_instance_type[opensearch_type] = {
                    "instance_type": opensearch_type,
                    "vcpu": vcpu,
                    "memory_gib": memory_gib,
                    "family": _family(base_type),
                }

    if matched_tables == 0:
        raise ScrapeError(f"no table matched expected columns {_REQUIRED_COLUMNS!r}")
    if not by_instance_type:
        raise ScrapeError("found zero instance types with a resolvable spec")
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
    ec2_specs = _load_ec2_specs(args.ec2_catalog)

    response = requests.get(args.url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    response.raise_for_status()
    response.encoding = "utf-8"

    try:
        entries = scrape(response.text, ec2_specs)
    except ScrapeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    entries.sort(key=lambda e: e["instance_type"])
    fieldnames = ["instance_type", "vcpu", "memory_gib", "family"]
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entries)

    print(f"wrote {len(entries)} instance types to {args.out} (source: {args.url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
