variable "name" {
  description = "Names everything this module makes, such as iki-production."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,16}[a-z0-9]$", var.name))
    error_message = "Use 3 to 18 lowercase letters, digits and hyphens, starting with a letter: service account IDs add a suffix and allow 30."
  }
}

variable "project_id" {
  description = "The project everything is in, and whose Workload Identity pool the cluster uses."
  type        = string
}

variable "namespace" {
  description = "The namespace Iki's chart is installed in."
  type        = string
  default     = "iki"
}

variable "release" {
  description = "The chart's full name, which prefixes its service accounts: the Helm release's name, if it contains \"iki\" (as \"iki\" does)."
  type        = string
  default     = "iki"
}

variable "files" {
  description = "The files bucket, from the storage module, and the prefix Iki writes under (the chart's files.prefix)."
  type = object({
    bucket = string
    prefix = optional(string, "")
  })
}

variable "register_locations" {
  description = "Buckets (and prefixes) where files may be registered in place: the chart's files.registerLocations. Iki only reads them. Each bucket needs uniform bucket-level access, for the prefix's condition."
  type = list(object({
    bucket = string
    prefix = optional(string, "")
  }))
  default = []
}

variable "external_secrets" {
  description = "External Secrets Operator's Kubernetes service account, and the secrets it may read: from the secrets module."
  type = object({
    namespace       = optional(string, "external-secrets")
    service_account = optional(string, "external-secrets")
    secret_ids      = map(string)
  })
}

variable "vm_tier" {
  description = "The VM tier (ADR 0074, 0076): the prefix its machines', disks' and images' names carry (the chart's vmTier.owner), the machines' service account from the vm-tier module, and the subnet and region they start in. Null: no provisioner identity."
  type = object({
    owner                    = string
    machines_service_account = string
    subnet                   = string
    region                   = string
  })
  default = null
}
