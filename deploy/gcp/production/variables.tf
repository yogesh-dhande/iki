variable "name" {
  description = "This deployment's name: it prefixes everything made, and names its VM tier's machines."
  type        = string
  default     = "iki-production"
  # Service accounts are named <name>-provisioner, at most 30 characters, and the VM
  # tier's machines <name>-..., which the provisioner's IAM condition matches.
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,16}[a-z0-9]$", var.name))
    error_message = "name must be 3 to 18 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "project_id" {
  description = "The project Iki runs in. Give each deployment its own: GKE's and Compute Engine's IAM roles are project-wide."
  type        = string
}

variable "region" {
  description = "Where Iki runs, such as europe-west1."
  type        = string
}

variable "dr_region" {
  description = "Where the database's replica, the files' second copy and the secrets' second copy are, to recover from losing region. In the same multi_region as region."
  type        = string

  validation {
    condition     = var.dr_region != var.region
    error_message = "The DR region must differ from the region Iki runs in."
  }
}

variable "multi_region" {
  description = "The multi-region holding both regions (europe, us or asia): the files bucket is a dual-region in it, and its key lives there."
  type        = string

  validation {
    condition     = contains(["europe", "us", "asia"], var.multi_region)
    error_message = "Use europe, us or asia."
  }
}

variable "zones" {
  description = "Zones in region to spread nodes over, by name (europe-west1-b, ...): three for production."
  type        = list(string)
}

variable "vpc_cidr" {
  description = "The VPC's range for nodes, machines and managed services. Pick one your other networks don't use, if they'll ever be peered."
  type        = string
  default     = "10.40.0.0/16"
}

variable "pods_cidr" {
  description = "Pods' range."
  type        = string
  default     = "10.44.0.0/14"
}

variable "services_cidr" {
  description = "Kubernetes Services' range."
  type        = string
  default     = "10.48.0.0/20"
}

variable "node_egress" {
  description = "Where nodes may go outside the VPC: the network module's node_egress. HTTPS anywhere by default; add your mail server's ports if Iki sends email."
  type = list(object({
    name  = string
    ports = list(number)
    cidrs = list(string)
  }))
  default = [{ name = "https", ports = [443], cidrs = ["0.0.0.0/0"] }]
}

variable "kubernetes_version" {
  description = "The least GKE version, such as \"1.36\"."
  type        = string
}

variable "release_channel" {
  description = "GKE's release channel: REGULAR or STABLE."
  type        = string
  default     = "REGULAR"
}

variable "admin_members" {
  description = "IAM members that administer the cluster: your platform team's group, and your CI's service account if it deploys."
  type        = list(string)
}

variable "public_access_cidrs" {
  description = "Networks that may reach the Kubernetes API's IP endpoint from outside the VPC, such as your office's. Empty: from inside the VPC only."
  type        = list(string)
  default     = []
}

variable "private_access_cidrs" {
  description = "Networks routed to the VPC (a VPN, Interconnect) that may reach the API's private endpoint."
  type        = list(string)
  default     = []
}

variable "dns_endpoint" {
  description = "Reach the Kubernetes API through its DNS endpoint from outside the VPC, with IAM deciding who may: the simplest way in without a VPN."
  type        = bool
  default     = false
}

variable "node_pools" {
  description = "The cluster module's node pools, if not its defaults (system, runs, gpu)."
  type        = any
  default     = null
}

variable "state_kms_key" {
  description = "The key the state is encrypted with: the bootstrap's state_kms_key output."
  type        = string
}

variable "kms_protection_level" {
  description = "SOFTWARE, or HSM to keep every key in Cloud HSM."
  type        = string
  default     = "SOFTWARE"
}

variable "database_tier" {
  description = "Cloud SQL's machine, Enterprise Plus: db-perf-optimized-N-<vCPUs>."
  type        = string
  default     = "db-perf-optimized-N-4"
}

variable "database_replica" {
  description = "Keep a replica of the database in dr_region, to recover from losing region."
  type        = bool
  default     = true
}

variable "database_iam_principals" {
  description = "People or groups who sign in to Postgres with IAM, such as your DBAs: the database module's iam_principals."
  type = list(object({
    email = string
    type  = optional(string, "group")
  }))
  default = []
}

variable "turbo_replication" {
  description = "Turbo replication for the files bucket: every new file in both regions within 15 minutes, for a fee per GB written."
  type        = bool
  default     = false
}

variable "files_prefix" {
  description = "Where in the files bucket Iki writes: the chart's files.prefix. Empty: anywhere."
  type        = string
  default     = ""
}

variable "register_locations" {
  description = "Buckets (and prefixes) where files may be registered in place: the access module's register_locations."
  type = list(object({
    bucket = string
    prefix = optional(string, "")
  }))
  default = []
}

variable "optional_secrets" {
  description = "Secrets for features you turn on: email, backend-worker-token."
  type        = set(string)
  default     = []
}

variable "image_writers" {
  description = "IAM members that push to the image repositories: your CI's service account."
  type        = list(string)
  default     = []
}

variable "plugin_images" {
  description = "Where plugins' environment images come from, besides the plugins repository this makes, as Binary Authorization's patterns (a trailing * is within one level, ** below it too). Iki's first-party plugins' by default."
  type        = list(string)
  default     = ["ghcr.io/yogesh-dhande/iki-*"]
}

variable "extra_images" {
  description = "Any other images the cluster may run, such as kube-prometheus-stack's or an OpenTelemetry Collector's, as Binary Authorization's patterns. GKE's own are allowed already."
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  description = "How long the deployment's audit, control-plane, network and database logs are kept."
  type        = number
  default     = 365
}

variable "vm_tier" {
  description = "The VM tier on Compute Engine (ADR 0074, 0109): the machines' service account, instance template and firewalls, the provisioner's identity, and the chart's vmTier. server_url is where machines reach Iki from the VPC (an https:// address; publicUrl by default), and extra_https_cidrs what else they may reach."
  type = object({
    server_url        = optional(string, "")
    extra_https_cidrs = optional(list(string), [])
  })
  validation {
    condition     = var.vm_tier == null || can(regex("^(https://.+)?$", var.vm_tier.server_url))
    error_message = "vm_tier.server_url must be an https:// address: machines send their token to it."
  }
  default = null
}

variable "filestore" {
  description = "Add Filestore for shared reference data (ADR 0077): its tier, size, and machines whose root isn't squashed, to make /iki once."
  type = object({
    tier                     = optional(string, "REGIONAL")
    capacity_gib             = optional(number, 1024)
    root_squash_except_cidrs = optional(list(string), [])
  })
  default = null
}

variable "recovery" {
  description = "Set only to stand Iki up again here after losing the region it ran in (the README): the lost deployment's name and region. Its database's replica, promoted first, becomes this one's database, and its files bucket, in the lost region and this one, this one's. region is the lost deployment's DR region; dr_region a third region, since nothing can be made in the lost one. Keep it set as long as this deployment runs."
  type = object({
    name        = string
    lost_region = string
  })
  default = null

  validation {
    condition     = var.recovery == null || try(var.recovery.name != var.name, false)
    error_message = "Recover under a new name, such as iki-production-r1: the lost deployment's service accounts, keys and secrets still exist."
  }
  validation {
    condition     = var.recovery == null || try(!contains([var.region, var.dr_region], var.recovery.lost_region), false)
    error_message = "Recover in the lost deployment's DR region, with dr_region a third region in the same multi_region: nothing can be made in the lost one while it's down."
  }
}

variable "data_access_services" {
  description = "Services whose Data Access audit logs are turned on (the logs module's): reads and writes of files, secrets, keys and the database's settings. They're project-wide, for every workload in the project, and each service named is set authoritatively."
  type        = list(string)
  default     = ["storage.googleapis.com", "secretmanager.googleapis.com", "cloudkms.googleapis.com", "sqladmin.googleapis.com"]
}

variable "deletion_protection" {
  description = "Refuse to delete the cluster, the database and the secrets. Set false and apply, then destroy, only to tear the deployment down (scripts/teardown.sh)."
  type        = bool
  default     = true
}

variable "labels" {
  description = "Labels on everything made, such as your cost centre's."
  type        = map(string)
  default     = {}
}

variable "public_host" {
  description = "Iki's host name, such as iki.example.com: the chart's publicUrl and allowedHosts, and the load balancer's certificate's."
  type        = string
}

variable "internal" {
  description = "Serve Iki from an internal load balancer in the VPC, reachable only from your network (through a VPN, Interconnect or peering), rather than the internet."
  type        = bool
  default     = false
}

variable "internal_certificate" {
  description = "With internal: the regional SSL certificate (by name, in region) the internal load balancer serves, which you upload: Google-managed certificates need a public load balancer."
  type        = string
  default     = null

  validation {
    condition     = !var.internal || var.internal_certificate != null
    error_message = "An internal load balancer needs internal_certificate: a regional SSL certificate you upload for public_host."
  }
}

variable "internal_dns" {
  description = "With internal: a private Cloud DNS zone for public_host alone, in the VPC, at the internal load balancer's address, so the cluster's workers and the VM tier's machines reach publicUrl. Turn off if your own DNS already resolves public_host inside the VPC."
  type        = bool
  default     = true
}

variable "ingress_cidrs" {
  description = "Networks that may reach the load balancer, such as your company's egress ranges, enforced by a Cloud Armor policy. Everyone, by default: Iki signs people in itself."
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = length(var.ingress_cidrs) > 0 && alltrue([for c in var.ingress_cidrs : can(cidrhost(c, 0))])
    error_message = "Give at least one network, in CIDR notation."
  }
}

variable "security_policy" {
  description = "A Cloud Armor security policy of your own to put in front of Iki (a WAF's rules, say), by name, instead of the allowlist made from ingress_cidrs."
  type        = string
  default     = null
}
