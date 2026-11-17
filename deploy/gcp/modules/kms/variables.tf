variable "name" {
  description = "Names everything this module makes, such as iki-production: the key rings are <name>, <name>-dr and <name>-files."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,16}[a-z0-9]$", var.name))
    error_message = "Use 3 to 18 lowercase letters, digits and hyphens, starting with a letter: service account IDs add a suffix and allow 30."
  }
}

variable "project_id" {
  description = "The project everything is in."
  type        = string
}

variable "region" {
  description = "Where Iki runs, such as europe-west1: the cluster's, database's, secrets', images' and logs' keys are here."
  type        = string
}

variable "dr_region" {
  description = "The disaster-recovery region: keys for the database's replica and the secrets' replicas."
  type        = string

  validation {
    condition     = var.dr_region != var.region
    error_message = "The DR region must differ from the region Iki runs in."
  }
}

variable "files_location" {
  description = "Where the files bucket's key is: the bucket's location, lowercase. For a custom dual-region bucket, the multi-region containing both regions (europe, us or asia), so the key outlives either region; for a predefined dual-region (nam4), that one."
  type        = string
}

variable "rotation_days" {
  description = "How often each key gets a new version: what's encrypted from then uses it, and older data stays readable. CIS Google Cloud 1.10 asks for 90 days or fewer."
  type        = number
  default     = 90

  validation {
    condition     = var.rotation_days >= 1 && var.rotation_days <= 90
    error_message = "Rotate every 1 to 90 days, as CIS asks."
  }
}

variable "protection_level" {
  description = "SOFTWARE, or HSM to keep the keys in Cloud HSM (FIPS 140-2 level 3), at a higher price per key and operation."
  type        = string
  default     = "SOFTWARE"

  validation {
    condition     = contains(["SOFTWARE", "HSM"], var.protection_level)
    error_message = "Use SOFTWARE or HSM."
  }
}

variable "labels" {
  description = "Labels on everything this module makes."
  type        = map(string)
  default     = {}
}
