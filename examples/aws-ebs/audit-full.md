# Audit objective

Perform a full architecture audit of this deployment. Evaluate every resource currently in
scope and recommend a change wherever the metrics justify one:

- **Compute** (`aws_instance`): is `instance_type` appropriately sized for the observed load?
- **Storage** (`aws_instance.root_block_device`): is `volume_type` (and `iops` / `throughput`,
  where relevant) appropriate for the observed disk read/write pattern, or would a different
  volume type serve it better?
- **Database** (`aws_db_instance`): is `instance_class` and `allocated_storage` appropriately
  sized for the observed connection count, query rate, and slow-query activity?
