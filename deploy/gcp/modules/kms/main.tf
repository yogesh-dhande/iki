# Cloud KMS keys for every purpose (ADR 0107): one key each, rotated, in the location of
# what it encrypts, and usable only by the Google service that encrypts with it. Nothing
# uses Google's default keys. Key rings and keys can't be deleted in Google Cloud, only
# their versions destroyed, so OpenTofu is told never to destroy one.

data "google_project" "this" {
  project_id = var.project_id
}

# Each service encrypts with a key through its own service agent. Cloud Storage's and
# Cloud Logging's are looked up (which also makes them); the others follow Google's
# naming, and exist once their APIs are on: deploy/gcp/bootstrap makes Cloud SQL's,
# Secret Manager's, Artifact Registry's and Filestore's.
data "google_storage_project_service_account" "this" {
  project = var.project_id
}

data "google_logging_project_cmek_settings" "this" {
  project = var.project_id
}

locals {
  number = data.google_project.this.number
  agents = {
    kubernetes = "serviceAccount:service-${local.number}@container-engine-robot.iam.gserviceaccount.com"
    compute    = "serviceAccount:service-${local.number}@compute-system.iam.gserviceaccount.com"
    sql        = "serviceAccount:service-${local.number}@gcp-sa-cloud-sql.iam.gserviceaccount.com"
    secrets    = "serviceAccount:service-${local.number}@gcp-sa-secretmanager.iam.gserviceaccount.com"
    registry   = "serviceAccount:service-${local.number}@gcp-sa-artifactregistry.iam.gserviceaccount.com"
    filestore  = "serviceAccount:service-${local.number}@cloud-filer.iam.gserviceaccount.com"
    storage    = "serviceAccount:${data.google_storage_project_service_account.this.email_address}"
    logging    = "serviceAccount:${data.google_logging_project_cmek_settings.this.service_account_id}"
  }

  # Each key, the ring (and so the location) it's in, and the service agent that uses it.
  keys = {
    "kubernetes-secrets" = { ring = "region", agent = "kubernetes", what = "Kubernetes Secrets in etcd" }
    "node-disks"         = { ring = "region", agent = "compute", what = "the cluster's node disks" }
    "vm-disks"           = { ring = "region", agent = "compute", what = "the VM tier's disks and snapshots" }
    "database"           = { ring = "region", agent = "sql", what = "the database and its backups" }
    "secrets"            = { ring = "region", agent = "secrets", what = "Iki's secrets" }
    "registry"           = { ring = "region", agent = "registry", what = "plugin and worker images" }
    "filestore"          = { ring = "region", agent = "filestore", what = "the shared file system" }
    "logs"               = { ring = "region", agent = "logging", what = "the deployment's logs" }
    "database-dr"        = { ring = "dr", agent = "sql", what = "the database's replica in the DR region" }
    "secrets-dr"         = { ring = "dr", agent = "secrets", what = "Iki's secrets' replicas in the DR region" }
    "files"              = { ring = "files", agent = "storage", what = "files" }
  }
  rings = {
    region = { name = var.name, location = var.region }
    dr     = { name = "${var.name}-dr", location = var.dr_region }
    files  = { name = "${var.name}-files", location = var.files_location }
  }
}

resource "google_kms_key_ring" "this" {
  for_each = local.rings
  project  = var.project_id
  name     = each.value.name
  location = each.value.location
}

resource "google_kms_crypto_key" "this" {
  #checkov:skip=CKV_GCP_43:Rotated every var.rotation_days, which can't be more than 90; tests/kms.tftest.hcl checks the period.
  #checkov:skip=CKV_GCP_82:deletion_policy = "PREVENT" refuses to destroy it in the provider; prevent_destroy would also stop tofu test's mocked destroy.
  for_each        = local.keys
  name            = each.key
  key_ring        = google_kms_key_ring.this[each.value.ring].id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "${var.rotation_days * 86400}s"
  # A destroyed version can be restored for 30 days: what it encrypted is lost after.
  destroy_scheduled_duration = "2592000s"
  deletion_policy            = "PREVENT"
  labels                     = merge(var.labels, { iki-deployment = var.name, purpose = each.key })

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = var.protection_level
  }
}

resource "google_kms_crypto_key_iam_member" "agents" {
  for_each      = local.keys
  crypto_key_id = google_kms_crypto_key.this[each.key].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = local.agents[each.value.agent]
}
