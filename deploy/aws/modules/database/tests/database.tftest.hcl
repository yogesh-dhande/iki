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
  name                      = "iki-test"
  vpc_id                    = "vpc-0123"
  subnet_ids                = ["subnet-a", "subnet-b", "subnet-c"]
  client_security_group_ids = ["sg-cluster"]
}

run "it_survives_losing_a_zone" {
  assert {
    condition     = aws_db_instance.this.multi_az
    error_message = "The database should be Multi-AZ."
  }
}

run "it_is_encrypted_with_a_rotated_key_of_its_own" {
  assert {
    condition     = aws_db_instance.this.storage_encrypted && aws_db_instance.this.kms_key_id == aws_kms_key.database.arn && aws_kms_key.database.enable_key_rotation
    error_message = "Storage should be encrypted with the database's own rotated key."
  }
  assert {
    condition     = aws_db_instance.this.performance_insights_kms_key_id == aws_kms_key.database.arn
    error_message = "Performance Insights holds query text: it should use the same key."
  }
}

run "nobody_outside_reaches_it" {
  assert {
    condition     = !aws_db_instance.this.publicly_accessible
    error_message = "The database should have no public address."
  }
  assert {
    condition     = length(aws_security_group.database.egress) == 0 && length(aws_security_group.database.ingress) == 0
    error_message = "The security group should have no inline rules: only the ingress rules below."
  }
  assert {
    condition = alltrue([
      for r in aws_vpc_security_group_ingress_rule.clients :
      r.from_port == 5432 && r.to_port == 5432 && r.referenced_security_group_id == "sg-cluster"
    ]) && length(aws_vpc_security_group_ingress_rule.clients) == 1
    error_message = "Only the named clients should reach it, and only on Postgres's port."
  }
}

run "connections_need_tls_and_are_logged" {
  assert {
    condition     = contains([for p in aws_db_parameter_group.this.parameter : "${p.name}=${p.value}"], "rds.force_ssl=1")
    error_message = "Every connection should use TLS."
  }
  assert {
    condition     = contains([for p in aws_db_parameter_group.this.parameter : "${p.name}=${p.value}"], "log_connections=1")
    error_message = "Connections should be logged."
  }
  assert {
    condition     = contains([for p in aws_db_parameter_group.this.parameter : "${p.name}=${p.value}"], "pgaudit.log=ddl,role")
    error_message = "pgaudit should log schema and role changes."
  }
  assert {
    condition     = toset(aws_db_instance.this.enabled_cloudwatch_logs_exports) == toset(["postgresql", "upgrade"]) && alltrue([for g in aws_cloudwatch_log_group.database : g.kms_key_id == aws_kms_key.logs.arn])
    error_message = "Postgres's logs should go to CloudWatch, encrypted."
  }
}

run "the_master_password_never_enters_the_state" {
  assert {
    condition     = aws_db_instance.this.manage_master_user_password && aws_db_instance.this.password == null
    error_message = "RDS should keep the master password in Secrets Manager itself."
  }
}

run "it_recovers_to_any_minute_of_the_last_35_days" {
  assert {
    condition     = aws_db_instance.this.backup_retention_period == 35
    error_message = "Point-in-time recovery should reach back 35 days."
  }
  assert {
    condition     = aws_db_instance.this.deletion_protection && !aws_db_instance.this.skip_final_snapshot && !aws_db_instance.this.delete_automated_backups
    error_message = "It shouldn't be deletable by accident, and destroying it should keep a final snapshot and its backups."
  }
}

run "its_backups_are_copied_to_the_dr_region" {
  assert {
    condition     = aws_db_instance_automated_backups_replication.dr.source_db_instance_arn == aws_db_instance.this.arn
    error_message = "The instance's automated backups should be replicated."
  }
  assert {
    condition     = aws_db_instance_automated_backups_replication.dr.kms_key_id == aws_kms_key.dr.arn && aws_db_instance_automated_backups_replication.dr.retention_period == 7
    error_message = "The copy should be encrypted with a key in the DR region, and kept 7 days."
  }
}

run "refuses_too_short_a_recovery_window" {
  command = plan
  variables {
    backup_retention_days = 3
  }
  expect_failures = [var.backup_retention_days]
}

run "refuses_a_single_zone" {
  command = plan
  variables {
    subnet_ids = ["subnet-a"]
  }
  expect_failures = [var.subnet_ids]
}
