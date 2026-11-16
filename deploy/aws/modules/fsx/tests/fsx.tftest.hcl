# Runs against a mocked AWS provider: no account, no credentials. Each run checks a
# property ADR 0077 and 0097 promise.

mock_provider "aws" {
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
  mock_resource "aws_fsx_lustre_file_system" {
    defaults = { id = "fs-0123456789abcdef0" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:eu-west-1:123456789012:log-group:/aws/fsx/iki-test" }
  }
}

variables {
  name                      = "iki-test"
  vpc_id                    = "vpc-0123456789abcdef0"
  subnet_id                 = "subnet-0123456789abcdef0"
  client_security_group_ids = ["sg-00000000000000node", "sg-0000000000000000vm"]
}

run "encrypted_with_its_own_key_and_logged" {
  assert {
    condition     = aws_fsx_lustre_file_system.this.kms_key_id == aws_kms_key.this.arn && aws_kms_key.this.enable_key_rotation
    error_message = "The file system should be encrypted with its own rotated key."
  }
  assert {
    condition     = aws_fsx_lustre_file_system.this.log_configuration[0].destination == aws_cloudwatch_log_group.this.arn && aws_cloudwatch_log_group.this.retention_in_days == 365
    error_message = "Its logs should be kept a year."
  }
}

run "root_is_nobody_by_default" {
  assert {
    condition     = aws_fsx_lustre_file_system.this.root_squash_configuration[0].root_squash == "65534:65534"
    error_message = "A client's root should be nobody on the file system."
  }
}

run "lustre_only_from_its_clients" {
  assert {
    condition = toset([
      for r in aws_vpc_security_group_ingress_rule.clients : "${r.referenced_security_group_id}:${r.from_port}-${r.to_port}"
      ]) == toset([
      "sg-00000000000000node:988-988", "sg-00000000000000node:1018-1023",
      "sg-0000000000000000vm:988-988", "sg-0000000000000000vm:1018-1023",
    ])
    error_message = "Only the named groups should reach it, and only on Lustre's ports."
  }
  assert {
    condition     = alltrue([for r in aws_vpc_security_group_ingress_rule.clients : r.cidr_ipv4 == null])
    error_message = "No network ranges: security groups only."
  }
}

run "no_linked_bucket_unless_asked" {
  assert {
    condition     = length(aws_fsx_data_repository_association.reference) == 0
    error_message = "Without a reference bucket, nothing is linked."
  }
}

run "a_linked_bucket_is_read_only_at_reference" {
  variables {
    reference_bucket = { arn = "arn:aws:s3:::lab-reference", prefix = "alphafold/" }
  }
  assert {
    condition     = aws_fsx_data_repository_association.reference[0].file_system_path == "/reference" && aws_fsx_data_repository_association.reference[0].data_repository_path == "s3://lab-reference/alphafold/"
    error_message = "The bucket should show at /reference, apart from Iki's /iki."
  }
  assert {
    condition     = length(aws_fsx_data_repository_association.reference[0].s3[0].auto_export_policy) == 0
    error_message = "Nothing written to the file system should go back to the bucket."
  }
}

run "refuses_a_size_fsx_doesnt_offer" {
  command = plan
  variables {
    storage_capacity_gib = 3000
  }
  expect_failures = [var.storage_capacity_gib]
}
