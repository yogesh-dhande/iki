variable "name" {
  description = "The cluster's name, such as iki-production."
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
  description = "The region the cluster runs in."
  type        = string
}

variable "zones" {
  description = "The zones nodes spread over, by name, such as [\"europe-west1-b\", \"europe-west1-c\", \"europe-west1-d\"]. Three keep Iki running through the loss of one."
  type        = list(string)

  validation {
    condition     = length(var.zones) >= 2 && length(var.zones) <= 4 && length(distinct(var.zones)) == length(var.zones)
    error_message = "Name 2 to 4 different zones."
  }
}

variable "kubernetes_version" {
  description = "The least Kubernetes version, such as \"1.36\": GKE keeps the cluster at or above it within the release channel. 1.36 or later, so the cluster needs no default node pool."
  type        = string

  validation {
    condition     = can(regex("^1\\.[0-9]{2}$", var.kubernetes_version)) && tonumber(split(".", var.kubernetes_version)[1]) >= 36
    error_message = "Give a minor version of 1.36 or later, such as \"1.36\"."
  }
}

variable "release_channel" {
  description = "GKE's release channel: REGULAR, or STABLE to take versions later and after more use."
  type        = string
  default     = "REGULAR"

  validation {
    condition     = contains(["REGULAR", "STABLE"], var.release_channel)
    error_message = "Use REGULAR or STABLE: RAPID isn't for production."
  }
}

variable "network_id" {
  description = "The VPC, from the network module."
  type        = string
}

variable "subnet_id" {
  description = "The nodes' subnet, from the network module."
  type        = string
}

variable "pods_range_name" {
  description = "The subnet's secondary range for pods."
  type        = string
}

variable "services_range_name" {
  description = "The subnet's secondary range for Services."
  type        = string
}

variable "node_tag" {
  description = "The network tag nodes carry, which the network module's firewall rules name."
  type        = string
}

variable "secrets_kms_key" {
  description = "The key Kubernetes Secrets are encrypted with, from the kms module: in the cluster's region."
  type        = string
}

variable "nodes_kms_key" {
  description = "The key nodes' disks are encrypted with, from the kms module."
  type        = string
}

variable "public_access_cidrs" {
  description = "Networks that may reach the Kubernetes API's IP endpoint from the internet, such as your office's. Empty: its IP endpoint is private, reachable only inside the VPC (and networks routed to it)."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.public_access_cidrs, "0.0.0.0/0")
    error_message = "Don't open the Kubernetes API to the whole internet: list the networks that need it."
  }
}

variable "private_access_cidrs" {
  description = "Networks inside the VPC, or routed to it (a VPN, Interconnect), that may reach the API's private endpoint. Nodes always can."
  type        = list(string)
  default     = []
}

variable "dns_endpoint" {
  description = "Let the Kubernetes API's DNS endpoint be reached from outside the VPC. Every call to it needs a Google identity allowed container.clusters.connect, and IAM and Cloud Audit Logs cover it: Google's recommended way to reach a private cluster without a VPN."
  type        = bool
  default     = false
}

variable "admin_members" {
  description = "IAM members given GKE's admin role, such as your platform team's group (group:platform@example.com). Others need a role granted elsewhere."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for m in var.admin_members : can(regex("^(group|user|serviceAccount|principal|principalSet):", m))])
    error_message = "Give IAM members, such as group:platform@example.com."
  }
}

variable "node_pools" {
  description = <<-EOT
    Node pools. `system` runs Iki and the cluster's controllers. Every other pool is dedicated:
    tainted iki.ikisci.org/pool=<name>:NoSchedule, so only pods that tolerate it land there. Point
    backend workers and compute templates at `runs` (or `gpu`), so plugins' code never shares a
    node with Iki's own pods and their Secrets. Sizes are totals across the zones.
  EOT
  type = map(object({
    machine_type  = string
    min_size      = number
    max_size      = number
    disk_size_gib = optional(number, 100)
    disk_type     = optional(string, "pd-balanced")
    spot          = optional(bool, false)
    gpu = optional(object({
      type  = string
      count = number
    }))
  }))
  nullable = false
  default = {
    system = { machine_type = "n2-standard-4", min_size = 3, max_size = 6, disk_size_gib = 50 }
    runs   = { machine_type = "n2-standard-8", min_size = 1, max_size = 20, disk_size_gib = 200 }
    gpu    = { machine_type = "g2-standard-8", min_size = 0, max_size = 4, disk_size_gib = 200, gpu = { type = "nvidia-l4", count = 1 } }
  }

  validation {
    condition     = contains(keys(var.node_pools), "system")
    error_message = "Keep a pool named system: Iki and the cluster's controllers run there."
  }
  validation {
    condition     = alltrue([for p in values(var.node_pools) : p.min_size <= p.max_size])
    error_message = "Each pool's min_size must be at most its max_size."
  }
  validation {
    condition     = var.node_pools["system"].min_size >= 2 && !var.node_pools["system"].spot
    error_message = "The system pool needs at least 2 nodes, not spot, so Iki survives losing one."
  }
}

variable "managed_prometheus" {
  description = "Google Cloud Managed Service for Prometheus collects the cluster's metrics. Iki's own go over OTLP to your collector (ADR 0098)."
  type        = bool
  default     = true
}

variable "filestore_csi" {
  description = "Turn on GKE's Filestore CSI driver, for a shared file system's PersistentVolumes (the filestore module)."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Refuse to delete the cluster. Turn off only to tear a deployment down."
  type        = bool
  default     = true
}

variable "labels" {
  description = "Labels on everything this module makes."
  type        = map(string)
  default     = {}
}
