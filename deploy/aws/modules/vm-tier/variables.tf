variable "name" {
  description = "Names everything this module makes, such as iki-production: the chart's vmTier.owner, which every machine is tagged with."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name))
    error_message = "Use 3 to 32 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "vpc_id" {
  description = "The VPC machines start in, from the network module."
  type        = string
}

variable "vpc_cidr" {
  description = "The VPC's range: machines reach Iki's server, the egress proxy and AWS's interface endpoints in it."
  type        = string
}

variable "s3_prefix_list_id" {
  description = "The S3 gateway endpoint's prefix list, from the network module: machines read images' layers and stored files through it."
  type        = string
}

variable "file_systems" {
  description = "Shared file systems machines mount (ADR 0077), by their security groups: nfs (2049, such as EFS) or lustre (988 and 1018-1023, such as FSx for Lustre)."
  type = list(object({
    kind              = string
    security_group_id = string
  }))
  default = []

  validation {
    condition     = alltrue([for f in var.file_systems : contains(["nfs", "lustre"], f.kind)])
    error_message = "A file system is nfs or lustre."
  }
}

variable "extra_https_cidrs" {
  description = "Other networks machines may reach on 443, such as a public registry's (through the NAT gateways). Runs reach everything else through the egress proxy."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.extra_https_cidrs : can(cidrhost(c, 0))])
    error_message = "Give networks in CIDR notation."
  }
}

variable "ecr_repository_arns" {
  description = "The ECR repositories machines pull images from: Iki's worker and your plugins' images. Empty: none, for images in a public registry."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags on everything this module makes."
  type        = map(string)
  default     = {}
}
