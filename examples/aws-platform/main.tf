resource "aws_instance" "app" {
  ami           = "ami-0000000000000000f" # placeholder -- not a real, resolvable AMI
  instance_type = "m5.large"

  root_block_device {
    volume_size = 100
    volume_type = "gp2"
  }

  tags = {
    Name = "platform-app-server"
  }
}

resource "aws_ebs_volume" "uploads" {
  availability_zone = "us-east-1a"
  size              = 500
  type              = "gp2"

  tags = {
    Name = "platform-user-uploads"
  }
}

resource "aws_rds_cluster" "primary" {
  cluster_identifier = "platform-primary"
  engine             = "aurora-mysql"
  engine_mode        = "provisioned"
  database_name      = "platform"
  master_username    = "admin"
  master_password    = var.db_password
}

resource "aws_rds_cluster_instance" "primary" {
  identifier         = "platform-primary-1"
  cluster_identifier = aws_rds_cluster.primary.id
  instance_class     = "db.r5.large"
  engine             = aws_rds_cluster.primary.engine
}

resource "aws_elasticache_replication_group" "sessions" {
  replication_group_id = "platform-sessions"
  description           = "user session cache"
  node_type             = "cache.t3.medium"
  num_cache_clusters    = 2
  engine                = "redis"
  engine_version        = "7.0"
}

resource "aws_redshift_cluster" "warehouse" {
  cluster_identifier = "platform-warehouse"
  node_type          = "dc2.large"
  number_of_nodes    = 2
  cluster_type       = "multi-node"
  database_name      = "analytics"
  master_username    = "admin"
  master_password    = var.redshift_password
}

resource "aws_opensearch_domain" "search" {
  domain_name = "platform-search"

  cluster_config {
    instance_type  = "r6g.large.search"
    instance_count = 3
  }
}

resource "aws_memorydb_cluster" "ratelimit" {
  name       = "platform-ratelimit"
  node_type  = "db.r6g.large"
  num_shards = 1
  acl_name   = "open-access"
}

resource "aws_s3_object" "nightly_backup" {
  bucket        = "platform-backups"
  key           = "nightly/latest.tar.gz"
  storage_class = "GLACIER"
  source        = "backup.tar.gz"
}

resource "aws_fsx_windows_file_system" "reports" {
  storage_capacity    = 300
  subnet_ids          = [var.subnet_id]
  throughput_capacity = 32
  active_directory_id = var.ad_id
}
