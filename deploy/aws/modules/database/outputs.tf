output "address" {
  description = "The database's host name. Its certificate names it, for sslmode=verify-full."
  value       = aws_db_instance.this.address
}

output "port" {
  description = "Postgres's port."
  value       = aws_db_instance.this.port
}

output "name" {
  description = "The database Iki uses."
  value       = aws_db_instance.this.db_name
}

output "arn" {
  description = "The instance's ARN."
  value       = aws_db_instance.this.arn
}

output "master_secret_arn" {
  description = "The Secrets Manager secret RDS keeps the master user's password in. Only the bootstrap that creates Iki's roles reads it."
  value       = one(aws_db_instance.this.master_user_secret[*].secret_arn)
}

output "security_group_id" {
  description = "The database's security group."
  value       = aws_security_group.database.id
}

output "dr_backups_arn" {
  description = "The automated backups in the DR region, to restore from there."
  value       = aws_db_instance_automated_backups_replication.dr.id
}

output "restored_from" {
  description = "The automated backups this database was restored from, recovering from a lost region; null for a database made new."
  value       = one(aws_db_instance.this.restore_to_point_in_time[*].source_db_instance_automated_backups_arn)
}
