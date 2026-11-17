output "keys" {
  description = "Each key's ID, by purpose. Ready only once its service agent may use it."
  value       = { for k, key in google_kms_crypto_key.this : k => key.id }
  depends_on  = [google_kms_crypto_key_iam_member.agents]
}

output "project_number" {
  description = "The project's number, which service agents' and Workload Identity's names carry."
  value       = local.number
}
