# A VPC for one deployment (ADR 0107): a subnet for nodes, pods, services and the VM tier,
# all private; Cloud NAT out to the internet from fixed addresses; Google's APIs reached
# at their private addresses; a range peered for Cloud SQL and Filestore; and firewalls
# that deny everything, both ways, that isn't allowed below. Nothing has a public address
# but the load balancer and the NAT.

locals {
  # The nodes' subnet (and the VM tier's machines), the proxy-only subnet internal load
  # balancers send from, and the range Google's managed services (Cloud SQL, Filestore)
  # are given addresses in, all inside cidr. Pods and services have ranges of their own.
  nodes_cidr = cidrsubnet(var.cidr, 4, 0)
  proxy_cidr = cidrsubnet(var.cidr, 7, 16)
  psa_cidr   = cidrsubnet(var.cidr, 4, 15)
  internal   = [var.cidr, var.pods_cidr, var.services_cidr]
  # private.googleapis.com: Google's APIs, reached through Private Google Access.
  google_apis = "199.36.153.8/30"
  node_tag    = "${var.name}-nodes"
  vm_tag      = "${var.name}-vm"
}

resource "google_compute_network" "this" {
  project                 = var.project_id
  name                    = var.name
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  # Keep the default route: Private Google Access and Cloud NAT both leave by it.
  delete_default_routes_on_create = false
}

resource "google_compute_subnetwork" "nodes" {
  project                  = var.project_id
  name                     = "${var.name}-nodes"
  region                   = var.region
  network                  = google_compute_network.this.id
  ip_cidr_range            = local.nodes_cidr
  private_ip_google_access = true
  stack_type               = "IPV4_ONLY"

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pods_cidr
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.services_cidr
  }

  # Every flow, accepted or not, with its metadata (CIS Google Cloud 3.8).
  log_config {
    aggregation_interval = "INTERVAL_1_MIN"
    flow_sampling        = var.flow_log_sampling
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# Where an internal Application Load Balancer's proxies send from (internal = true).
resource "google_compute_subnetwork" "proxy" {
  project       = var.project_id
  name          = "${var.name}-proxy"
  region        = var.region
  network       = google_compute_network.this.id
  ip_cidr_range = local.proxy_cidr
  purpose       = "REGIONAL_MANAGED_PROXY"
  role          = "ACTIVE"
}

# --- Out to the internet -----------------------------------------------------------------

# Fixed addresses, so other systems can allowlist where Iki's traffic comes from. Cloud
# NAT is regional and spans every zone itself: losing one doesn't cut the others off.
resource "google_compute_address" "nat" {
  count   = var.nat_addresses
  project = var.project_id
  name    = "${var.name}-nat-${count.index}"
  region  = var.region
  labels  = merge(var.labels, { iki-deployment = var.name })
}

resource "google_compute_router" "this" {
  project = var.project_id
  name    = var.name
  region  = var.region
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  project                            = var.project_id
  name                               = var.name
  region                             = var.region
  router                             = google_compute_router.this.name
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = google_compute_address.nat[*].self_link
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"
  enable_dynamic_port_allocation     = true
  min_ports_per_vm                   = 64

  subnetwork {
    name                    = google_compute_subnetwork.nodes.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# --- Google's APIs, at their private addresses ---------------------------------------------

# *.googleapis.com and Artifact Registry's names resolve to private.googleapis.com inside
# the VPC, so calls to Google stay on Google's network and the firewall can name them.
resource "google_dns_managed_zone" "google_apis" {
  for_each    = toset(["googleapis.com.", "pkg.dev.", "gcr.io."])
  project     = var.project_id
  name        = "${var.name}-${replace(trimsuffix(each.key, "."), ".", "-")}"
  dns_name    = each.key
  description = "${var.name}: ${trimsuffix(each.key, ".")} through Private Google Access"
  visibility  = "private"
  labels      = merge(var.labels, { iki-deployment = var.name })

  private_visibility_config {
    networks {
      network_url = google_compute_network.this.id
    }
  }
}

resource "google_dns_record_set" "google_apis" {
  for_each     = google_dns_managed_zone.google_apis
  project      = var.project_id
  managed_zone = each.value.name
  name         = each.value.dns_name
  type         = "A"
  ttl          = 300
  rrdatas      = [for i in range(4) : cidrhost(local.google_apis, i)]
}

resource "google_dns_record_set" "google_apis_wildcard" {
  for_each     = google_dns_managed_zone.google_apis
  project      = var.project_id
  managed_zone = each.value.name
  name         = "*.${each.value.dns_name}"
  type         = "CNAME"
  ttl          = 300
  rrdatas      = [each.value.dns_name]
}

# --- Google's managed services: Cloud SQL and Filestore --------------------------------------

resource "google_compute_global_address" "services" {
  project       = var.project_id
  name          = "${var.name}-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  address       = cidrhost(local.psa_cidr, 0)
  prefix_length = tonumber(split("/", local.psa_cidr)[1])
  network       = google_compute_network.this.id
}

resource "google_service_networking_connection" "services" {
  network                 = google_compute_network.this.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.services.name]
  # Google deletes the peering itself once no instance uses it; deleting it first fails.
  deletion_policy = "ABANDON"
}

# --- Firewalls ------------------------------------------------------------------------------

# Nothing in, and nothing out, unless a rule below allows it; both are logged. Google
# Cloud already denies ingress implicitly, but not egress, and logs neither.
resource "google_compute_firewall" "deny_ingress" {
  project       = var.project_id
  name          = "${var.name}-deny-all-ingress"
  network       = google_compute_network.this.id
  description   = "Nothing in that a rule doesn't allow"
  direction     = "INGRESS"
  priority      = 65534
  source_ranges = ["0.0.0.0/0"]
  deny {
    protocol = "all"
  }
  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "deny_egress" {
  project            = var.project_id
  name               = "${var.name}-deny-all-egress"
  network            = google_compute_network.this.id
  description        = "Nothing out that a rule doesn't allow"
  direction          = "EGRESS"
  priority           = 65534
  destination_ranges = ["0.0.0.0/0"]
  deny {
    protocol = "all"
  }
  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

# Nodes reach each other, pods, services, the control plane's private endpoint (in the
# nodes' subnet) and Google's managed services. GKE adds its own ingress rules for these.
resource "google_compute_firewall" "nodes_internal" {
  project            = var.project_id
  name               = "${var.name}-nodes-internal"
  network            = google_compute_network.this.id
  description        = "The cluster's nodes and pods, to the VPC and its managed services"
  direction          = "EGRESS"
  priority           = 1000
  target_tags        = [local.node_tag]
  destination_ranges = concat(local.internal, [local.psa_cidr])
  allow {
    protocol = "all"
  }
  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "google_apis" {
  project            = var.project_id
  name               = "${var.name}-google-apis"
  network            = google_compute_network.this.id
  description        = "Nodes and the VM tier's machines, to Google's APIs at private.googleapis.com"
  direction          = "EGRESS"
  priority           = 1000
  target_tags        = [local.node_tag, local.vm_tag]
  destination_ranges = [local.google_apis]
  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

# The cluster's way out, through Cloud NAT: the egress proxy, the identity provider,
# webhooks, public registries. The chart's NetworkPolicies decide which pods may use it.
resource "google_compute_firewall" "nodes_out" {
  for_each           = { for r in var.node_egress : r.name => r }
  project            = var.project_id
  name               = "${var.name}-nodes-${each.key}"
  network            = google_compute_network.this.id
  description        = "The cluster's nodes, out: ${each.key}"
  direction          = "EGRESS"
  priority           = 1000
  target_tags        = [local.node_tag]
  destination_ranges = each.value.cidrs
  allow {
    protocol = "tcp"
    ports    = [for p in each.value.ports : tostring(p)]
  }
  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}
