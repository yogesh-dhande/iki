# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0107 promises.

mock_provider "google" {}

variables {
  name       = "iki-test"
  project_id = "iki-test-project"
  region     = "europe-west1"
  dr_region  = "europe-west4"
  kms_key    = "projects/iki-test-project/locations/europe-west1/keyRings/iki-test/cryptoKeys/secrets"
  dr_kms_key = "projects/iki-test-project/locations/europe-west4/keyRings/iki-test-dr/cryptoKeys/secrets-dr"
}

run "one_entry_for_each_secret_the_chart_and_runbook_need" {
  assert {
    condition     = toset(keys(google_secret_manager_secret.this)) == toset(["secret-key", "encryption-keys", "bundle-signing-key", "database", "database-migrator", "database-admin", "run-secrets"])
    error_message = "Each Secret the chart requires, and the database's admin password should have an entry, and nothing more by default."
  }
  assert {
    condition     = output.secret_names["database"] == "iki-test-database"
    error_message = "Secrets should be named <deployment>-<secret>, so a policy can scope to one deployment's."
  }
}

run "optional_secrets_when_asked" {
  variables {
    optional_secrets = ["email"]
  }
  assert {
    condition     = contains(keys(google_secret_manager_secret.this), "email") && !contains(keys(google_secret_manager_secret.this), "backend-worker-token")
    error_message = "Only the optional secrets asked for should be made."
  }
}

run "kept_in_both_regions_each_under_its_own_key" {
  assert {
    condition = alltrue([for s in google_secret_manager_secret.this :
      { for r in one(one(s.replication).user_managed).replicas : r.location => one(r.customer_managed_encryption).kms_key_name } == { "europe-west1" = var.kms_key, "europe-west4" = var.dr_kms_key }
    ])
    error_message = "Each secret should be kept in the region and the DR region, each copy under its region's key."
  }
  assert {
    condition     = alltrue([for s in google_secret_manager_secret.this : s.deletion_protection && s.version_destroy_ttl == "2592000s"])
    error_message = "Secrets shouldn't be deletable by accident, and a destroyed version should be restorable for 30 days."
  }
}

run "deletable_only_when_told_to_tear_down" {
  variables {
    deletion_protection = false
  }
  assert {
    condition     = alltrue([for s in google_secret_manager_secret.this : !s.deletion_protection])
    error_message = "With deletion_protection off, the secrets should be deletable."
  }
}

run "refuses_an_unknown_optional_secret" {
  command = plan
  variables {
    optional_secrets = ["ldap"]
  }
  expect_failures = [var.optional_secrets]
}
