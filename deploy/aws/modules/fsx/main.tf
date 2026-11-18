# An optional FSx for Lustre file system for shared reference data (ADR 0077): runs mount
# one copy instead of each making a disk. Encrypted with a key of its own, root squashed
# so a container's root is nobody on it, its logs kept, and reachable only from the
# security groups named. Iki keeps its versions under /iki; a linked bucket shows at
# /reference, read-only.

data "aws_caller_identity" "here" {}

data "aws_partition" "here" {}

locals {
  account   = data.aws_caller_identity.here.account_id
  partition = data.aws_partition.here.partition
  tags      = merge(var.tags, { "iki.ikisci.org/deployment" = var.name })
  # Lustre's own port, and the range its servers and clients talk on.
  ports = { lnet = [988, 988], data = [1018, 1023] }
}

data "aws_iam_policy_document" "key" {
  #checkov:skip=CKV_AWS_109:A key policy's "*" is the key itself; this is AWS's default statement, which hands the key's control to the account's IAM.
  #checkov:skip=CKV_AWS_111:As above: the account's IAM policies decide who may write with it.
  #checkov:skip=CKV_AWS_356:As above: in a key policy, "*" can only mean this key.
  statement {
    sid       = "TheAccountManagesIt"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account}:root"]
    }
  }
}

resource "aws_kms_key" "this" {
  description             = "${var.name}: the shared file system"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name}-fsx"
  target_key_id = aws_kms_key.this.key_id
}

resource "aws_security_group" "this" {
  name        = "${var.name}-fsx"
  description = "The shared file system: Lustre from its clients, and between its own servers"
  vpc_id      = var.vpc_id
  tags        = merge(local.tags, { Name = "${var.name}-fsx" })
}

resource "aws_vpc_security_group_ingress_rule" "clients" {
  for_each                     = { for p in setproduct(keys(var.client_security_groups), keys(local.ports)) : "${p[0]}:${p[1]}" => p }
  security_group_id            = aws_security_group.this.id
  description                  = "Lustre from what mounts it"
  ip_protocol                  = "tcp"
  from_port                    = local.ports[each.value[1]][0]
  to_port                      = local.ports[each.value[1]][1]
  referenced_security_group_id = var.client_security_groups[each.value[0]]
  tags                         = local.tags
}

# FSx's servers talk to each other through the same group.
resource "aws_vpc_security_group_ingress_rule" "itself" {
  for_each                     = local.ports
  security_group_id            = aws_security_group.this.id
  description                  = "Lustre between the file system's own servers"
  ip_protocol                  = "tcp"
  from_port                    = each.value[0]
  to_port                      = each.value[1]
  referenced_security_group_id = aws_security_group.this.id
  tags                         = local.tags
}

resource "aws_vpc_security_group_egress_rule" "itself" {
  for_each                     = local.ports
  security_group_id            = aws_security_group.this.id
  description                  = "Lustre between the file system's own servers"
  ip_protocol                  = "tcp"
  from_port                    = each.value[0]
  to_port                      = each.value[1]
  referenced_security_group_id = aws_security_group.this.id
  tags                         = local.tags
}

resource "aws_cloudwatch_log_group" "this" {
  # FSx writes only to groups named /aws/fsx/... Encrypted: its logs name files by path.
  name              = "/aws/fsx/${var.name}"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.logs.arn
  tags              = local.tags
}

data "aws_iam_policy_document" "logs_key" {
  #checkov:skip=CKV_AWS_109:A key policy's "*" is the key itself; this is AWS's default statement, which hands the key's control to the account's IAM.
  #checkov:skip=CKV_AWS_111:As above: the account's IAM policies decide who may write with it.
  #checkov:skip=CKV_AWS_356:As above: in a key policy, "*" can only mean this key.
  statement {
    sid       = "TheAccountManagesIt"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account}:root"]
    }
  }
  statement {
    sid       = "CloudWatchLogsUsesIt"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:*:${local.account}:log-group:/aws/fsx/${var.name}"]
    }
  }
}

resource "aws_kms_key" "logs" {
  description             = "${var.name}: the shared file system's logs"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.logs_key.json
  tags                    = local.tags
}

resource "aws_fsx_lustre_file_system" "this" {
  deployment_type                   = "PERSISTENT_2"
  storage_type                      = "SSD"
  storage_capacity                  = var.storage_capacity_gib
  per_unit_storage_throughput       = var.throughput_per_tib
  file_system_type_version          = "2.15"
  subnet_ids                        = [var.subnet_id]
  security_group_ids                = [aws_security_group.this.id]
  kms_key_id                        = aws_kms_key.this.arn
  data_compression_type             = "LZ4"
  automatic_backup_retention_days   = var.backup_retention_days
  copy_tags_to_backups              = true
  daily_automatic_backup_start_time = "03:00"
  weekly_maintenance_start_time     = "7:04:00"

  # Root on a client is nobody here, so only runs' user (ADR 0077) writes. The clients
  # listed are exempt, so an administrator can make /iki for runs' user.
  dynamic "root_squash_configuration" {
    for_each = var.root_squash.enabled ? [1] : []
    content {
      root_squash    = "65534:65534"
      no_squash_nids = var.root_squash.except_nids
    }
  }

  log_configuration {
    level       = "WARN_ERROR"
    destination = aws_cloudwatch_log_group.this.arn
  }

  metadata_configuration {
    mode = "AUTOMATIC"
  }

  tags = merge(local.tags, { Name = "${var.name}-fsx" })
}

# The linked bucket, read-only: new and changed objects appear, deleted ones go, and
# nothing written here goes back to it.
resource "aws_fsx_data_repository_association" "reference" {
  count                            = var.reference_bucket == null ? 0 : 1
  file_system_id                   = aws_fsx_lustre_file_system.this.id
  file_system_path                 = "/reference"
  data_repository_path             = "s3://${split(":::", var.reference_bucket.arn)[1]}/${var.reference_bucket.prefix}"
  batch_import_meta_data_on_create = true
  delete_data_in_filesystem        = false

  s3 {
    auto_import_policy {
      events = ["NEW", "CHANGED", "DELETED"]
    }
  }

  tags = local.tags
}
