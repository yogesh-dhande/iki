# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0107 promises.

mock_provider "google" {
  mock_resource "google_logging_project_bucket_config" {
    defaults = { id = "projects/iki-test-project/locations/europe-west1/buckets/iki-test" }
  }
}

variables {
  name       = "iki-test"
  project_id = "iki-test-project"
  region     = "europe-west1"
  kms_key    = "projects/iki-test-project/locations/europe-west1/keyRings/iki-test/cryptoKeys/logs"
}

run "logs_are_kept_a_year_encrypted_with_their_own_key" {
  assert {
    condition     = google_logging_project_bucket_config.this.retention_days == 365 && one(google_logging_project_bucket_config.this.cmek_settings).kms_key_name == var.kms_key
    error_message = "Logs should be kept a year, encrypted with the logs key."
  }
}

run "the_audit_control_plane_network_and_database_logs_go_there" {
  assert {
    condition = alltrue([for kind in ["cloudaudit.googleapis.com", "k8s_cluster", "k8s_control_plane_component", "vpc_flows", "firewall", "cloudsql_database"] :
    strcontains(google_logging_project_sink.this.filter, kind)])
    error_message = "The sink should route audit, control-plane, flow, firewall and database logs."
  }
  assert {
    condition     = google_logging_project_sink.this.destination == "logging.googleapis.com/projects/iki-test-project/locations/europe-west1/buckets/iki-test"
    error_message = "And route them to this bucket."
  }
}

run "reads_of_files_secrets_and_keys_are_logged" {
  assert {
    condition     = toset(keys(google_project_iam_audit_config.data_access)) == toset(["storage.googleapis.com", "secretmanager.googleapis.com", "cloudkms.googleapis.com", "cloudsql.googleapis.com"])
    error_message = "Data Access logs should be on for files, secrets, keys and the database."
  }
  assert {
    condition     = alltrue([for a in google_project_iam_audit_config.data_access : toset([for c in a.audit_log_config : c.log_type]) == toset(["ADMIN_READ", "DATA_READ", "DATA_WRITE"])])
    error_message = "Each should log reads and writes."
  }
}

run "refuses_too_short_a_retention" {
  command = plan
  variables {
    retention_days = 7
  }
  expect_failures = [var.retention_days]
}
