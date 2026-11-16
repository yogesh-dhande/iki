output "name" {
  description = "The cluster's name."
  value       = aws_eks_cluster.this.name
}

output "arn" {
  description = "The cluster's ARN: Pod Identity roles trust pods of this cluster only."
  value       = aws_eks_cluster.this.arn
}

output "endpoint" {
  description = "The Kubernetes API's address."
  value       = aws_eks_cluster.this.endpoint
}

output "certificate_authority" {
  description = "The API's CA certificate, base64-encoded, for a kubeconfig."
  value       = aws_eks_cluster.this.certificate_authority[0].data
}

output "security_group_id" {
  description = "The security group EKS gave the control plane and the nodes. The database admits it."
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

output "node_role_arn" {
  description = "The nodes' IAM role."
  value       = aws_iam_role.node.arn
}

output "node_pools" {
  description = "Each pool's label and, for dedicated pools, the toleration pods need to run there."
  value = {
    for name, _ in var.node_pools : name => {
      node_selector = { "iki.ikisci.org/pool" = name }
      tolerations   = name == "system" ? [] : [{ key = "iki.ikisci.org/pool", operator = "Equal", value = name, effect = "NoSchedule" }]
    }
  }
}
