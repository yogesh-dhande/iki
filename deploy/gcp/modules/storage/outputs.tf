output "bucket" {
  description = "The files bucket's name: the chart's files.bucket."
  value       = google_storage_bucket.files.name
}

output "location" {
  description = "Where it is."
  value       = google_storage_bucket.files.location
}

output "kms_key" {
  description = "The key new files are encrypted with."
  value       = var.kms_key
}
