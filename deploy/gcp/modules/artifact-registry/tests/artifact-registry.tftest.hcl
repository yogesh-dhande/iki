# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0107 promises.

mock_provider "google" {}

variables {
  name       = "iki-test"
  project_id = "iki-test-project"
  location   = "europe-west1"
  kms_key    = "projects/iki-test-project/locations/europe-west1/keyRings/iki-test/cryptoKeys/registry"
  readers    = { nodes = "serviceAccount:iki-test-nodes@iki-test-project.iam.gserviceaccount.com" }
  writers    = ["serviceAccount:ci@iki-test-project.iam.gserviceaccount.com"]
}

run "images_are_encrypted_scanned_and_their_tags_fixed" {
  assert {
    condition     = alltrue([for r in google_artifact_registry_repository.this : r.kms_key_name == var.kms_key && one(r.docker_config).immutable_tags])
    error_message = "Repositories should be encrypted with their key, and a tag never moved."
  }
  assert {
    condition     = alltrue([for r in google_artifact_registry_repository.this : one(r.vulnerability_scanning_config).enablement_config == "INHERITED" && length(r.cleanup_policies) == 0])
    error_message = "Images should be scanned, and none cleaned up: a run's image is part of its record."
  }
  assert {
    condition     = output.repositories["worker"] == "europe-west1-docker.pkg.dev/iki-test-project/iki-test-worker"
    error_message = "The worker's repository should be named for the deployment."
  }
}

run "only_the_members_named_pull_or_push" {
  assert {
    condition     = toset([for m in google_artifact_registry_repository_iam_member.readers : "${m.role} ${m.member}"]) == toset(["roles/artifactregistry.reader serviceAccount:iki-test-nodes@iki-test-project.iam.gserviceaccount.com"])
    error_message = "Only the readers named should pull."
  }
  assert {
    condition     = length(google_artifact_registry_repository_iam_member.writers) == 3 && alltrue([for m in google_artifact_registry_repository_iam_member.writers : m.role == "roles/artifactregistry.writer"])
    error_message = "CI should push to each repository, and nothing more."
  }
}

run "refuses_a_reader_that_isnt_a_member" {
  command = plan
  variables {
    readers = { nodes = "iki-test-nodes@iki-test-project.iam.gserviceaccount.com" }
  }
  expect_failures = [var.readers]
}
