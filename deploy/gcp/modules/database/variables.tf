variable "name" {
  description = "Names everything this module makes, such as iki-production: the instance, and <name>-dr for its replica."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,16}[a-z0-9]$", var.name))
    error_message = "Use 3 to 18 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "project_id" {
  description = "The project everything is in."
  type        = string
}

variable "region" {
  description = "Where the database runs: the cluster's region."
  type        = string
}

variable "network_id" {
  description = "The VPC, from the network module: the database has an address in it and no other."
  type        = string
}

variable "allocated_ip_range" {
  description = "The range reserved for Google's managed services, from the network module (managed_services_range_name). Make the database only once that peering exists."
  type        = string
}

variable "kms_key" {
  description = "The database's key, from the kms module: in its region."
  type        = string
}

variable "engine_version" {
  description = "PostgreSQL's major version. Cloud SQL keeps the minor version current."
  type        = string
  default     = "17"

  validation {
    condition     = can(regex("^1[5-9]$", var.engine_version))
    error_message = "Give a major version Cloud SQL offers, 15 or later, such as \"17\"."
  }
}

variable "edition" {
  description = "ENTERPRISE_PLUS (99.99% availability, near-zero-downtime maintenance, 35 days of point-in-time recovery) or ENTERPRISE (at most 7 days)."
  type        = string
  default     = "ENTERPRISE_PLUS"

  validation {
    condition     = contains(["ENTERPRISE", "ENTERPRISE_PLUS"], var.edition)
    error_message = "Use ENTERPRISE or ENTERPRISE_PLUS."
  }
}

variable "tier" {
  description = "The machine: db-perf-optimized-N-<vCPUs> for Enterprise Plus, db-custom-<vCPUs>-<MiB> for Enterprise."
  type        = string
  default     = "db-perf-optimized-N-4"
}

variable "disk_size_gib" {
  description = "Storage to start with."
  type        = number
  default     = 100
}

variable "max_disk_size_gib" {
  description = "How far Cloud SQL may grow storage by itself as it fills."
  type        = number
  default     = 1000
}

variable "backup_retention_days" {
  description = "How far back point-in-time recovery reaches, in days; one more daily backup than that is kept. 35 is the most Enterprise Plus allows; Enterprise allows 7."
  type        = number
  default     = 35

  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35
    error_message = "Keep 7 to 35 days of backups."
  }
  validation {
    condition     = var.edition == "ENTERPRISE_PLUS" || var.backup_retention_days <= 7
    error_message = "Enterprise keeps at most 7 days of point-in-time recovery: use ENTERPRISE_PLUS for more."
  }
}

variable "backup_location" {
  description = "Where daily backups are stored: the DR region, so they outlive this one. Null: the instance's own region."
  type        = string
  default     = null
}

variable "backup_start_time" {
  description = "When the daily backup starts, HH:MM in UTC. Point-in-time recovery covers every minute regardless."
  type        = string
  default     = "03:00"
}

variable "maintenance_window" {
  description = "When Cloud SQL may apply updates: day 1 (Monday) to 7, and hour in UTC. Enterprise Plus does it with under a second of downtime."
  type = object({
    day  = number
    hour = number
  })
  default = { day = 7, hour = 4 }
}

variable "dr_region" {
  description = "Where the cross-region replica is. Null: no replica, such as for a deployment recovering into what was its DR region while the lost one is down."
  type        = string
  default     = null

  validation {
    condition     = var.dr_region == null || var.dr_region != var.region
    error_message = "The replica's region must differ from the database's."
  }
}

variable "dr_kms_key" {
  description = "The replica's key, from the kms module: in the DR region."
  type        = string
  default     = null

  validation {
    condition     = var.dr_region == null || var.dr_kms_key != null
    error_message = "A replica in dr_region needs dr_kms_key: a key in that region (Cloud SQL encrypts with keys in its own region)."
  }
}

variable "iam_principals" {
  description = "People, groups or service accounts who may sign in to Postgres with IAM rather than a password, such as your DBAs' group. They get no privileges there until granted some."
  type = list(object({
    email = string
    type  = optional(string, "group")
  }))
  default = []

  validation {
    condition     = alltrue([for p in var.iam_principals : contains(["user", "group", "service_account"], p.type)])
    error_message = "A principal's type is user, group or service_account."
  }
}

variable "adopt" {
  description = "Recovering from a lost region (docs/disaster-recovery.md): the lost deployment's replica, promoted, and the key it's encrypted with, taken on as this deployment's database. Import it first (the production root does). Null: a new database."
  type = object({
    instance = string
    kms_key  = string
  })
  default = null
}

variable "deletion_protection" {
  description = "Refuse to delete the database and its replica, in OpenTofu and in Cloud SQL. Turn off only to tear a deployment down: deleting it still keeps its backups."
  type        = bool
  default     = true
}

variable "labels" {
  description = "Labels on everything this module makes."
  type        = map(string)
  default     = {}
}
