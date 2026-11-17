# The files bucket (ADR 0036, 0107): in two regions at once, so losing one loses no file
# and the bucket keeps its name; every version kept; encrypted with a key of its own,
# never Google's default keys; private, with uniform access; deletions recoverable for a
# while. Iki never overwrites what it stores, so versions are its history, not churn.
# Reads and writes are in Cloud Audit Logs (the logs module turns them on).

locals {
  labels  = merge(var.labels, { iki-deployment = var.name })
  adopted = var.adopt != null
  bucket  = local.adopted ? var.adopt.bucket : "${var.name}-files-${var.project_id}"
  # Iki's own rule, which `iki-server setup_storage` sets on S3: abandoned uploads, and
  # staging objects older than 2 days, go.
  staging = var.prefix == "" ? "uploads/" : "${trimsuffix(var.prefix, "/")}/uploads/"
}

resource "google_storage_bucket" "files" {
  #checkov:skip=CKV_GCP_62:Cloud Audit Logs' Data Access logs record every read and write (the logs module); usage logs are Google's legacy for that.
  project  = var.project_id
  name     = local.bucket
  location = var.location
  # A custom dual-region: written to both regions, served from either.
  dynamic "custom_placement_config" {
    for_each = length(var.data_locations) == 0 ? [] : [1]
    content {
      data_locations = [for l in var.data_locations : upper(l)]
    }
  }
  # Turbo replication copies every object to the other region within 15 minutes.
  rpo           = length(var.data_locations) == 0 ? null : (var.turbo_replication ? "ASYNC_TURBO" : "DEFAULT")
  storage_class = "STANDARD"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  # Never deleted with what it holds.
  force_destroy = false

  versioning {
    enabled = true
  }

  # This key encrypts new objects; neither Google's default keys nor a caller's own
  # (customer-supplied) may.
  encryption {
    default_kms_key_name = var.kms_key
    google_managed_encryption_enforcement_config {
      restriction_mode = "FullyRestricted"
    }
    customer_supplied_encryption_enforcement_config {
      restriction_mode = "FullyRestricted"
    }
    customer_managed_encryption_enforcement_config {
      restriction_mode = "NotRestricted"
    }
  }

  # A deleted object or version can be restored for this long (S3 has no equivalent).
  soft_delete_policy {
    retention_duration_seconds = var.soft_delete_days * 86400
  }

  # Lets a retention period or legal hold be put on single objects later (legal holds are
  # Iki's own, ADR 0092; this keeps the option of holding them in storage too).
  enable_object_retention = var.object_retention

  # Iki's rule for abandoned uploads, as setup_storage would set it: OpenTofu's here,
  # since the server's account can't change the bucket (setup_storage --check checks it).
  lifecycle_rule {
    condition {
      age            = 2
      matches_prefix = [local.staging]
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }
  lifecycle_rule {
    condition {
      age            = 2
      matches_prefix = [local.staging]
      with_state     = "ANY"
    }
    action {
      type = "Delete"
    }
  }

  # Browsers send upload parts straight here, and some pages read files, from Iki's
  # public URL, with links Iki signed: setup_storage's CORS rule.
  dynamic "cors" {
    for_each = length(var.cors_origins) == 0 ? [] : [1]
    content {
      origin          = var.cors_origins
      method          = ["GET", "PUT"]
      response_header = ["content-type"]
      max_age_seconds = 3600
    }
  }

  labels = local.labels
}
