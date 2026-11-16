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

variable "client_security_group_ids" {
  description = "Security groups allowed to connect, such as the EKS cluster's. Nothing else reaches the database."
  type        = list(string)
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
}

variable "allocated_storage_gib" {
  description = "Storage to start with."
  type        = number
  default     = 100
}

variable "max_allocated_storage_gib" {
  description = "How far RDS may grow storage by itself as it fills."
  type        = number
  default     = 1000
}

variable "backup_retention_days" {
  description = "How far back point-in-time recovery reaches, in days. 35 is RDS's most."
  type        = number
  default     = 35

  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35
    error_message = "Keep 7 to 35 days of backups."
  }
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

variable "tags" {
  description = "Tags on everything this module makes."
  type        = map(string)
  default     = {}
}
