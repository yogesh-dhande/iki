# The files bucket (ADR 0036, 0097): versioned, encrypted with a key of its own, private,
# TLS only, its access logged, and every version replicated to the DR region. Iki never
# overwrites what it stores, so versions are its history, not churn. Its lifecycle rule
# and CORS are Iki's own: `iki-server setup_storage` sets them, beside any others.

data "aws_region" "here" {}

data "aws_caller_identity" "here" {}

data "aws_partition" "here" {}

data "aws_region" "dr" {
  provider = aws.dr
}

locals {
  account   = data.aws_caller_identity.here.account_id
  partition = data.aws_partition.here.partition
  region    = data.aws_region.here.region
  root      = "arn:${local.partition}:iam::${local.account}:root"
  tags      = merge(var.tags, { "iki.ikisci.org/deployment" = var.name })
  files     = "${var.name}-files-${local.account}"
  logs      = "${var.name}-s3-logs-${local.account}"
  dr        = "${var.name}-files-dr-${local.account}"
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

resource "aws_kms_key" "files" {
  description             = "${var.name}: files"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.account_key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "files" {
  name          = "alias/${var.name}-files"
  target_key_id = aws_kms_key.files.key_id
}

# --- The files bucket --------------------------------------------------------------------

resource "aws_s3_bucket" "files" {
  #checkov:skip=CKV2_AWS_62:Iki learns of uploads through its own API, not S3 events.
  #checkov:skip=CKV2_AWS_61:`iki-server setup_storage` owns the lifecycle rule (abandoned uploads) and CORS, merged with any others; OpenTofu would overwrite them. Nothing else expires here: Iki expires artifacts and bundles itself, and records it.
  bucket = local.files
  tags   = local.tags
}

resource "aws_s3_bucket_versioning" "files" {
  bucket = aws_s3_bucket.files.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "files" {
  bucket = aws_s3_bucket.files.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.files.arn
    }
    # One data key per bucket rather than per object: far fewer KMS calls.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "files" {
  bucket                  = aws_s3_bucket.files.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "files" {
  bucket = aws_s3_bucket.files.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

data "aws_iam_policy_document" "tls_only" {
  for_each = { files = aws_s3_bucket.files.arn, logs = aws_s3_bucket.logs.arn }
  statement {
    sid       = "DenyWithoutTLS"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [each.value, "${each.value}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
  statement {
    sid       = "DenyOldTLS"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [each.value, "${each.value}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }
}

resource "aws_s3_bucket_policy" "files" {
  bucket     = aws_s3_bucket.files.id
  policy     = data.aws_iam_policy_document.tls_only["files"].json
  depends_on = [aws_s3_bucket_public_access_block.files]
}

resource "aws_s3_bucket_logging" "files" {
  bucket        = aws_s3_bucket.files.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "files/"
  target_object_key_format {
    partitioned_prefix {
      partition_date_source = "EventTime"
    }
  }
}

# --- Its access logs ---------------------------------------------------------------------

resource "aws_s3_bucket" "logs" {
  #checkov:skip=CKV_AWS_18:This is where access logs go; logging its own access would loop.
  #checkov:skip=CKV_AWS_144:Access logs are kept here only; the files they describe are replicated.
  #checkov:skip=CKV2_AWS_62:Nothing acts on new log files.
  #checkov:skip=CKV_AWS_145:S3 delivers access logs only to buckets encrypted with S3-managed keys (SSE-S3).
  bucket = local.logs
  tags   = local.tags
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

data "aws_iam_policy_document" "logs" {
  source_policy_documents = [data.aws_iam_policy_document.tls_only["logs"].json]
  statement {
    sid       = "S3DeliversAccessLogs"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/files/*"]
    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.files.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_s3_bucket_policy" "logs" {
  bucket     = aws_s3_bucket.logs.id
  policy     = data.aws_iam_policy_document.logs.json
  depends_on = [aws_s3_bucket_public_access_block.logs]
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = var.access_log_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# --- The copy in the DR region -----------------------------------------------------------

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
      identifiers = [local.root]
    }
  }
}

resource "aws_kms_key" "dr" {
  provider                = aws.dr
  description             = "${var.name}: files replicated from ${local.region}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.dr_key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "dr" {
  provider      = aws.dr
  name          = "alias/${var.name}-files-dr"
  target_key_id = aws_kms_key.dr.key_id
}

resource "aws_s3_bucket" "dr" {
  #checkov:skip=CKV_AWS_18:Only replication writes here; reading it, to recover, is an AWS API call CloudTrail records.
  #checkov:skip=CKV_AWS_144:This is the replica.
  #checkov:skip=CKV2_AWS_62:Nothing acts on replicated files.
  #checkov:skip=CKV2_AWS_61:Replicas are kept as long as the files they copy; Iki's own expiry removes the originals.
  provider = aws.dr
  bucket   = local.dr
  tags     = local.tags
}

resource "aws_s3_bucket_versioning" "dr" {
  provider = aws.dr
  bucket   = aws_s3_bucket.dr.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "dr" {
  provider = aws.dr
  bucket   = aws_s3_bucket.dr.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.dr.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "dr" {
  provider                = aws.dr
  bucket                  = aws_s3_bucket.dr.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "dr" {
  provider = aws.dr
  bucket   = aws_s3_bucket.dr.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

data "aws_iam_policy_document" "dr_tls_only" {
  provider = aws.dr
  statement {
    sid       = "DenyWithoutTLS"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.dr.arn, "${aws_s3_bucket.dr.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "dr" {
  provider   = aws.dr
  bucket     = aws_s3_bucket.dr.id
  policy     = data.aws_iam_policy_document.dr_tls_only.json
  depends_on = [aws_s3_bucket_public_access_block.dr]
}

data "aws_iam_policy_document" "replication_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "replication" {
  name               = "${var.name}-files-replication"
  assume_role_policy = data.aws_iam_policy_document.replication_assume.json
  tags               = local.tags
}

# Reads versions from the files bucket and writes them to the copy, decrypting with one
# key and encrypting with the other, and nothing else.
data "aws_iam_policy_document" "replication" {
  statement {
    sid       = "ReadTheConfiguration"
    actions   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
    resources = [aws_s3_bucket.files.arn]
  }
  statement {
    sid       = "ReadVersions"
    actions   = ["s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl", "s3:GetObjectVersionTagging"]
    resources = ["${aws_s3_bucket.files.arn}/*"]
  }
  statement {
    sid       = "WriteThem"
    actions   = ["s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags"]
    resources = ["${aws_s3_bucket.dr.arn}/*"]
  }
  statement {
    sid       = "Decrypt"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.files.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${local.region}.amazonaws.com"]
    }
  }
  statement {
    sid       = "Encrypt"
    actions   = ["kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.dr.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${data.aws_region.dr.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "replication" {
  name   = "replicate-files"
  role   = aws_iam_role.replication.id
  policy = data.aws_iam_policy_document.replication.json
}

resource "aws_s3_bucket_replication_configuration" "files" {
  bucket = aws_s3_bucket.files.id
  role   = aws_iam_role.replication.arn

  rule {
    id       = "everything-to-${data.aws_region.dr.region}"
    status   = "Enabled"
    priority = 1
    filter {}

    delete_marker_replication {
      status = "Enabled"
    }

    source_selection_criteria {
      sse_kms_encrypted_objects {
        status = "Enabled"
      }
    }

    destination {
      bucket        = aws_s3_bucket.dr.arn
      storage_class = var.dr_storage_class
      encryption_configuration {
        replica_kms_key_id = aws_kms_key.dr.arn
      }

      dynamic "replication_time" {
        for_each = var.replication_time_control ? [1] : []
        content {
          status = "Enabled"
          time {
            minutes = 15
          }
        }
      }

      dynamic "metrics" {
        for_each = var.replication_time_control ? [1] : []
        content {
          status = "Enabled"
          event_threshold {
            minutes = 15
          }
        }
      }
    }
  }

  # Replication needs versioning on both sides first.
  depends_on = [aws_s3_bucket_versioning.files, aws_s3_bucket_versioning.dr]
}
