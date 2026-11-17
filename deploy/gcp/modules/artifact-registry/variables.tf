variable "name" {
  description = "Prefixes the repositories' names, such as iki-production."
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
  description = "Where the repositories are: the cluster's region, so pulls stay in it. Its key must be there too."
  type        = string
}

variable "kms_key" {
  description = "The images' key, from the kms module."
  type        = string
}

variable "repositories" {
  description = "Repositories to make, and what each holds. Each is <name>-<key>."
  type        = map(string)
  default = {
    worker  = "Iki's worker image, which runs every action"
    plugins = "plugins' environment images"
  }
}

variable "readers" {
  description = "Who may pull, by a name of your choosing: the cluster's nodes and the VM tier's machines, as IAM members (serviceAccount:...)."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for m in values(var.readers) : can(regex("^(serviceAccount|group|user|principal|principalSet):", m))])
    error_message = "Give IAM members, such as serviceAccount:iki-production-nodes@<project>.iam.gserviceaccount.com."
  }
}

variable "writers" {
  description = "Who may push: your CI's service account, which publishes the images you validate."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for m in var.writers : can(regex("^(serviceAccount|group|user|principal|principalSet):", m))])
    error_message = "Give IAM members, such as serviceAccount:ci@<project>.iam.gserviceaccount.com."
  }
}

variable "labels" {
  description = "Labels on everything this module makes."
  type        = map(string)
  default     = {}
}
