# RDS PostgreSQL (ADR 0097): Multi-AZ, encrypted with a key of its own, reachable only
# from the cluster, logging connections and DDL, with 35 days of point-in-time recovery
# here and its backups copied to the DR region.

data "aws_region" "here" {}

data "aws_caller_identity" "here" {}

data "aws_partition" "here" {}

locals {
  account   = data.aws_caller_identity.here.account_id
  partition = data.aws_partition.here.partition
  region    = data.aws_region.here.region
  root      = "arn:${local.partition}:iam::${local.account}:root"
  tags      = merge(var.tags, { "iki.ikisci.org/deployment" = var.name })
  major     = split(".", var.engine_version)[0]
  log_types = ["postgresql", "upgrade"]
}

data "aws_iam_policy_document" "account_key" {
  #checkov:skip=CKV_AWS_109:A key policy's "*" is the key itself; this is AWS's default statement, which hands the key's control to the account's IAM.
  #checkov:skip=CKV_AWS_111:As above: the account's IAM policies decide who may write with it.
  #checkov:skip=CKV_AWS_356:As above: in a key policy, "*" can only mean this key.
  statement {
    sid       = "TheAccountManagesIt"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [local.root]
    }
  }
}

# Data, snapshots, Performance Insights and the master password's secret.
resource "aws_kms_key" "database" {
  description             = "${var.name}: the database"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.account_key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "database" {
  name          = "alias/${var.name}-database"
  target_key_id = aws_kms_key.database.key_id
}

resource "aws_kms_key" "logs" {
  description             = "${var.name}: database logs"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.logs_key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${var.name}-database-logs"
  target_key_id = aws_kms_key.logs.key_id
}

data "aws_iam_policy_document" "logs_key" {
  #checkov:skip=CKV_AWS_109:A key policy's "*" is the key itself; this is AWS's default statement, which hands the key's control to the account's IAM.
  #checkov:skip=CKV_AWS_111:As above: the account's IAM policies decide who may write with it.
  #checkov:skip=CKV_AWS_356:As above: in a key policy, "*" can only mean this key.
  source_policy_documents = [data.aws_iam_policy_document.account_key.json]
  statement {
    sid       = "CloudWatchLogsUsesIt"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${local.region}:${local.account}:log-group:/aws/rds/instance/${var.name}/*"]
    }
  }
}

# Made before the instance, so RDS writes into groups that are encrypted and expire.
resource "aws_cloudwatch_log_group" "database" {
  for_each          = toset(local.log_types)
  name              = "/aws/rds/instance/${var.name}/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs.arn
  tags              = local.tags
}

resource "aws_db_subnet_group" "this" {
  name        = var.name
  description = "${var.name}: the database subnets, with no route out of the VPC"
  subnet_ids  = var.subnet_ids
  tags        = local.tags
}

# Postgres from the cluster, and nothing else in or out.
resource "aws_security_group" "database" {
  name        = "${var.name}-database"
  description = "Postgres from the clients named, nothing else"
  vpc_id      = var.vpc_id
  tags        = merge(local.tags, { Name = "${var.name}-database" })
}

resource "aws_vpc_security_group_ingress_rule" "clients" {
  for_each                     = var.client_security_groups
  security_group_id            = aws_security_group.database.id
  description                  = "Postgres from ${each.key}"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_db_parameter_group" "this" {
  name        = "${var.name}-postgres${local.major}"
  family      = "postgres${local.major}"
  description = "${var.name}: TLS only, connections and DDL logged"

  # TLS 1.2 or later for every connection.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "ssl_min_protocol_version"
    value = "TLSv1.2"
  }
  # Who connected, from where, and for how long.
  parameter {
    name  = "log_connections"
    value = "1"
  }
  parameter {
    name  = "log_disconnections"
    value = "1"
  }
  # Every change to the schema or to roles, through pgaudit: the database's own audit
  # trail, beside Iki's (ADR 0025).
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements,pgaudit"
    apply_method = "pending-reboot"
  }
  parameter {
    name  = "pgaudit.log"
    value = "ddl,role"
  }
  parameter {
    name  = "pgaudit.log_parameter"
    value = "0"
  }
  parameter {
    name  = "log_statement"
    value = "ddl"
  }

  tags = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

data "aws_iam_policy_document" "monitoring_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "monitoring" {
  name               = "${var.name}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.monitoring_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "monitoring" {
  role       = aws_iam_role.monitoring.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class
  # A restore brings the database, its roles and its master user with it.
  db_name = var.restore_from == null ? "iki" : null

  # The master user only creates Iki's two roles (ADR 0025): its password lives in
  # Secrets Manager, where RDS rotates it, and never in OpenTofu's state.
  username                      = var.restore_from == null ? "iki_admin" : null
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.database.arn

  storage_type          = "gp3"
  allocated_storage     = var.allocated_storage_gib
  max_allocated_storage = var.max_allocated_storage_gib
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.database.arn

  multi_az               = true
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false
  network_type           = "IPV4"
  port                   = 5432
  parameter_group_name   = aws_db_parameter_group.this.name
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  # IAM authentication on, so people can sign in with short-lived tokens rather than
  # shared passwords when they must; Iki itself uses its roles' passwords.
  iam_database_authentication_enabled = true

  backup_retention_period   = var.backup_retention_days
  backup_window             = var.backup_window
  maintenance_window        = var.maintenance_window
  copy_tags_to_snapshot     = true
  delete_automated_backups  = false
  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.name}-final"

  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false
  apply_immediately           = false

  enabled_cloudwatch_logs_exports       = local.log_types
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.database.arn
  performance_insights_retention_period = 7
  monitoring_interval                   = 30
  monitoring_role_arn                   = aws_iam_role.monitoring.arn

  # Recovering from a lost region: the lost database, from its backups copied here.
  dynamic "restore_to_point_in_time" {
    for_each = var.restore_from == null ? [] : [var.restore_from]
    content {
      source_db_instance_automated_backups_arn = restore_to_point_in_time.value.automated_backups_arn
      restore_time                             = restore_to_point_in_time.value.restore_time
      use_latest_restorable_time               = restore_to_point_in_time.value.restore_time == null ? true : null
    }
  }

  tags       = local.tags
  depends_on = [aws_cloudwatch_log_group.database, aws_iam_role_policy_attachment.monitoring]

  lifecycle {
    # How the database began, which a later apply mustn't redo: replacing it would
    # lose it (and deletion protection refuses).
    ignore_changes = [restore_to_point_in_time, db_name, username]
  }
}

# --- The DR region -----------------------------------------------------------------------

data "aws_caller_identity" "dr" {
  provider = aws.dr
}

data "aws_iam_policy_document" "dr_key" {
  #checkov:skip=CKV_AWS_109:A key policy's "*" is the key itself; this is AWS's default statement, which hands the key's control to the account's IAM.
  #checkov:skip=CKV_AWS_111:As above: the account's IAM policies decide who may write with it.
  #checkov:skip=CKV_AWS_356:As above: in a key policy, "*" can only mean this key.
  provider = aws.dr
  statement {
    sid       = "TheAccountManagesIt"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${data.aws_caller_identity.dr.account_id}:root"]
    }
  }
}

resource "aws_kms_key" "dr" {
  provider                = aws.dr
  description             = "${var.name}: database backups copied from ${local.region}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.dr_key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "dr" {
  provider      = aws.dr
  name          = "alias/${var.name}-database-dr"
  target_key_id = aws_kms_key.dr.key_id
}

# Snapshots and transaction logs, copied as they're made: the database can be restored
# in the DR region to any point in the last dr_backup_retention_days, minutes behind.
resource "aws_db_instance_automated_backups_replication" "dr" {
  provider               = aws.dr
  source_db_instance_arn = aws_db_instance.this.arn
  kms_key_id             = aws_kms_key.dr.arn
  retention_period       = var.dr_backup_retention_days
}
