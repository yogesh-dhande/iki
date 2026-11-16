# Runs against mocked AWS providers, one per region: no account, no credentials. Each
# run checks a property ADR 0097 promises.

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = { region = "eu-west-1" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  # The provider checks ARNs it's given, so mocked ones must look real.
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-west-1:123456789012:key/00000000-0000-0000-0000-000000000000" }
  }
}

mock_provider "aws" {
  alias = "dr"
  mock_data "aws_region" {
    defaults = { region = "eu-central-1" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-central-1:123456789012:key/11111111-1111-1111-1111-111111111111" }
  }
}

variables {
  name = "iki-test"
}

run "one_entry_for_each_secret_the_chart_needs" {
  assert {
    condition     = toset(keys(aws_secretsmanager_secret.this)) == toset(["secret-key", "encryption-keys", "bundle-signing-key", "database", "database-migrator", "run-secrets"])
    error_message = "Each Secret the chart requires should have an entry, and nothing more by default."
  }
  assert {
    condition     = output.secret_names["database"] == "iki-test/database"
    error_message = "Secrets should be named <deployment>/<secret>, so a policy can scope to one deployment's."
  }
}

run "optional_secrets_when_asked" {
  variables {
    optional_secrets = ["email"]
  }
  assert {
    condition     = contains(keys(aws_secretsmanager_secret.this), "email") && !contains(keys(aws_secretsmanager_secret.this), "backend-worker-token")
    error_message = "Only the optional secrets asked for should be made."
  }
}

run "encrypted_here_and_replicated_to_the_dr_region" {
  assert {
    condition     = alltrue([for s in aws_secretsmanager_secret.this : s.kms_key_id == aws_kms_key.secrets.arn])
    error_message = "Secrets should be encrypted with their own key."
  }
  assert {
    condition     = alltrue([for s in aws_secretsmanager_secret.this : one(s.replica).region == "eu-central-1" && one(s.replica).kms_key_id == aws_kms_key.dr.arn])
    error_message = "Each secret should be replicated to the DR region, under its key there."
  }
  assert {
    condition     = alltrue([for s in aws_secretsmanager_secret.this : s.recovery_window_in_days == 30])
    error_message = "A deleted secret should be restorable for 30 days."
  }
}

run "refuses_an_unknown_optional_secret" {
  command = plan
  variables {
    optional_secrets = ["ldap"]
  }
  expect_failures = [var.optional_secrets]
}
