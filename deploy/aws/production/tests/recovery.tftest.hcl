# Recovering from a lost region (docs/disaster-recovery.md), in a file of its own: runs in
# a file share their state, and a deployment made new can't later become a recovery.

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
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-west-1:123456789012:key/00000000-0000-0000-0000-000000000000" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/iki-test" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/iki-test" }
  }
  mock_resource "aws_eks_cluster" {
    defaults = {
      arn                   = "arn:aws:eks:eu-west-1:123456789012:cluster/iki-test"
      certificate_authority = [{ data = "LS0tLS1CRUdJTg==" }]
    }
  }
  mock_resource "aws_launch_template" {
    defaults = { id = "lt-0123456789abcdef0", latest_version = 1 }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::iki-test-files-123456789012" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:eu-west-1:123456789012:log-group:iki-test" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:iki-test/x-AbCdEf" }
  }
  mock_resource "aws_db_instance" {
    defaults = { arn = "arn:aws:rds:eu-west-1:123456789012:db:iki-test" }
  }
  # Recovering: the lost deployment's copy of the files, here.
  mock_data "aws_s3_bucket" {
    defaults = { id = "iki-lost-files-dr-123456789012", arn = "arn:aws:s3:::iki-lost-files-dr-123456789012" }
  }
  mock_data "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-west-1:123456789012:key/22222222-2222-2222-2222-222222222222" }
  }
}

mock_provider "aws" {
  alias = "dr"
  mock_data "aws_region" {
    defaults = { region = "eu-central-1" }
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
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-central-1:123456789012:key/11111111-1111-1111-1111-111111111111" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::iki-test-files-dr-123456789012" }
  }
}

variables {
  name                 = "iki-test"
  region               = "eu-west-1"
  dr_region            = "eu-central-1"
  zones                = ["eu-west-1a", "eu-west-1b", "eu-west-1c"]
  kubernetes_version   = "1.35"
  admin_principal_arns = ["arn:aws:iam::123456789012:role/platform"]
  state_kms_key_arn    = "arn:aws:kms:eu-west-1:123456789012:key/state"
  public_host          = "iki.example.com"
  certificate_arn      = "arn:aws:acm:eu-west-1:123456789012:certificate/abc"
}

# Subnets' IDs are mocked, so pin them: the chart's values are then known exactly.
override_resource {
  target = module.network.aws_subnet.public
  values = { id = "subnet-0000000000public" }
}

override_resource {
  target = module.network.aws_subnet.private
  values = { id = "subnet-000000000private" }
}

override_resource {
  target = module.storage.aws_s3_bucket.files
  values = { id = "iki-test-files-123456789012", arn = "arn:aws:s3:::iki-test-files-123456789012" }
}

run "recovering_it_restores_the_database_and_reads_the_lost_bucket_from_its_copy" {
  variables {
    recovery = {
      name                 = "iki-lost"
      database_backups_arn = "arn:aws:rds:eu-west-1:123456789012:auto-backup:ab-abcdefghijklmnop"
    }
  }
  assert {
    condition     = module.database.restored_from == "arn:aws:rds:eu-west-1:123456789012:auto-backup:ab-abcdefghijklmnop"
    error_message = "The database should be restored from the lost one's backups."
  }
  assert {
    condition     = yamldecode(output.helm_values).files.bucket == "iki-lost-files-dr-123456789012"
    error_message = "Iki's files bucket should be the lost deployment's copy, which holds every file."
  }
  assert {
    condition     = yamldecode(output.helm_values).files.bucketAliases == { "iki-lost-files-123456789012" = "iki-lost-files-dr-123456789012" }
    error_message = "The chart should be told the lost bucket's objects are in the copy."
  }
}

run "refuses_to_recover_under_the_lost_deployments_name" {
  command = plan
  variables {
    recovery = {
      name                 = "iki-test"
      database_backups_arn = "arn:aws:rds:eu-west-1:123456789012:auto-backup:ab-abcdefghijklmnop"
    }
  }
  expect_failures = [var.recovery]
}
