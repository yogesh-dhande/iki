output "service_account" {
  description = "The machines' service account: the only one the provisioner may give a machine (the access module's vm_tier.machines_service_account)."
  value       = google_service_account.machines.email
}

output "member" {
  description = "It as an IAM member, for the artifact-registry module's readers."
  value       = google_service_account.machines.member
}

output "instance_template" {
  description = "The template machines start from (sourceInstanceTemplate): a new one replaces it whenever it changes."
  value       = google_compute_region_instance_template.machines.self_link
}

output "network_tag" {
  description = "The network tag machines carry."
  value       = var.network_tag
}

output "disk_kms_key" {
  description = "The key for the machines' disks, and the images setup builds make."
  value       = var.kms_key
}
