terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 8.11, < 9.0"
    }
  }

  # The state lives in the bootstrap's bucket, which Cloud Storage locks while anyone
  # applies. Its bucket comes from backend.hcl: tofu init -backend-config=backend.hcl.
  backend "gcs" {
    prefix = "iki/production"
  }

  # And it's encrypted by OpenTofu before it's written, with the bootstrap's key: the
  # bucket's own encryption doesn't stop anyone who can read the bucket.
  encryption {
    key_provider "gcp_kms" "state" {
      kms_encryption_key = var.state_kms_key
      key_length         = 32
    }
    method "aes_gcm" "state" {
      keys = key_provider.gcp_kms.state
    }
    state {
      method   = method.aes_gcm.state
      enforced = true
    }
    plan {
      method   = method.aes_gcm.state
      enforced = true
    }
  }
}

provider "google" {
  project        = var.project_id
  region         = var.region
  default_labels = var.labels
}
