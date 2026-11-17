# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0107 promises.

mock_provider "google" {}

variables {
  name           = "iki-test"
  project_id     = "iki-test-project"
  location       = "EU"
  data_locations = ["europe-west1", "europe-west4"]
  kms_key        = "projects/iki-test-project/locations/europe/keyRings/iki-test-files/cryptoKeys/files"
  cors_origins   = ["https://iki.example.com"]
}

run "the_bucket_is_named_for_the_deployment_and_the_project" {
  assert {
    condition     = google_storage_bucket.files.name == "iki-test-files-iki-test-project"
    error_message = "Bucket names are global: it should carry the project's ID."
  }
}

run "every_file_is_in_both_regions" {
  assert {
    condition     = one(google_storage_bucket.files.custom_placement_config).data_locations == toset(["EUROPE-WEST1", "EUROPE-WEST4"])
    error_message = "The bucket should be a dual-region over the region and the DR region."
  }
  assert {
    condition     = google_storage_bucket.files.rpo == "DEFAULT"
    error_message = "Turbo replication costs extra: off unless asked."
  }
}

run "every_version_is_kept_and_deletions_are_recoverable" {
  assert {
    condition     = one(google_storage_bucket.files.versioning).enabled
    error_message = "The bucket should keep versions: a registered file pins one (ADR 0036)."
  }
  assert {
    condition     = one(google_storage_bucket.files.soft_delete_policy).retention_duration_seconds == 2592000
    error_message = "A deleted object should be restorable for 30 days."
  }
}

run "files_are_encrypted_with_their_key_and_no_other" {
  assert {
    condition     = one(google_storage_bucket.files.encryption).default_kms_key_name == var.kms_key
    error_message = "Files should be encrypted with the files key."
  }
  assert {
    condition = (
      one(one(google_storage_bucket.files.encryption).google_managed_encryption_enforcement_config).restriction_mode == "FullyRestricted"
      && one(one(google_storage_bucket.files.encryption).customer_supplied_encryption_enforcement_config).restriction_mode == "FullyRestricted"
    )
    error_message = "Objects encrypted with Google's keys or a caller's own should be refused."
  }
}

run "nothing_is_public" {
  assert {
    condition     = google_storage_bucket.files.public_access_prevention == "enforced" && google_storage_bucket.files.uniform_bucket_level_access
    error_message = "The bucket should refuse public access, and have no per-object ACLs."
  }
}

run "abandoned_uploads_go_and_browsers_upload_from_iki_only" {
  variables {
    prefix = "prod/"
  }
  assert {
    condition     = toset([for r in google_storage_bucket.files.lifecycle_rule : one(r.action).type]) == toset(["AbortIncompleteMultipartUpload", "Delete"])
    error_message = "Abandoned uploads and old staging objects should go."
  }
  assert {
    condition     = alltrue([for r in google_storage_bucket.files.lifecycle_rule : one(r.condition).matches_prefix == tolist(["prod/uploads/"])])
    error_message = "Only under Iki's staging prefix: nothing else expires by lifecycle."
  }
  assert {
    condition     = one(google_storage_bucket.files.cors).origin == tolist(["https://iki.example.com"]) && toset(one(google_storage_bucket.files.cors).method) == toset(["GET", "PUT"])
    error_message = "Browsers should reach the bucket only from Iki's URL, to GET and PUT."
  }
}

run "turbo_replication_when_asked" {
  variables {
    turbo_replication = true
  }
  assert {
    condition     = google_storage_bucket.files.rpo == "ASYNC_TURBO"
    error_message = "turbo_replication should turn on turbo replication."
  }
}

run "recovering_it_takes_on_the_lost_deployments_bucket" {
  variables {
    name  = "iki-test-r1"
    adopt = { bucket = "iki-test-files-iki-test-project" }
  }
  assert {
    condition     = output.bucket == "iki-test-files-iki-test-project"
    error_message = "The recovered deployment's files bucket should be the lost one's, which still holds every file."
  }
}

run "refuses_a_single_region_custom_placement" {
  command = plan
  variables {
    data_locations = ["europe-west1", "europe-west1"]
  }
  expect_failures = [var.data_locations]
}
