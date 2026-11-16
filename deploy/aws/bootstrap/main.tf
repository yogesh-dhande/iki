# Made once, before deploy/aws/production: the bucket its state lives in and the key
# OpenTofu encrypts that state with. Its own state is small and holds no secrets; keep
# it in the bucket it made (the README's last step).

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.73, < 7.0"
    }
  }
}

variable "name" {
  description = "The deployment's name, as in deploy/aws/production."
  type        = string
  default     = "iki-production"
}

variable "region" {
  description = "Where the state lives: the deployment's region."
  type        = string
}

variable "tags" {
  description = "Tags on everything made."
  type        = map(string)
  default     = {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = var.tags
  }
}

data "aws_caller_identity" "here" {}

data "aws_partition" "here" {}

locals {
  account = data.aws_caller_identity.here.account_id
  root    = "arn:${data.aws_partition.here.partition}:iam::${local.account}:root"
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
      identifiers = [local.root]
    }
  }
}

resource "aws_kms_key" "state" {
  description             = "${var.name}: OpenTofu's state"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.key.json
}

resource "aws_kms_alias" "state" {
  name          = "alias/${var.name}-tofu-state"
  target_key_id = aws_kms_key.state.key_id
}

resource "aws_s3_bucket" "state" {
  #checkov:skip=CKV_AWS_144:The state is rebuilt from code and the account; its versions are its history. Copy it to another region if your recovery plan needs it there.
  #checkov:skip=CKV2_AWS_62:Nothing listens for changes to the state.
  #checkov:skip=CKV_AWS_18:CloudTrail data events, if you want them, say who read the state; a logs bucket for one small bucket is more to run than it's worth.
  bucket = "${var.name}-tofu-state-${local.account}"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.state.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Old versions go after a year; the current one stays.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 365
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "state" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
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

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state.json
}

output "backend_hcl" {
  description = "deploy/aws/production's backend.hcl: tofu output -raw backend_hcl > ../production/backend.hcl."
  value       = "bucket = \"${aws_s3_bucket.state.bucket}\"\nregion = \"${var.region}\"\n"
}

output "state_kms_key_arn" {
  description = "deploy/aws/production's state_kms_key_arn."
  value       = aws_kms_key.state.arn
}
