output "secret_arns" {
  description = "Each secret's ARN, by its name: External Secrets may read these."
  value       = { for k, s in aws_secretsmanager_secret.this : k => s.arn }
}

output "secret_names" {
  description = "Each secret's name in Secrets Manager, by its short name: an ExternalSecret's remoteRef.key."
  value       = { for k, s in aws_secretsmanager_secret.this : k => s.name }
}

output "kms_key_arn" {
  description = "The key the secrets are encrypted with: External Secrets needs to decrypt with it."
  value       = aws_kms_key.secrets.arn
}
