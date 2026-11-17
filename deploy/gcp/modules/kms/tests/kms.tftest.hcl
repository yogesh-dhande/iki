# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0107 promises.

mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "123456789012" }
  }
  mock_data "google_storage_project_service_account" {
    defaults = { email_address = "service-123456789012@gs-project-accounts.iam.gserviceaccount.com" }
  }
  mock_data "google_logging_project_cmek_settings" {
    defaults = { service_account_id = "cmek-p123456789012@gcp-sa-logging.iam.gserviceaccount.com" }
  }
}

variables {
  name           = "iki-test"
  project_id     = "iki-test-project"
  region         = "europe-west1"
  dr_region      = "europe-west4"
  files_location = "europe"
}

run "each_purpose_has_its_own_rotated_key" {
  assert {
    condition     = length(google_kms_crypto_key.this) == 11 && alltrue([for k in google_kms_crypto_key.this : k.rotation_period == "7776000s"])
    error_message = "Every key should rotate every 90 days (CIS Google Cloud 1.10)."
  }
  assert {
    condition     = alltrue([for k in google_kms_crypto_key.this : k.deletion_policy == "PREVENT"])
    error_message = "OpenTofu should never destroy a key: what it encrypted would be lost."
  }
}

run "keys_live_where_what_they_encrypt_does" {
  assert {
    condition     = google_kms_key_ring.this["region"].location == "europe-west1" && google_kms_key_ring.this["dr"].location == "europe-west4" && google_kms_key_ring.this["files"].location == "europe"
    error_message = "Key rings should be in the region, the DR region and the files bucket's location."
  }
  assert {
    condition     = google_kms_crypto_key.this["database-dr"].key_ring == google_kms_key_ring.this["dr"].id && google_kms_crypto_key.this["files"].key_ring == google_kms_key_ring.this["files"].id
    error_message = "The replica's key should be in the DR region, and the files key with the bucket."
  }
}

run "only_the_service_that_encrypts_with_a_key_may_use_it" {
  assert {
    condition     = google_kms_crypto_key_iam_member.agents["database"].member == "serviceAccount:service-123456789012@gcp-sa-cloud-sql.iam.gserviceaccount.com"
    error_message = "Cloud SQL's service agent should use the database's key."
  }
  assert {
    condition     = google_kms_crypto_key_iam_member.agents["files"].member == "serviceAccount:service-123456789012@gs-project-accounts.iam.gserviceaccount.com"
    error_message = "Cloud Storage's service agent should use the files key."
  }
  assert {
    condition     = alltrue([for m in google_kms_crypto_key_iam_member.agents : m.role == "roles/cloudkms.cryptoKeyEncrypterDecrypter"])
    error_message = "Agents should only encrypt and decrypt: never manage the keys."
  }
}

run "hsm_when_asked" {
  variables {
    protection_level = "HSM"
  }
  assert {
    condition     = alltrue([for k in google_kms_crypto_key.this : one(k.version_template).protection_level == "HSM"])
    error_message = "protection_level should put every key in Cloud HSM."
  }
}

run "refuses_the_same_region_for_dr" {
  command = plan
  variables {
    dr_region = "europe-west1"
  }
  expect_failures = [var.dr_region]
}
