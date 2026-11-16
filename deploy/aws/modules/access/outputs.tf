output "server_role_arns" {
  description = "The roles of Iki's web, tasks and maintenance pods."
  value       = { for k, r in aws_iam_role.server : k => r.arn }
}

output "provisioner_role_arn" {
  description = "The VM tier provisioner's role, or null."
  value       = one(aws_iam_role.provisioner[*].arn)
}

output "external_secrets_role_arn" {
  description = "External Secrets Operator's role."
  value       = aws_iam_role.external_secrets.arn
}

output "load_balancer_controller_role_arn" {
  description = "The AWS Load Balancer Controller's role, or null."
  value       = one(aws_iam_role.load_balancer_controller[*].arn)
}

output "cluster_autoscaler_role_arn" {
  description = "The Cluster Autoscaler's role, or null."
  value       = one(aws_iam_role.cluster_autoscaler[*].arn)
}
