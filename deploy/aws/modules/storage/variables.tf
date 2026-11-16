variable "name" {
  description = "Names everything this module makes, such as iki-production. Buckets add the account's ID, since their names are global."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name))
    error_message = "Use 3 to 32 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "access_log_retention_days" {
  description = "How long S3's access logs of the files bucket are kept."
  type        = number
  default     = 365
}

variable "dr_storage_class" {
  description = "The storage class of the copies in the DR region, which are rarely read: STANDARD_IA is cheaper to keep and dearer to read."
  type        = string
  default     = "STANDARD_IA"

  validation {
    condition     = contains(["STANDARD", "STANDARD_IA", "INTELLIGENT_TIERING", "GLACIER_IR"], var.dr_storage_class)
    error_message = "Use STANDARD, STANDARD_IA, INTELLIGENT_TIERING or GLACIER_IR: the copies must be readable at once to recover."
  }
}

variable "replication_time_control" {
  description = "S3 Replication Time Control: AWS commits to replicating 99.99% of objects within 15 minutes, and reports on it, for a fee per GB. Without it, most objects replicate within seconds, with no commitment."
  type        = bool
  default     = false
}

variable "adopt" {
  description = "Recovering from a lost region (docs/disaster-recovery.md): the lost deployment's copy here, its bucket and its key's ARN or alias, taken on as this deployment's files bucket rather than a new one made. Every file and run artifact is already in it. Null: a new bucket."
  type = object({
    bucket  = string
    kms_key = string
  })
  default = null
}

variable "tags" {
  description = "Tags on everything this module makes."
  type        = map(string)
  default     = {}
}
