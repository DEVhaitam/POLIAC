#!/usr/bin/env python3
"""
Turn one results/<config>/<run_id>/ folder (raw Locust + system-sampler CSVs,
one set per user-count step) into a single aggregated CSV: one row per step,
columns grouped by resource dimension (compute / network / storage / database
/ functions).

This is data prep for building example fixtures for poliac. It is NOT part of
poliac's runtime pipeline: it only computes plain descriptive statistics
(mean / max / p95) over numbers that were actually observed. It makes no
judgement about what those numbers mean -- that stays the LLM's job.

Usage:
    python3 scripts/aggregate_run.py results/2cpu-4gb/20260706_011803 \
        --out examples/libvirt/analysis.csv
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

# Steps present in every results/<config>/<run_id>/ dir are auto-detected by
# scanning for `u<N>_system.csv`, ordered by N ascending.

# system.csv columns we consider potentially useful, grouped by dimension.
# Columns not listed here (or found entirely empty for this run) are dropped,
# not zero-filled -- see `_prune_all_empty`.
SYSTEM_COLUMNS = {
    "compute": {
        "cpu_pct": ["mean", "max", "p95"],
        "ram_used_bytes": ["mean", "max"],
        "jvm_heap_bytes": ["mean", "max"],
    },
    "network": {
        "net_rx_bps": ["mean", "max"],
        "net_tx_bps": ["mean", "max"],
    },
    "storage": {
        "disk_read_bps": ["mean", "max"],
        "disk_write_bps": ["mean", "max"],
    },
    "database": {
        "mysql_threads_connected": ["mean", "max"],
        "mysql_queries_per_sec": ["mean", "max"],
        "mysql_slow_queries": ["max"],
        "mysql_cpu_pct": ["mean", "max"],
        "mysql_mem_bytes": ["mean", "max"],
    },
}

STATS_AGGREGATED_COLUMNS = {
    "functions": {
        "Request Count": "request_count",
        "95%": "p95_latency_ms",
        "99%": "p99_latency_ms",
        "Requests/s": "rps",
    }
}


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (same convention as numpy default)."""
    if not values:
        raise ValueError("percentile of empty sequence")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def discover_steps(run_dir: Path) -> list[int]:
    steps = []
    for p in run_dir.glob("u*_system.csv"):
        n = p.name[1:].split("_system.csv")[0]
        if n.isdigit():
            steps.append(int(n))
    return sorted(steps)


def read_system_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, reader.fieldnames or []


def read_stats_aggregated_row(path: Path) -> dict[str, str] | None:
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Name") == "Aggregated":
                return row
    return None


def numeric_column(rows: list[dict[str, str]], col: str) -> list[float]:
    out = []
    for r in rows:
        v = r.get(col, "")
        if v is None or v.strip() == "":
            continue
        try:
            out.append(float(v))
        except ValueError:
            continue
    return out


def ram_pct_column(rows: list[dict[str, str]]) -> list[float]:
    """Per-sample RAM used as a % of ram_total_bytes (0-100), skipping samples
    missing either column or with a zero/invalid total."""
    out = []
    for r in rows:
        used, total = r.get("ram_used_bytes", ""), r.get("ram_total_bytes", "")
        if not used or not total:
            continue
        try:
            used_f, total_f = float(used), float(total)
        except ValueError:
            continue
        if total_f > 0:
            out.append(used_f / total_f * 100)
    return out


def aggregate_step(run_dir: Path, users: int) -> dict[str, object]:
    system_rows, system_fields = read_system_csv(run_dir / f"u{users}_system.csv")
    stats_row = read_stats_aggregated_row(run_dir / f"u{users}_stats.csv")

    row: dict[str, object] = {"users": users, "system_samples_n": len(system_rows)}

    for dimension, cols in SYSTEM_COLUMNS.items():
        for raw_col, aggs in cols.items():
            if raw_col not in system_fields:
                continue
            values = numeric_column(system_rows, raw_col)
            if not values:
                continue  # column present but empty for every sample -> drop, don't fabricate
            for agg in aggs:
                key = f"{dimension}_{raw_col}_{agg}"
                if agg == "mean":
                    row[key] = round(statistics.mean(values), 3)
                elif agg == "max":
                    row[key] = round(max(values), 3)
                elif agg == "p95":
                    row[key] = round(percentile(values, 95), 3)

            if raw_col == "ram_used_bytes" and "ram_total_bytes" in system_fields:
                ram_pct_values = ram_pct_column(system_rows)
                if ram_pct_values:
                    row[f"{dimension}_ram_pct_mean"] = round(statistics.mean(ram_pct_values), 3)
                    row[f"{dimension}_ram_pct_max"] = round(max(ram_pct_values), 3)
                    row[f"{dimension}_ram_pct_p95"] = round(percentile(ram_pct_values, 95), 3)

    if stats_row is not None:
        req_count = float(stats_row.get("Request Count", 0) or 0)
        fail_count = float(stats_row.get("Failure Count", 0) or 0)
        for raw_col, out_name in STATS_AGGREGATED_COLUMNS["functions"].items():
            v = stats_row.get(raw_col, "")
            if v is None or v.strip() == "":
                continue
            row[f"functions_{out_name}"] = float(v)
        row["functions_failure_rate"] = round(fail_count / req_count, 6) if req_count else 0.0

    return row


def _prune_all_empty(rows: list[dict[str, object]]) -> list[str]:
    """Return the union of keys across all rows, in a stable, grouped order."""
    seen: dict[str, None] = {}
    for row in rows:
        for k in row:
            seen[k] = None
    return list(seen.keys())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=Path, help="results/<config>/<run_id>/ directory")
    ap.add_argument("--out", type=Path, required=True, help="output CSV path")
    args = ap.parse_args()

    run_dir: Path = args.run_dir
    if not run_dir.is_dir():
        sys.exit(f"not a directory: {run_dir}")

    steps = discover_steps(run_dir)
    if not steps:
        sys.exit(f"no u<N>_system.csv files found in {run_dir}")

    rows = [aggregate_step(run_dir, users) for users in steps]
    fieldnames = _prune_all_empty(rows)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} rows x {len(fieldnames)} cols -> {args.out}", file=sys.stderr)
    print(f"source: {run_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
