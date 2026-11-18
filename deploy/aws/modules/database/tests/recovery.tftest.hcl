# Recovering from a lost region (docs/disaster-recovery.md), in a file of its own: runs in
# a file share their state, and a database made new can't later become a restore.

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
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/iki-test-rds-monitoring" }
  }
  mock_resource "aws_db_instance" {
    defaults = { arn = "arn:aws:rds:eu-west-1:123456789012:db:iki-test" }
  }
}

mock_provider "aws" {
  alias = "dr"
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-central-1:123456789012:key/11111111-1111-1111-1111-111111111111" }
  }
}

variables {
  name                   = "iki-test"
  vpc_id                 = "vpc-0123"
  subnet_ids             = ["subnet-a", "subnet-b", "subnet-c"]
  client_security_groups = { cluster = "sg-cluster" }
}

run "recovering_to_a_moment" {
  command = plan
  variables {
    restore_from = { automated_backups_arn = "arn:aws:rds:eu-west-1:123456789012:auto-backup:ab-abcdefghijklmnop", restore_time = "2026-11-16T09:41:00Z" }
  }
  assert {
    condition     = one(aws_db_instance.this.restore_to_point_in_time).restore_time == "2026-11-16T09:41:00Z" && one(aws_db_instance.this.restore_to_point_in_time).use_latest_restorable_time == null
    error_message = "With a time, to that time."
  }
}

run "recovering_it_restores_the_lost_database_from_its_copied_backups" {
  variables {
    restore_from = { automated_backups_arn = "arn:aws:rds:eu-west-1:123456789012:auto-backup:ab-abcdefghijklmnop" }
  }
  assert {
    condition     = one(aws_db_instance.this.restore_to_point_in_time).source_db_instance_automated_backups_arn == "arn:aws:rds:eu-west-1:123456789012:auto-backup:ab-abcdefghijklmnop"
    error_message = "It should be restored from the backups named."
  }
  assert {
    condition     = one(aws_db_instance.this.restore_to_point_in_time).use_latest_restorable_time == true
    error_message = "Without a time, to the latest the backups reach."
  }
  assert {
    condition     = aws_db_instance.this.multi_az && aws_db_instance.this.storage_encrypted && aws_db_instance.this.deletion_protection && aws_db_instance.this.backup_retention_period == 35
    error_message = "A restored database should be as protected as a new one."
  }
  assert {
    condition     = aws_db_instance_automated_backups_replication.dr.source_db_instance_arn == aws_db_instance.this.arn
    error_message = "And its own backups copied to its own DR region."
  }
}

run "refuses_to_restore_from_something_not_automated_backups" {
  command = plan
  variables {
    restore_from = { automated_backups_arn = "arn:aws:rds:eu-west-1:123456789012:snapshot:nightly" }
  }
  expect_failures = [var.restore_from]
}

run "refuses_a_time_that_isnt_one" {
  command = plan
  variables {
    restore_from = { automated_backups_arn = "arn:aws:rds:eu-west-1:123456789012:auto-backup:ab-abcdefghijklmnop", restore_time = "yesterday" }
  }
  expect_failures = [var.restore_from]
}

run "a_later_apply_without_restore_from_keeps_the_restored_database" {
  # The restore is how it began: dropping restore_from mustn't replace it.
  variables {
    restore_from = null
  }
  assert {
    condition     = one(aws_db_instance.this.restore_to_point_in_time).source_db_instance_automated_backups_arn == "arn:aws:rds:eu-west-1:123456789012:auto-backup:ab-abcdefghijklmnop"
    error_message = "The database should be the one restored, not one made new."
  }
}
