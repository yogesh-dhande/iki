output "secret_ids" {
  description = "Each secret's resource ID (projects/<project>/secrets/<name>), by its short name: External Secrets may read these."
  value       = { for k, s in google_secret_manager_secret.this : k => s.id }
}

output "secret_names" {
  description = "Each secret's name in Secret Manager, by its short name: an ExternalSecret's remoteRef.key."
  value       = { for k, s in google_secret_manager_secret.this : k => s.secret_id }
}
