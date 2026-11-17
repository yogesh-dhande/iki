output "address" {
  description = "The database's host name: its certificate names it, for sslmode=verify-full."
  value       = local.dns_name
}

output "private_ip" {
  description = "Its private address."
  value       = google_sql_database_instance.this.private_ip_address
}

output "port" {
  description = "Postgres's port."
  value       = 5432
}

output "instance" {
  description = "The instance's name, for gcloud sql."
  value       = google_sql_database_instance.this.name
}

output "connection_name" {
  description = "project:region:instance, for the Cloud SQL Auth Proxy."
  value       = google_sql_database_instance.this.connection_name
}

output "dr_instance" {
  description = "The replica in the DR region, to promote after losing this one; null without one."
  value       = one(google_sql_database_instance.dr[*].name)
}

output "adopted" {
  description = "The promoted replica this database is, recovering from a lost region; null for a database made new."
  value       = local.adopted ? local.instance : null
}

output "deletion_protection" {
  description = "Whether OpenTofu and Cloud SQL refuse to delete the database."
  value       = google_sql_database_instance.this.deletion_protection && one(google_sql_database_instance.this.settings).deletion_protection_enabled
}
