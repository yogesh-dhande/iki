variable "name" {
  description = "Names the instance, such as iki-production."
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
  description = "Where it is: the cluster's region."
  type        = string
}

variable "zone" {
  description = "Its zone, for the ZONAL tier only."
  type        = string
  default     = null
}

variable "network_id" {
  description = "The VPC, from the network module: Filestore takes an address in its range for Google's managed services."
  type        = string
}

variable "kms_key" {
  description = "Its key, from the kms module (filestore)."
  type        = string
}

variable "tier" {
  description = "REGIONAL (survives losing a zone), or ZONAL (faster, in one zone)."
  type        = string
  default     = "REGIONAL"

  validation {
    condition     = contains(["REGIONAL", "ZONAL"], var.tier)
    error_message = "Use REGIONAL or ZONAL: they offer customer-managed keys and NFSv4.1."
  }
}

variable "capacity_gib" {
  description = "Its size: from 1024 GiB."
  type        = number
  default     = 1024

  validation {
    condition     = var.capacity_gib >= 1024
    error_message = "Regional and zonal Filestore start at 1024 GiB."
  }
}

variable "client_cidrs" {
  description = "Who mounts it: the nodes' and VM tier's subnet."
  type        = list(string)

  validation {
    condition     = length(var.client_cidrs) > 0 && alltrue([for c in var.client_cidrs : can(cidrhost(c, 0))])
    error_message = "Name at least one network, in CIDR notation, that mounts it."
  }
}

variable "root_squash_except_cidrs" {
  description = "Machines whose root isn't squashed, such as one address that makes /iki for runs' user. Take them off once it's made."
  type        = list(string)
  default     = []
}

variable "labels" {
  description = "Labels on the instance."
  type        = map(string)
  default     = {}
}
