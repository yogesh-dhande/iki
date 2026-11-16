# Runs against a mocked AWS provider: no account, no credentials. Plans only: the
# bucket refuses to be destroyed (prevent_destroy), which an applied test would try.

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
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-west-1:123456789012:key/00000000-0000-0000-0000-000000000000" }
  }
}

variables {
  region = "eu-west-1"
}

run "the_state_is_kept_private_versioned_and_encrypted" {
  command = plan

  assert {
    condition     = aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled"
    error_message = "Every version of the state should be kept, to go back to."
  }
  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default[0].kms_master_key_id == aws_kms_key.state.arn
    error_message = "The bucket should encrypt with the state's own key."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.state.block_public_policy && aws_s3_bucket_public_access_block.state.restrict_public_buckets
    error_message = "Nothing about the state should be public."
  }
  assert {
    condition     = one([for s in data.aws_iam_policy_document.state.statement : s.effect if s.sid == "TlsOnly"]) == "Deny"
    error_message = "The bucket should refuse anything but TLS."
  }
}

run "it_tells_production_where_its_state_goes" {
  command = plan

  assert {
    condition     = output.backend_hcl == "bucket = \"iki-production-tofu-state-123456789012\"\nregion = \"eu-west-1\"\n"
    error_message = "backend.hcl should name the bucket and region."
  }
}
