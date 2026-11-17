variable "name" {
  description = "Names the bucket, such as iki-production: <name>-files-<project>, since bucket names are global."
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

variable "location" {
  description = "The bucket's location: a multi-region (EU, US, ASIA) with data_locations for a dual-region of your choosing, or a predefined dual-region (EUR4, NAM4) or a region."
  type        = string
}

variable "data_locations" {
  description = "The two regions of a custom dual-region, such as [\"europe-west1\", \"europe-west4\"]: the region Iki runs in and its DR region, both in location. Empty for a predefined location."
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.data_locations) == 0 || length(distinct(var.data_locations)) == 2
    error_message = "Name two different regions, or none."
  }
}

variable "turbo_replication" {
  description = "Turbo replication: Google commits to copying every new object to the other region within 15 minutes, for a fee per GB written. Without it, 99.9% within an hour."
  type        = bool
  default     = false
}

variable "kms_key" {
  description = "The files key, from the kms module: in the bucket's location, or the multi-region containing its regions."
  type        = string
}

variable "prefix" {
  description = "Where in the bucket Iki writes: the chart's files.prefix. Empty: anywhere."
  type        = string
  default     = ""
}

variable "cors_origins" {
  description = "Where browsers upload and read from: Iki's public URL, such as https://iki.example.com."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for o in var.cors_origins : startswith(o, "https://")])
    error_message = "Origins are https:// URLs."
  }
}

variable "soft_delete_days" {
  description = "How long a deleted object or version can be restored. Iki's own expiry (ADR 0068) deletes artifacts' versions; they stay restorable, and stored, this long."
  type        = number
  default     = 30

  validation {
    condition     = var.soft_delete_days >= 7 && var.soft_delete_days <= 90
    error_message = "Cloud Storage keeps deleted objects 7 to 90 days."
  }
}

variable "object_retention" {
  description = "Let retention periods and holds be set on single objects. It can't be turned off once on."
  type        = bool
  default     = true
}

variable "adopt" {
  description = "Recovering from a lost region (docs/disaster-recovery.md): the lost deployment's bucket, which outlived the region, taken on as this deployment's files bucket. Import it first (the production root does). Null: a new bucket."
  type = object({
    bucket = string
  })
  default = null
}

variable "labels" {
  description = "Labels on the bucket."
  type        = map(string)
  default     = {}
}
