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
    defaults = { arn = "arn:aws:iam::123456789012:role/iki-test-files-replication" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::iki-test-files-123456789012" }
  }
  # Recovering: the lost deployment's copy, in the region recovering.
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
  name = "iki-test"
}

run "buckets_are_named_for_the_deployment_and_the_account" {
  assert {
    condition     = aws_s3_bucket.files[0].bucket == "iki-test-files-123456789012" && aws_s3_bucket.dr.bucket == "iki-test-files-dr-123456789012"
    error_message = "Bucket names are global: they should carry the account's ID."
  }
}

run "every_version_is_kept" {
  # Nothing expires by lifecycle here: setup_storage adds Iki's one rule, for abandoned
  # uploads, and Iki expires artifacts and bundles itself.
  assert {
    condition     = one(aws_s3_bucket_versioning.files.versioning_configuration).status == "Enabled" && one(aws_s3_bucket_versioning.dr.versioning_configuration).status == "Enabled"
    error_message = "Both buckets should keep versions: a registered file pins one (ADR 0036)."
  }
}

run "files_are_encrypted_with_keys_of_their_own" {
  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.files.rule).apply_server_side_encryption_by_default).kms_master_key_id == aws_kms_key.files[0].arn
    error_message = "Files should be encrypted with the files key."
  }
  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.dr.rule).apply_server_side_encryption_by_default).kms_master_key_id == aws_kms_key.dr.arn
    error_message = "The copies should be encrypted with a key in the DR region."
  }
}

run "nothing_is_public" {
  assert {
    condition = alltrue([
      for b in [aws_s3_bucket_public_access_block.files, aws_s3_bucket_public_access_block.logs, aws_s3_bucket_public_access_block.dr] :
      b.block_public_acls && b.block_public_policy && b.ignore_public_acls && b.restrict_public_buckets
    ])
    error_message = "Every bucket should block public access entirely."
  }
  assert {
    condition = alltrue([
      for o in [aws_s3_bucket_ownership_controls.files, aws_s3_bucket_ownership_controls.logs, aws_s3_bucket_ownership_controls.dr] :
      one(o.rule).object_ownership == "BucketOwnerEnforced"
    ])
    error_message = "ACLs should be off: the bucket's owner owns every object."
  }
}

run "every_version_is_replicated_to_the_dr_region" {
  assert {
    condition     = aws_s3_bucket_replication_configuration.files.rule[0].status == "Enabled" && aws_s3_bucket_replication_configuration.files.rule[0].destination[0].bucket == aws_s3_bucket.dr.arn
    error_message = "The files bucket should replicate to the copy."
  }
  assert {
    condition     = aws_s3_bucket_replication_configuration.files.rule[0].destination[0].encryption_configuration[0].replica_kms_key_id == aws_kms_key.dr.arn
    error_message = "Replicas should be encrypted with the DR region's key."
  }
  assert {
    condition     = aws_s3_bucket_replication_configuration.files.rule[0].source_selection_criteria[0].sse_kms_encrypted_objects[0].status == "Enabled"
    error_message = "Files encrypted with KMS, which is all of them, should replicate."
  }
  assert {
    condition     = length(aws_s3_bucket_replication_configuration.files.rule[0].destination[0].replication_time) == 0
    error_message = "Replication Time Control costs extra: off unless asked."
  }
}

run "replication_time_control_when_asked" {
  variables {
    replication_time_control = true
  }
  assert {
    condition     = aws_s3_bucket_replication_configuration.files.rule[0].destination[0].replication_time[0].status == "Enabled"
    error_message = "replication_time_control should turn on S3 Replication Time Control."
  }
}

run "access_to_files_is_logged" {
  assert {
    condition     = aws_s3_bucket_logging.files.target_bucket == aws_s3_bucket.logs.id
    error_message = "The files bucket's access should be logged to the logs bucket."
  }
  assert {
    condition     = aws_s3_bucket_lifecycle_configuration.logs.rule[0].expiration[0].days == 365
    error_message = "Access logs should be kept a year by default."
  }
}

run "refuses_a_dr_storage_class_too_slow_to_recover_from" {
  command = plan
  variables {
    dr_storage_class = "DEEP_ARCHIVE"
  }
  expect_failures = [var.dr_storage_class]
}

run "recovering_it_takes_on_the_lost_deployments_copy_as_its_files_bucket" {
  variables {
    adopt = { bucket = "iki-lost-files-dr-123456789012", kms_key = "alias/iki-lost-files-dr" }
  }
  assert {
    condition     = length(aws_s3_bucket.files) == 0 && length(aws_kms_key.files) == 0
    error_message = "Recovering, it shouldn't make a files bucket or key: the copy already holds every file."
  }
  assert {
    condition     = output.bucket == "iki-lost-files-dr-123456789012" && output.bucket_arn == "arn:aws:s3:::iki-lost-files-dr-123456789012" && output.kms_key_arn == data.aws_kms_key.adopted[0].arn
    error_message = "Iki and the pods' roles should be given the copy and its key."
  }
  assert {
    condition = alltrue([
      for b in [aws_s3_bucket_versioning.files.bucket, aws_s3_bucket_server_side_encryption_configuration.files.bucket, aws_s3_bucket_public_access_block.files.bucket, aws_s3_bucket_ownership_controls.files.bucket, aws_s3_bucket_policy.files.bucket, aws_s3_bucket_logging.files.bucket, aws_s3_bucket_replication_configuration.files.bucket] :
      b == "iki-lost-files-dr-123456789012"
    ])
    error_message = "The copy should get every setting a files bucket has: versions, encryption, TLS only, no public access, logging, and its own copy elsewhere."
  }
  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.files.rule).apply_server_side_encryption_by_default).kms_master_key_id == data.aws_kms_key.adopted[0].arn
    error_message = "New files should be encrypted with the key the copy's files already are."
  }
  assert {
    condition     = aws_s3_bucket_replication_configuration.files.rule[0].destination[0].bucket == aws_s3_bucket.dr.arn
    error_message = "The recovered deployment's files should be copied to its own DR region."
  }
}
