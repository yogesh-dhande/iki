output "file_system_id" {
  description = "The file system, for the PersistentVolume runs' claim binds to (FSx's CSI driver)."
  value       = aws_fsx_lustre_file_system.this.id
}

output "dns_name" {
  description = "Where clients mount it from."
  value       = aws_fsx_lustre_file_system.this.dns_name
}

output "mount_name" {
  description = "Its Lustre mount name: mount -t lustre <dns_name>@tcp:/<mount_name>."
  value       = aws_fsx_lustre_file_system.this.mount_name
}

output "security_group_id" {
  description = "Its security group: give it to the vm-tier module's file_systems, as lustre."
  value       = aws_security_group.this.id
}
