# examples/aws-platform

A richer example workspace, built specifically to exercise the resources wired up in M6.2
(`ROADMAP.md`) — one resource per newly-supported catalog, so `poliac analyse` has a literal
value to reason about for each:

| Resource | Attribute checked | Catalog |
|---|---|---|
| `aws_instance.app` | `instance_type` | `aws_instances.csv` |
| `aws_ebs_volume.uploads` | `type` | `ebs_volume_types.csv` |
| `aws_rds_cluster_instance.primary` | `instance_class` | `rds_instance_classes.csv` |
| `aws_elasticache_replication_group.sessions` | `node_type` | `elasticache_node_types.csv` |
| `aws_redshift_cluster.warehouse` | `node_type` | `redshift_node_types.csv` |
| `aws_opensearch_domain.search` | `cluster_config.instance_type` | `opensearch_instance_types.csv` |
| `aws_memorydb_cluster.ratelimit` | `node_type` | `memorydb_node_types.csv` |
| `aws_s3_object.nightly_backup` | `storage_class` | `s3_storage_classes.csv` |
| `aws_fsx_windows_file_system.reports` | `throughput_capacity` | `fsx_windows_throughput.csv` |

**`analysis.csv` is synthetic, not real deployment data** — same honesty note as `examples/aws`
and `examples/aws-ebs` (`ARCHITECTURE.md` §10). It's hand-crafted to tell one coherent story (8x
user growth over 6 months with zero infrastructure change) with each column escalating toward a
real, catalog-derived ceiling for its resource:

- `storage_uploads_iops_max` climbs toward gp2's realistic burst ceiling for a 500 GiB volume —
  the case for `gp2` → `gp3` (configurable IOPS, not just a bigger fixed one).
- `cache_sessions_cpu_credit_balance_min` drains to 0 — `cache.t3.medium` is a *burstable*
  (CPU-credit) node type; this is what running out of credits looks like, not just "usage is
  high." The fix is a non-burstable family, not a bigger `t3`.
- `ratelimit_memorydb_throughput_gbps_max` climbs toward `db.r6g.large`'s documented *baseline*
  bandwidth (0.75 Gbps) — sustained load pinned at baseline, not occasionally bursting.
- `backup_restore_requests_per_month` climbs while `backup_restore_latency_hours_mean` stays flat
  around ~3.5h — that's `GLACIER`'s designed retrieval time, constant regardless of load. The
  story here isn't "scale it up," it's "this is the wrong storage class for how it's actually
  being used" — the one resource in this file where the right recommendation is a different
  value, not a bigger one.
- Compute/Aurora/Redshift/OpenSearch/FSx all climb toward generic saturation (CPU %, disk %,
  p95 latency, throughput % of tier) to justify a same-shape "bigger" recommendation, so the
  example doesn't read as one-note.

Run it like any other example workspace:

```bash
cd examples/aws-platform
poliac inspect main.tf --metrics-csv analysis.csv
poliac analyse main.tf --dry-run   # see the exact prompt, free
poliac analyse main.tf             # the real thing
```
