variable "name" {
  description = "This deployment's name: it prefixes everything made, and tags its VM tier's machines."
  type        = string
  default     = "iki-production"
}

variable "region" {
  description = "Where Iki runs, such as eu-west-1."
  type        = string
}

variable "dr_region" {
  description = "Where the database's backups, the files and the secrets are copied, to recover from losing region."
  type        = string

  validation {
    condition     = var.dr_region != var.region
    error_message = "The DR region must differ from the region Iki runs in."
  }
}

variable "zones" {
  description = "Zones in region to spread over, by name (eu-west-1a, ...): three for production."
  type        = list(string)
}

variable "vpc_cidr" {
  description = "The VPC's range. Pick one your other networks don't use, if they'll ever be peered."
  type        = string
  default     = "10.40.0.0/16"
}

variable "kubernetes_version" {
  description = "The EKS version, such as \"1.35\": one in standard support."
  type        = string
}

variable "admin_principal_arns" {
  description = "IAM roles that administer the cluster: your platform team's, and your CI's if it deploys."
  type        = list(string)
}

variable "public_access_cidrs" {
  description = "Networks that may reach the Kubernetes API from outside the VPC, such as your office's. Empty: from inside the VPC only."
  type        = list(string)
  default     = []
}

variable "node_pools" {
  description = "The cluster module's node pools, if not its defaults (system, runs, gpu)."
  type        = any
  default     = null
}

variable "state_kms_key_arn" {
  description = "The key the state is encrypted with: the bootstrap's state_kms_key_arn output."
  type        = string
}

variable "files_prefix" {
  description = "Where in the files bucket Iki writes: the chart's files.prefix. Empty: anywhere."
  type        = string
  default     = ""
}

variable "register_locations" {
  description = "Buckets (and prefixes) where files may be registered in place: the access module's register_locations."
  type = list(object({
    bucket_arn  = string
    prefix      = optional(string, "")
    kms_key_arn = optional(string)
  }))
  default = []
}

variable "optional_secrets" {
  description = "Secrets for features you turn on: email, backend-worker-token."
  type        = set(string)
  default     = []
}

variable "vm_tier" {
  description = "Turn on the VM tier (ADR 0074): the ECR repositories its machines pull from (Iki's worker and your plugins' images), and other networks they reach on 443."
  type = object({
    ecr_repository_arns = optional(list(string), [])
    extra_https_cidrs   = optional(list(string), [])
  })
  default = null
}

variable "fsx" {
  description = "Add FSx for Lustre for shared reference data (ADR 0077): its size, and a bucket to link read-only at /reference."
  type = object({
    storage_capacity_gib = optional(number, 2400)
    throughput_per_tib   = optional(number, 250)
    reference_bucket = optional(object({
      arn    = string
      prefix = optional(string, "")
    }))
    root_squash_except_nids = optional(list(string), [])
  })
  default = null
}

variable "tags" {
  description = "Tags on everything made, such as your cost centre's."
  type        = map(string)
  default     = {}
}

variable "public_host" {
  description = "Iki's host name, such as iki.example.com: the chart's publicUrl and allowedHosts, and the ALB's certificate's."
  type        = string
}

variable "certificate_arn" {
  description = "The ACM certificate the ALB serves for public_host."
  type        = string
}

variable "internal" {
  description = "Put the ALB in the private subnets, reachable only from your network (through a VPN, Direct Connect or peering), rather than the internet."
  type        = bool
  default     = false
}

variable "ingress_cidrs" {
  description = "Networks that may reach the ALB, such as your company's egress ranges. Everyone, by default: Iki signs people in itself."
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = length(var.ingress_cidrs) > 0 && alltrue([for c in var.ingress_cidrs : can(cidrhost(c, 0))])
    error_message = "Give at least one network, in CIDR notation."
  }
}

variable "waf_acl_arn" {
  description = "A WAFv2 web ACL to put in front of the ALB, if you have one."
  type        = string
  default     = null
}
