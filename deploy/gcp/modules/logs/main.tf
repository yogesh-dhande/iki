# The deployment's logs, kept a year and encrypted with a key of their own (ADR 0107):
# Cloud Audit Logs (who did what to which resource, including reads of files and
# secrets), the Kubernetes API's audit log and its control plane's, VPC flow logs,
# firewall and NAT logs, and Postgres's logs (connections, DDL and role changes through
# pgaudit). Cloud Logging's _Default bucket keeps 30 days; this keeps them as long as
# your records need.

locals {
  # One query per kind, joined with OR.
  kinds = [
    "logName:\"cloudaudit.googleapis.com\"",
    "resource.type=\"k8s_cluster\"",
    "resource.type=\"k8s_control_plane_component\"",
    "logName:\"compute.googleapis.com%2Fvpc_flows\"",
    "logName:\"compute.googleapis.com%2Ffirewall\"",
    "logName:\"compute.googleapis.com%2Fnat_flows\"",
    "resource.type=\"cloudsql_database\"",
  ]
}

resource "google_logging_project_bucket_config" "this" {
  project        = var.project_id
  location       = var.region
  bucket_id      = var.name
  description    = "${var.name}: audit, control-plane, network and database logs"
  retention_days = var.retention_days
  # Locked, retention can't be shortened or the bucket deleted until every entry is
  # past it: for records that must be kept unaltered (21 CFR Part 11).
  locked = var.locked

  cmek_settings {
    kms_key_name = var.kms_key
  }
}

resource "google_logging_project_sink" "this" {
  project     = var.project_id
  name        = var.name
  description = "${var.name}: what the ${var.name} log bucket keeps"
  destination = "logging.googleapis.com/${google_logging_project_bucket_config.this.id}"
  filter      = join(" OR ", local.kinds)
  # A bucket in the same project needs no writer identity of its own.
  unique_writer_identity = true
}

# Reads and writes of objects, secrets and keys are Data Access logs, which are off by
# default. On for the services holding Iki's data: these are S3's access logs and
# CloudTrail's data events, here.
resource "google_project_iam_audit_config" "data_access" {
  for_each = toset(var.data_access_services)
  project  = var.project_id
  service  = each.key

  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}
