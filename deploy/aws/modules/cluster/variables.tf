variable "name" {
  description = "The cluster's name, such as iki-production."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name))
    error_message = "Use 3 to 32 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "kubernetes_version" {
  description = "The Kubernetes version, one EKS supports in standard support, such as \"1.35\". Required, so a default never goes stale under you."
  type        = string

  validation {
    condition     = can(regex("^1\\.[0-9]{2}$", var.kubernetes_version))
    error_message = "Give a minor version, such as \"1.35\"."
  }
}

variable "vpc_id" {
  description = "The VPC, from the network module."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets in two or more zones for the nodes and the API's network interfaces."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "EKS needs subnets in at least two zones."
  }
}

variable "public_access_cidrs" {
  description = "Networks that may reach the Kubernetes API from the internet, such as your office's or a CI runner's. Empty: the API is reachable only inside the VPC (and networks routed to it)."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.public_access_cidrs, "0.0.0.0/0")
    error_message = "Don't open the Kubernetes API to the whole internet: list the networks that need it."
  }
}

variable "admin_principal_arns" {
  description = "IAM roles (or users) given cluster-admin, such as your platform team's SSO role. Nobody else is, not even whoever creates the cluster."
  type        = list(string)
  default     = []
}

variable "node_pools" {
  description = <<-EOT
    Managed node groups. `system` runs Iki and the cluster's controllers. Every other pool is
    dedicated: tainted iki.ikisci.org/pool=<name>:NoSchedule, so only pods that tolerate it land
    there. Point backend workers and compute templates at `runs` (or `gpu`), so plugins' code
    never shares a node with Iki's own pods and their Secrets.
  EOT
  type = map(object({
    instance_types = list(string)
    min_size       = number
    max_size       = number
    desired_size   = optional(number)
    disk_size_gib  = optional(number, 100)
    gpu            = optional(bool, false)
    capacity_type  = optional(string, "ON_DEMAND")
  }))
  default = {
    system = { instance_types = ["m7i.xlarge"], min_size = 3, max_size = 6, disk_size_gib = 50 }
    runs   = { instance_types = ["m7i.2xlarge"], min_size = 1, max_size = 20, disk_size_gib = 200 }
    gpu    = { instance_types = ["g6.xlarge"], min_size = 0, max_size = 4, disk_size_gib = 200, gpu = true }
  }

  validation {
    condition     = contains(keys(var.node_pools), "system")
    error_message = "Keep a pool named system: Iki and the cluster's controllers run there."
  }
  validation {
    condition     = alltrue([for p in values(var.node_pools) : p.min_size <= p.max_size && contains(["ON_DEMAND", "SPOT"], p.capacity_type)])
    error_message = "Each pool's min_size must be at most its max_size, and capacity_type ON_DEMAND or SPOT."
  }
  validation {
    condition     = var.node_pools["system"].min_size >= 2 && var.node_pools["system"].capacity_type == "ON_DEMAND"
    error_message = "The system pool needs at least 2 on-demand nodes, so Iki survives losing one."
  }
}

variable "log_retention_days" {
  description = "How long the control plane's logs (API, audit, authenticator, controller manager, scheduler) are kept."
  type        = number
  default     = 365
}

variable "tags" {
  description = "Tags on everything this module makes."
  type        = map(string)
  default     = {}
}
