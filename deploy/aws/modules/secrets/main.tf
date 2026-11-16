# Secrets Manager entries for Iki's secrets (ADR 0097), encrypted with a key of their
# own and replicated to the DR region. OpenTofu makes the entries, never their values:
# scripts/put-secrets.sh generates those and writes them straight to Secrets Manager, so
# they never sit in OpenTofu's state. External Secrets copies them into the cluster.

data "aws_caller_identity" "here" {}

data "aws_partition" "here" {}

data "aws_region" "here" {}

data "aws_region" "dr" {
  provider = aws.dr
}

locals {
  account   = data.aws_caller_identity.here.account_id
  partition = data.aws_partition.here.partition
  root      = "arn:${local.partition}:iam::${local.account}:root"
  tags      = merge(var.tags, { "iki.ikisci.org/deployment" = var.name })
  # What each holds, and the chart's value that names it.
  required = {
    "secret-key"         = "Django's SECRET_KEY (secretKey)"
    "encryption-keys"    = "Fernet keys that encrypt secrets Iki stores, the first one encrypting (encryptionKeys)"
    "bundle-signing-key" = "The Ed25519 seed this deployment signs bundles with (bundles.signingKey)"
    "database"           = "A postgres:// URI for the app's role, iki_app (database)"
    "database-migrator"  = "A postgres:// URI for the schema's owner, iki (database.migrator)"
    "run-secrets"        = "Plugins' secrets, as a JSON object keyed like dna.NCBI_API_KEY (runSecrets)"
  }
  optional = {
    "email"                = "The mail server's URL, with its credentials (email)"
    "backend-worker-token" = "A backend-tier registration token (backendWorkers.registrationToken)"
  }
  secrets = merge(local.required, { for k, v in local.optional : k => v if contains(var.optional_secrets, k) })
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

resource "aws_kms_key" "secrets" {
  description             = "${var.name}: Iki's secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.account_key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/${var.name}-secrets"
  target_key_id = aws_kms_key.secrets.key_id
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
      identifiers = [local.root]
    }
  }
}

resource "aws_kms_key" "dr" {
  provider                = aws.dr
  description             = "${var.name}: Iki's secrets, replicated from ${data.aws_region.here.region}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.dr_key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "dr" {
  provider      = aws.dr
  name          = "alias/${var.name}-secrets-dr"
  target_key_id = aws_kms_key.dr.key_id
}

resource "aws_secretsmanager_secret" "this" {
  #checkov:skip=CKV2_AWS_57:Iki's secrets rotate by procedure, not by a Lambda: each needs Iki's own steps (ADR 0097's runbook), such as re-encrypting with a new Fernet key.
  for_each                = local.secrets
  name                    = "${var.name}/${each.key}"
  description             = each.value
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = var.recovery_window_days

  replica {
    region     = data.aws_region.dr.region
    kms_key_id = aws_kms_key.dr.arn
  }

  tags = local.tags
}
