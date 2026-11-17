# Made once, before deploy/gcp/production: the APIs it uses, the bucket its state lives in
# and the key OpenTofu encrypts that state with. Both are multi-regional, so the state
# outlives losing a region. Its own state is small and holds no secrets; keep it in the
# bucket it made (the README's last step).

terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 8.11, < 9.0"
    }
  }
}

variable "name" {
  description = "The deployment's name, as in deploy/gcp/production."
  type        = string
  default     = "iki-production"
}

variable "project_id" {
  description = "The project the deployment goes in."
  type        = string
}

variable "location" {
  description = "Where the state and its key are: the multi-region (europe, us or asia) containing the deployment's region and its DR region."
  type        = string

  validation {
    condition     = contains(["europe", "us", "asia"], var.location)
    error_message = "Use europe, us or asia."
  }
}

variable "labels" {
  description = "Labels on everything made."
  type        = map(string)
  default     = {}
}

provider "google" {
  project        = var.project_id
  default_labels = var.labels
}

locals {
  # Every API the production root uses. Turning one off later would break what uses it,
  # so destroying this never does.
  apis = [
    "artifactregistry.googleapis.com", "cloudkms.googleapis.com", "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com", "container.googleapis.com", "containerscanning.googleapis.com",
    "dns.googleapis.com", "file.googleapis.com", "iam.googleapis.com", "iamcredentials.googleapis.com",
    "logging.googleapis.com", "monitoring.googleapis.com", "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com", "sqladmin.googleapis.com", "storage.googleapis.com",
  ]
  bucket_location = { europe = "EU", us = "US", asia = "ASIA" }[var.location]
}

resource "google_project_service" "this" {
  for_each           = toset(local.apis)
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

resource "google_kms_key_ring" "state" {
  project    = var.project_id
  name       = "${var.name}-tofu-state"
  location   = var.location
  depends_on = [google_project_service.this]
}

resource "google_kms_crypto_key" "state" {
  name            = "state"
  key_ring        = google_kms_key_ring.state.id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "7776000s"
  deletion_policy = "PREVENT"

  lifecycle {
    prevent_destroy = true
  }
}

data "google_storage_project_service_account" "this" {
  project    = var.project_id
  depends_on = [google_project_service.this]
}

# Cloud Storage encrypts the state's objects with the key too, beside OpenTofu's own
# encryption of what it writes.
resource "google_kms_crypto_key_iam_member" "storage" {
  crypto_key_id = google_kms_crypto_key.state.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.this.email_address}"
}

resource "google_storage_bucket" "state" {
  #checkov:skip=CKV_GCP_62:Cloud Audit Logs' Data Access logs (the production root's logs module) record who read the state; usage logs are Google's legacy for that.
  project                     = var.project_id
  name                        = "${var.name}-tofu-state-${var.project_id}"
  location                    = local.bucket_location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  encryption {
    default_kms_key_name = google_kms_crypto_key.state.id
  }

  soft_delete_policy {
    retention_duration_seconds = 2592000
  }

  # Old versions go after a year; the current one stays.
  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 365
      with_state                 = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_kms_crypto_key_iam_member.storage]
}

output "backend_hcl" {
  description = "deploy/gcp/production's backend.hcl: tofu output -raw backend_hcl > ../production/backend.hcl."
  value       = "bucket = \"${google_storage_bucket.state.name}\"\n"
}

output "state_kms_key" {
  description = "deploy/gcp/production's state_kms_key."
  value       = google_kms_crypto_key.state.id
}
