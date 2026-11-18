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

variable "subnet_id" {
  description = "One private subnet: a Lustre file system lives in one zone."
  type        = string
}

variable "client_security_groups" {
  description = "What mounts it, by a name of yours: the cluster's node security group, and the vm-tier module's, such as { cluster = ..., vm_tier = ... }. The names are what OpenTofu plans by, so they hold when a group is made in the same apply and its ID isn't known yet."
  type        = map(string)

  validation {
    condition     = length(var.client_security_groups) > 0
    error_message = "Name at least one security group that mounts it."
  }
}

variable "storage_capacity_gib" {
  description = "Its size: 1200, 2400, or a multiple of 2400 GiB."
  type        = number
  default     = 2400

  validation {
    condition     = var.storage_capacity_gib == 1200 || var.storage_capacity_gib % 2400 == 0
    error_message = "Persistent SSD Lustre is 1200, 2400, or a multiple of 2400 GiB."
  }
}

variable "throughput_per_tib" {
  description = "MB/s for each TiB stored: 125, 250, 500 or 1000."
  type        = number
  default     = 250

  validation {
    condition     = contains([125, 250, 500, 1000], var.throughput_per_tib)
    error_message = "Persistent 2 Lustre offers 125, 250, 500 or 1000 MB/s per TiB."
  }
}

variable "reference_bucket" {
  description = "A bucket of reference data to show read-only at /reference, loaded as it's read and kept in step as it changes. Iki's own versions go under /iki (ADR 0077). Null: none."
  type = object({
    arn    = string
    prefix = optional(string, "")
  })
  default = null
}

variable "backup_retention_days" {
  description = "Daily backups kept, 0 to 90. What Iki builds can be built again, so 7 is plenty."
  type        = number
  default     = 7

  validation {
    condition     = var.backup_retention_days >= 0 && var.backup_retention_days <= 90
    error_message = "FSx keeps 0 to 90 days of backups."
  }
}

variable "root_squash" {
  description = "Treat root on every client as nobody, but for the Lustre network IDs listed (such as 10.40.1.5@tcp): an administrator's machine, to make /iki owned by runs' user (UID 10001) before the first build."
  type = object({
    enabled     = optional(bool, true)
    except_nids = optional(list(string), [])
  })
  default = {}

  validation {
    condition     = alltrue([for n in var.root_squash.except_nids : can(regex("^[0-9.\\[\\]*-]+@tcp$", n))])
    error_message = "Give Lustre network IDs, such as 10.40.1.5@tcp or 10.40.1.[5-9]@tcp."
  }
}

variable "tags" {
  description = "Tags on everything this module makes."
  type        = map(string)
  default     = {}
}
