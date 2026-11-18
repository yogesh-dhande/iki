variable "name" {
  description = "Names everything this module makes, such as iki-production. Each secret is <name>/<secret>."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name))
    error_message = "Use 3 to 32 lowercase letters, digits and hyphens, starting with a letter."
  }
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
  description = "How long a deleted secret can still be restored."
  type        = number
  default     = 30
  nullable    = false

  validation {
    condition     = var.recovery_window_days >= 7 && var.recovery_window_days <= 30
    error_message = "Secrets Manager keeps a deleted secret 7 to 30 days."
  }
}

variable "tags" {
  description = "Tags on everything this module makes."
  type        = map(string)
  default     = {}
}
