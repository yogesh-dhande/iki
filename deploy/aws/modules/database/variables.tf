variable "name" {
  description = "Names everything this module makes, such as iki-production."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name))
    error_message = "Use 3 to 32 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "vpc_id" {
  description = "The VPC, from the network module."
  type        = string
}

variable "subnet_ids" {
  description = "The database subnets, from the network module: two or more zones, with no route out of the VPC."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "Multi-AZ needs subnets in at least two zones."
  }
}

variable "client_security_groups" {
  description = "Security groups allowed to connect, by a name of yours, such as { cluster = the EKS cluster's }. Nothing else reaches the database. The names are what OpenTofu plans by, so they hold when a group is made in the same apply and its ID isn't known yet."
  type        = map(string)
}

variable "engine_version" {
  description = "PostgreSQL's major version (RDS keeps the minor version current), or a full version to pin it."
  type        = string
  default     = "17"
}

variable "instance_class" {
  description = "The instance class. Memory-optimised Graviton suits Iki's mix of reads and writes."
  type        = string
  default     = "db.r7g.large"
  # Null keeps the default, so a root can pass its own optional value through.
  nullable = false

  validation {
    condition     = can(regex("^db\\.[a-z0-9]+\\.[a-z0-9]+$", var.instance_class))
    error_message = "Give an RDS instance class, such as db.r7g.large."
  }

  # A micro's 1 GiB isn't enough: with Multi-AZ, Performance Insights and Iki's pools it
  # swaps, and new connections time out (live run iki-test-11180614's task runner).
  validation {
    condition     = !can(regex("\\.(nano|micro)$", var.instance_class))
    error_message = "Iki's database needs 2 GiB of memory or more: a small class (db.t4g.small) at least, never a nano or micro."
  }
}

variable "allocated_storage_gib" {
  description = "Storage to start with."
  type        = number
  default     = 100
  nullable    = false

  validation {
    condition     = var.allocated_storage_gib >= 20
    error_message = "gp3 storage starts at 20 GiB."
  }
}

variable "max_allocated_storage_gib" {
  description = "How far RDS may grow storage by itself as it fills."
  type        = number
  default     = 1000
  nullable    = false

  validation {
    condition     = var.max_allocated_storage_gib > var.allocated_storage_gib
    error_message = "max_allocated_storage_gib must be more than allocated_storage_gib, so storage can grow."
  }
}

variable "backup_retention_days" {
  description = "How far back point-in-time recovery reaches, in days. 35 is RDS's most."
  type        = number
  default     = 35
  nullable    = false

  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35
    error_message = "Keep 7 to 35 days of backups."
  }
}

variable "deletion_protection" {
  description = "Refuse to delete the database. Turn it off only to tear the deployment down (deploy/aws/scripts/teardown.sh does)."
  type        = bool
  default     = true
  nullable    = false
}

variable "dr_backup_retention_days" {
  description = "How far back the copy in the DR region reaches, in days."
  type        = number
  default     = 7

  validation {
    condition     = var.dr_backup_retention_days >= 1 && var.dr_backup_retention_days <= 35
    error_message = "Keep 1 to 35 days of backups in the DR region."
  }
}

variable "log_retention_days" {
  description = "How long Postgres's logs (connections, DDL and role changes, through pgaudit) are kept."
  type        = number
  default     = 365
}

variable "backup_window" {
  description = "When the daily snapshot is taken, in UTC. Point-in-time recovery covers every minute regardless."
  type        = string
  default     = "03:00-04:00"
}

variable "maintenance_window" {
  description = "When RDS may apply minor upgrades and patches, in UTC. Multi-AZ fails over first, so it's a minute or so of reconnections."
  type        = string
  default     = "sun:04:30-sun:05:30"
}

variable "restore_from" {
  description = "Recovering from a lost region (docs/disaster-recovery.md): the lost database's automated backups, copied here, and the moment to restore to (RFC 3339), or null for the latest they reach. Null: a new, empty database."
  type = object({
    automated_backups_arn = string
    restore_time          = optional(string)
  })
  default = null

  validation {
    condition     = var.restore_from == null || can(regex("^arn:[^:]+:rds:[^:]+:[0-9]{12}:auto-backup:ab-", var.restore_from.automated_backups_arn))
    error_message = "restore_from.automated_backups_arn should be an automated backup's ARN, arn:aws:rds:<region>:<account>:auto-backup:ab-...: describe-db-instance-automated-backups gives it."
  }
  validation {
    condition     = var.restore_from == null || try(var.restore_from.restore_time == null || can(formatdate("YYYY", var.restore_from.restore_time)), false)
    error_message = "restore_from.restore_time should be an RFC 3339 time, such as 2026-11-16T09:41:00Z."
  }
}

variable "tags" {
  description = "Tags on everything this module makes."
  type        = map(string)
  default     = {}
}
