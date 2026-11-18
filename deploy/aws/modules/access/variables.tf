variable "name" {
  description = "Names everything this module makes, such as iki-production."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name))
    error_message = "Use 3 to 32 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "cluster_name" {
  description = "The EKS cluster, from the cluster module."
  type        = string
}

variable "cluster_arn" {
  description = "The cluster's ARN: each role trusts pods of this cluster only."
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
  description = "The files bucket and its key, from the storage module, and the prefix Iki writes under (the chart's files.prefix)."
  type = object({
    bucket_arn  = string
    kms_key_arn = string
    prefix      = optional(string, "")
  })
}

variable "register_locations" {
  description = "Buckets (and prefixes) where files may be registered in place: the chart's files.registerLocations. Iki only reads them. Give the key, too, if a bucket is encrypted with one."
  type = list(object({
    bucket_arn  = string
    prefix      = optional(string, "")
    kms_key_arn = optional(string)
  }))
  default = []
}

variable "external_secrets" {
  description = "External Secrets Operator's service account, and the secrets it may read: from the secrets module."
  type = object({
    namespace       = optional(string, "external-secrets")
    service_account = optional(string, "external-secrets")
    secret_arns     = list(string)
    kms_key_arn     = string
  })
}

variable "vm_tier" {
  description = "The VM tier (ADR 0074, 0076): the deployment tag its machines carry (the chart's vmTier.owner), and from the vm-tier module the machines' role and their disks' key, and the regions they start in. Null: no provisioner role."
  type = object({
    owner            = string
    worker_role_arn  = string
    disk_kms_key_arn = string
    regions          = list(string)
  })
  default = null
}

variable "load_balancer_controller" {
  description = "A role for the AWS Load Balancer Controller, which makes the ALB in front of Iki from its Ingress."
  type = object({
    enabled         = optional(bool, true)
    namespace       = optional(string, "kube-system")
    service_account = optional(string, "aws-load-balancer-controller")
  })
  default = {}
}

variable "cluster_autoscaler" {
  description = "A role for the Cluster Autoscaler, which grows and shrinks the node pools within their limits."
  type = object({
    enabled         = optional(bool, true)
    namespace       = optional(string, "kube-system")
    service_account = optional(string, "cluster-autoscaler")
  })
  default = {}
}

variable "tags" {
  description = "Tags on everything this module makes."
  type        = map(string)
  default     = {}
}

variable "external_dns" {
  description = "A role for ExternalDNS, which names the load balancer in a private hosted zone: the zone's ARN and ExternalDNS's service account (null: no role). Whether it is null is known when planning, though the ARN of a zone made in the same apply isn't."
  type = object({
    zone_arn        = string
    namespace       = optional(string, "kube-system")
    service_account = optional(string, "external-dns")
  })
  default = null
}
