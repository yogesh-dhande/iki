# Iki is served through Google's Application Load Balancer, which GKE's Gateway controller
# makes from the Gateway this deployment's gateway output holds: HTTPS only, TLS 1.2 or
# later with modern ciphers, sending straight to web pods (container-native), from a
# fixed address, with Cloud Armor allowing only the networks listed. These are the parts
# that outlive the cluster.

locals {
  # Google's front ends and health checkers, which reach web pods for an external load
  # balancer: the chart's NetworkPolicy admits them.
  google_front_ends = ["35.191.0.0/16", "130.211.0.0/22"]
  allowlist         = !var.internal && var.security_policy == null && !contains(var.ingress_cidrs, "0.0.0.0/0")
  security_policy   = var.security_policy != null ? var.security_policy : (local.allowlist ? google_compute_security_policy.allowlist[0].name : null)
}

# --- An external load balancer (the default) -----------------------------------------------

resource "google_compute_global_address" "web" {
  count       = var.internal ? 0 : 1
  project     = var.project_id
  name        = "${var.name}-web"
  description = "${var.name}: Iki's address; point ${var.public_host} at it"
  labels      = merge(var.labels, { iki-deployment = var.name })
}

# Google issues and renews it once public_host points at the address above.
resource "google_compute_managed_ssl_certificate" "web" {
  count   = var.internal ? 0 : 1
  project = var.project_id
  name    = "${var.name}-web"
  managed {
    domains = [var.public_host]
  }
}

resource "google_compute_ssl_policy" "web" {
  count           = var.internal ? 0 : 1
  project         = var.project_id
  name            = "${var.name}-web"
  description     = "${var.name}: TLS 1.2 or later, modern ciphers only"
  min_tls_version = "TLS_1_2"
  profile         = "RESTRICTED"
}

# Only the networks listed reach Iki; everyone else is refused at Google's edge.
resource "google_compute_security_policy" "allowlist" {
  count       = local.allowlist ? 1 : 0
  project     = var.project_id
  name        = "${var.name}-web"
  description = "${var.name}: only the networks in ingress_cidrs"

  dynamic "rule" {
    # Cloud Armor takes 10 networks a rule.
    for_each = chunklist(var.ingress_cidrs, 10)
    content {
      action      = "allow"
      priority    = 1000 + rule.key
      description = "Allowed networks"
      match {
        versioned_expr = "SRC_IPS_V1"
        config {
          src_ip_ranges = rule.value
        }
      }
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 2147483647
    description = "Everyone else"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

# --- Or an internal one --------------------------------------------------------------------

resource "google_compute_address" "web" {
  count        = var.internal ? 1 : 0
  project      = var.project_id
  name         = "${var.name}-web"
  description  = "${var.name}: Iki's internal address; point ${var.public_host} at it"
  region       = var.region
  address_type = "INTERNAL"
  subnetwork   = module.network.subnet_id
  purpose      = "SHARED_LOADBALANCER_VIP"
  labels       = merge(var.labels, { iki-deployment = var.name })
}

resource "google_compute_region_ssl_policy" "web" {
  count           = var.internal ? 1 : 0
  project         = var.project_id
  name            = "${var.name}-web"
  description     = "${var.name}: TLS 1.2 or later, modern ciphers only"
  region          = var.region
  min_tls_version = "TLS_1_2"
  profile         = "RESTRICTED"
}
