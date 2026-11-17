output "address" {
  description = "Its NFS server's address, for runs' PersistentVolume and the VM tier's templates."
  value       = one(one(google_filestore_instance.this.networks).ip_addresses)
}

output "share" {
  description = "The exported share: mount <address>:/<share>."
  value       = one(google_filestore_instance.this.file_shares).name
}

output "cidr" {
  description = "Its address as a network, for the vm-tier module's nfs_cidrs."
  value       = "${one(one(google_filestore_instance.this.networks).ip_addresses)}/32"
}
