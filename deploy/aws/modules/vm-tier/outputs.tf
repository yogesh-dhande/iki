output "instance_profile_name" {
  description = "The machines' instance profile: a template's aws.instance_profile."
  value       = aws_iam_instance_profile.worker.name
}

output "worker_role_arn" {
  description = "The machines' role: the provisioner may pass this one alone (the access module's vm_tier.worker_role_arn)."
  value       = aws_iam_role.worker.arn
}

output "security_group_id" {
  description = "The machines' security group: a template's aws.security_groups. Let it in to your file systems' security groups, too."
  value       = aws_security_group.machines.id
}

output "disk_kms_key_arn" {
  description = "The key for the machines' disks and snapshots: a template's aws.kms_key, and the access module's vm_tier.disk_kms_key_arn."
  value       = aws_kms_key.disks.arn
}
