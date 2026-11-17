variable "name" {
  description = "Names everything this module makes, such as iki-production: the chart's vmTier.owner."
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
  description = "Where machines start: the instance template's region."
  type        = string
}

variable "network_id" {
  description = "The VPC, from the network module."
  type        = string
}

variable "subnet_id" {
  description = "The subnet machines start in, from the network module."
  type        = string
}

variable "vpc_cidr" {
  description = "The VPC's range: machines reach Iki's server and the egress proxy in it."
  type        = string
}

variable "network_tag" {
  description = "The network tag machines carry, from the network module (vm_tag): its rule for Google's APIs names it."
  type        = string
}

variable "kms_key" {
  description = "The key for the machines' disks, and the images setup builds make, from the kms module (vm-disks)."
  type        = string
}

variable "machine_type" {
  description = "The template's machine type: a starting point the provisioner replaces with each compute template's."
  type        = string
  default     = "n2-standard-8"
}

variable "image" {
  description = "The machines' boot image: Container-Optimized OS, which runs the worker's host and Docker, by default."
  type        = string
  default     = "projects/cos-cloud/global/images/family/cos-stable"
}

variable "nfs_cidrs" {
  description = "Shared file systems machines mount over NFS (ADR 0077), by their addresses: a Filestore instance's, from the filestore module."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.nfs_cidrs : can(cidrhost(c, 0))])
    error_message = "Give networks in CIDR notation."
  }
}

variable "extra_https_cidrs" {
  description = "Other networks machines may reach on 443, such as a public registry's (through Cloud NAT). Runs reach everything else through the egress proxy."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.extra_https_cidrs : can(cidrhost(c, 0))])
    error_message = "Give networks in CIDR notation."
  }
}

variable "labels" {
  description = "Labels on everything this module makes."
  type        = map(string)
  default     = {}
}
