# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0107 promises.

mock_provider "google" {}

variables {
  name         = "iki-test"
  project_id   = "iki-test-project"
  repositories = ["europe-west1-docker.pkg.dev/iki-test-project/iki-test-worker"]
  images       = ["ghcr.io/yogesh-dhande/iki-server*"]
}

run "anything_not_named_is_refused_and_logged" {
  assert {
    condition     = one(google_binary_authorization_policy.this.default_admission_rule).evaluation_mode == "ALWAYS_DENY"
    error_message = "An image the policy doesn't name should be refused."
  }
  assert {
    condition     = one(google_binary_authorization_policy.this.default_admission_rule).enforcement_mode == "ENFORCED_BLOCK_AND_AUDIT_LOG"
    error_message = "It should be refused, not only logged, and each refusal written to the audit log."
  }
}

run "gkes_own_images_still_run" {
  assert {
    condition     = google_binary_authorization_policy.this.global_policy_evaluation_mode == "ENABLE"
    error_message = "Google's global policy should admit GKE's system images, or the cluster can't run its own agents."
  }
}

run "only_the_deployments_repositories_and_the_images_named_run" {
  assert {
    condition     = output.allowed == ["europe-west1-docker.pkg.dev/iki-test-project/iki-test-worker/**", "ghcr.io/yogesh-dhande/iki-server*"]
    error_message = "Each repository, at any depth, and each image named, and nothing else."
  }
}

run "refuses_a_whole_registry" {
  command = plan
  variables {
    images = ["docker.io/**"]
  }
  expect_failures = [var.images]
}

run "refuses_a_wildcard_before_the_end" {
  command = plan
  variables {
    images = ["ghcr.io/*/tool"]
  }
  expect_failures = [var.images]
}
