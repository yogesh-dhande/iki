variable "name" {
  description = "Names the log bucket and its sink, such as iki-production."
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
  description = "Where the log bucket is: its key must be there too."
  type        = string
}

variable "kms_key" {
  description = "The logs' key, from the kms module."
  type        = string
}

variable "retention_days" {
  description = "How long logs are kept."
  type        = number
  default     = 365

  validation {
    condition     = var.retention_days >= 30 && var.retention_days <= 3650
    error_message = "Keep logs 30 to 3650 days."
  }
}

variable "locked" {
  description = "Lock the bucket's retention: then nobody can shorten it or delete the bucket while it holds logs younger than retention_days. It can't be unlocked."
  type        = bool
  default     = false
}

variable "data_access_services" {
  description = "Services whose Data Access audit logs are turned on: reads and writes of files, secrets and keys. Project-wide, and authoritative for each service named."
  type        = list(string)
  default     = ["storage.googleapis.com", "secretmanager.googleapis.com", "cloudkms.googleapis.com", "cloudsql.googleapis.com"]
}
