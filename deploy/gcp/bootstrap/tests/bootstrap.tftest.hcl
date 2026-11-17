# Runs against a mocked Google provider: no project, no credentials. Plans only: the
# bucket and key refuse to be destroyed (prevent_destroy), which an applied test would try.

mock_provider "google" {
  mock_data "google_storage_project_service_account" {
    defaults = { email_address = "service-123456789012@gs-project-accounts.iam.gserviceaccount.com" }
  }
}

variables {
  project_id = "iki-test-project"
  location   = "europe"
}

run "the_state_is_kept_private_versioned_encrypted_and_in_two_regions" {
  command = plan

  assert {
    condition     = one(google_storage_bucket.state.versioning).enabled
    error_message = "Every version of the state should be kept, to go back to."
  }
  assert {
    condition     = google_storage_bucket.state.location == "EU" && google_kms_key_ring.state.location == "europe"
    error_message = "The state and its key should be multi-regional, so they outlive a region."
  }
  assert {
    condition     = google_storage_bucket.state.public_access_prevention == "enforced" && google_storage_bucket.state.uniform_bucket_level_access
    error_message = "Nothing about the state should be public."
  }
  assert {
    condition     = google_kms_crypto_key.state.rotation_period == "7776000s"
    error_message = "The state's key should rotate every 90 days."
  }
}

run "every_api_production_uses_is_on_and_stays_on" {
  command = plan

  assert {
    condition     = alltrue([for s in ["container.googleapis.com", "sqladmin.googleapis.com", "secretmanager.googleapis.com", "servicenetworking.googleapis.com"] : contains(keys(google_project_service.this), s)])
    error_message = "The APIs production uses should be turned on."
  }
  assert {
    condition     = alltrue([for s in google_project_service.this : !s.disable_on_destroy])
    error_message = "Destroying the bootstrap shouldn't turn an API off under a deployment."
  }
}

run "it_tells_production_where_its_state_goes" {
  command = plan

  assert {
    condition     = output.backend_hcl == "bucket = \"iki-production-tofu-state-iki-test-project\"\n"
    error_message = "backend.hcl should name the bucket."
  }
}
