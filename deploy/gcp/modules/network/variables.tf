variable "name" {
  description = "Names everything this module makes, such as iki-production."
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
  description = "Where the subnets, router and NAT are, such as europe-west1."
  type        = string
}

variable "cidr" {
  description = "The range for nodes, the VM tier's machines, internal load balancers' proxies and Google's managed services (Cloud SQL, Filestore). A /16 leaves room for each."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.cidr, 0)) && tonumber(split("/", var.cidr)[1]) <= 16
    error_message = "Give a CIDR block of /16 or larger, such as 10.40.0.0/16."
  }
}

variable "pods_cidr" {
  description = "Pods' addresses: GKE gives each node a /24 of it. A /14 is room for 1,024 nodes."
  type        = string
  default     = "10.44.0.0/14"

  validation {
    condition     = can(cidrhost(var.pods_cidr, 0)) && tonumber(split("/", var.pods_cidr)[1]) <= 18
    error_message = "Give a CIDR block of /18 or larger for pods."
  }
}

variable "services_cidr" {
  description = "Kubernetes Services' addresses."
  type        = string
  default     = "10.48.0.0/20"

  validation {
    condition     = can(cidrhost(var.services_cidr, 0)) && tonumber(split("/", var.services_cidr)[1]) <= 24
    error_message = "Give a CIDR block of /24 or larger for Services."
  }
}

variable "nat_addresses" {
  description = "How many fixed addresses Cloud NAT sends from. Each serves about 64,000 connections at once."
  type        = number
  default     = 2

  validation {
    condition     = var.nat_addresses >= 1 && var.nat_addresses <= 16
    error_message = "Use 1 to 16 addresses."
  }
}

variable "node_egress" {
  description = "Where the cluster's nodes may go outside the VPC and Google's APIs, through Cloud NAT: HTTPS anywhere by default, for the egress proxy, the identity provider, webhooks and public registries. Narrow it to your providers' ranges if you can, and add the mail server's ports (587, 465) if Iki sends email."
  type = list(object({
    name  = string
    ports = list(number)
    cidrs = list(string)
  }))
  default = [{ name = "https", ports = [443], cidrs = ["0.0.0.0/0"] }]

  validation {
    condition     = alltrue([for r in var.node_egress : can(regex("^[a-z][a-z0-9-]{0,20}$", r.name)) && length(r.ports) > 0 && alltrue([for c in r.cidrs : can(cidrhost(c, 0))])])
    error_message = "Each rule needs a short lowercase name, at least one port, and networks in CIDR notation."
  }
}

variable "flow_log_sampling" {
  description = "The share of flows VPC flow logs record, from 0 to 1. Every flow by default, as on AWS; less costs less."
  type        = number
  default     = 1

  validation {
    condition     = var.flow_log_sampling > 0 && var.flow_log_sampling <= 1
    error_message = "Sample more than 0 and at most 1."
  }
}

variable "labels" {
  description = "Labels on everything this module makes that takes them."
  type        = map(string)
  default     = {}
}
