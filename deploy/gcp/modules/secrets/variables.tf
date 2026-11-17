variable "name" {
  description = "Names everything this module makes, such as iki-production. Each secret is <name>-<secret>."
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
  description = "Where Iki runs: one copy of each secret is here."
  type        = string
}

variable "dr_region" {
  description = "The DR region: the other copy is here."
  type        = string

  validation {
    condition     = var.dr_region != var.region
    error_message = "The DR region must differ from the region Iki runs in."
  }
}

variable "kms_key" {
  description = "The secrets' key in region, from the kms module."
  type        = string
}

variable "dr_kms_key" {
  description = "The secrets' key in the DR region, from the kms module."
  type        = string
}

variable "optional_secrets" {
  description = "Secrets for features you turn on: email (the mail server's URL, with its credentials), backend-worker-token (a registration token a Compute admin makes in Iki)."
  type        = set(string)
  default     = []

  validation {
    condition     = length(setsubtract(var.optional_secrets, ["email", "backend-worker-token"])) == 0
    error_message = "Optional secrets are email and backend-worker-token."
  }
}

variable "recovery_window_days" {
  description = "How long a destroyed version of a secret can still be restored."
  type        = number
  default     = 30

  validation {
    condition     = var.recovery_window_days >= 1 && var.recovery_window_days <= 30
    error_message = "Secret Manager delays destroying a version 1 to 30 days."
  }
}

variable "labels" {
  description = "Labels on everything this module makes."
  type        = map(string)
  default     = {}
}
