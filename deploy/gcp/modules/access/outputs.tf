output "server_service_account" {
  description = "The Google service account of Iki's web, tasks and maintenance pods: the chart's serviceAccount.annotations names it, and its HMAC key is the files' credentials."
  value       = google_service_account.server.email
}

output "provisioner_service_account" {
  description = "The VM tier provisioner's Google service account, or null."
  value       = one(google_service_account.provisioner[*].email)
}

output "external_secrets_principal" {
  description = "External Secrets' identity, as IAM sees it."
  value       = "${local.principal}/ns/${var.external_secrets.namespace}/sa/${var.external_secrets.service_account}"
}
