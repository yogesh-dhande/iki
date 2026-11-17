output "bucket" {
  description = "The log bucket's ID, to query it: gcloud logging read --bucket=<name> --location=<region> --view=_AllLogs."
  value       = google_logging_project_bucket_config.this.id
}
