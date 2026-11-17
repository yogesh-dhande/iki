# Artifact Registry for the server and worker images you build, and plugins' images
# (ADR 0107): Docker repositories whose tags can't be moved, so a tag always names what
# was validated; encrypted with a key of their own; scanned for known vulnerabilities as
# images arrive; and readable only by the cluster's nodes and the VM tier's machines.

locals {
  labels = merge(var.labels, { iki-deployment = var.name })
  # Readers are keyed by name, not member: service accounts' addresses are only known once
  # they're made, and keys must be known when planning.
  readers = { for p in setproduct(keys(var.repositories), keys(var.readers)) : "${p[0]}:${p[1]}" => { repository = p[0], member = var.readers[p[1]] } }
  writers = { for p in setproduct(keys(var.repositories), var.writers) : "${p[0]}:${p[1]}" => { repository = p[0], member = p[1] } }
}

resource "google_artifact_registry_repository" "this" {
  for_each      = var.repositories
  project       = var.project_id
  location      = var.location
  repository_id = "${var.name}-${each.key}"
  description   = "${var.name}: ${each.value}"
  format        = "DOCKER"
  kms_key_name  = var.kms_key
  labels        = local.labels

  # A tag, once pushed, names that image for good: Iki's images are pinned by digest
  # too, and a validated plugin's tag can't be moved under it.
  docker_config {
    immutable_tags = true
  }

  # Container Scanning (containerscanning.googleapis.com) scans each image as it's pushed,
  # and again as new vulnerabilities are published.
  vulnerability_scanning_config {
    enablement_config = "INHERITED"
  }

  # Nothing is cleaned up: an image a run used is part of its record.
}

resource "google_artifact_registry_repository_iam_member" "readers" {
  for_each   = local.readers
  project    = var.project_id
  location   = var.location
  repository = google_artifact_registry_repository.this[each.value.repository].name
  role       = "roles/artifactregistry.reader"
  member     = each.value.member
}

resource "google_artifact_registry_repository_iam_member" "writers" {
  for_each   = local.writers
  project    = var.project_id
  location   = var.location
  repository = google_artifact_registry_repository.this[each.value.repository].name
  role       = "roles/artifactregistry.writer"
  member     = each.value.member
}
