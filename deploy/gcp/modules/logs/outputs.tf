output "bucket" {
  description = "The log bucket's ID, to query it: gcloud logging read --bucket=<name> --location=<region> --view=_AllLogs."
  value       = google_logging_project_bucket_config.this.id
}

output "data_access_services" {
  description = "The services whose Data Access audit logs are on, each to its audit config's ID."
  value       = { for s, c in google_project_iam_audit_config.data_access : s => c.id }
}
