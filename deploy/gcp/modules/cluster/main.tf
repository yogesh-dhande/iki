# GKE with private nodes (ADR 0107): regional, so its control plane and nodes span three
# zones; Kubernetes Secrets encrypted with a key of their own; Dataplane V2 enforcing
# NetworkPolicies; Workload Identity, so pods never see a node's credentials; shielded
# nodes on Container-Optimized OS; and every control-plane log kept.

locals {
  labels = merge(var.labels, { iki-deployment = var.name })
}

# --- The nodes' service account ------------------------------------------------------------

# Only what a node itself needs: to write its logs and metrics, and to pull images (the
# artifact-registry module grants that, on its repositories alone). Pods get their own
# identities through Workload Identity, never this one.
resource "google_service_account" "nodes" {
  project      = var.project_id
  account_id   = "${var.name}-nodes"
  display_name = "${var.name}: GKE nodes"
  description  = "The cluster's nodes: logs, metrics and pulling images, nothing else"
}

resource "google_project_iam_member" "nodes" {
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = google_service_account.nodes.member
}

# Who administers clusters in this project (GKE's IAM roles are project-wide: give each
# deployment a project of its own). Nobody else gets in, not even whoever applies this.
resource "google_project_iam_member" "admins" {
  for_each = toset(var.admin_members)
  project  = var.project_id
  role     = "roles/container.admin"
  member   = each.key
}

# --- The cluster ---------------------------------------------------------------------------

resource "google_container_cluster" "this" {
  #checkov:skip=CKV_GCP_12:Dataplane V2 (ADVANCED_DATAPATH) enforces NetworkPolicies itself; network_policy is Calico's, which it replaces.
  #checkov:skip=CKV_GCP_69:There's no default pool; every node pool runs GKE's metadata server (GKE_METADATA), which tests/cluster.tftest.hcl checks.
  #checkov:skip=CKV_GCP_65:Who administers the cluster is IAM's (admin_members). Google Groups for RBAC needs a Workspace group named gke-security-groups, which not every company has.
  #checkov:skip=CKV_GCP_21:resource_labels is local.labels, a merge Checkov doesn't evaluate: the deployment's labels and iki-deployment.
  project     = var.project_id
  name        = var.name
  description = "Iki: ${var.name}"
  location    = var.region
  # The nodes' zones; a regional control plane spans the region's zones itself.
  node_locations = var.zones

  network         = var.network_id
  subnetwork      = var.subnet_id
  networking_mode = "VPC_NATIVE"
  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
    stack_type                    = "IPV4"
  }

  # Node pools are made below, each its own resource: no default pool (GKE 1.36.4+).
  # Release channel and version: GKE upgrades within the channel, never below this.
  min_master_version = var.kubernetes_version
  release_channel {
    channel = var.release_channel
  }

  # Dataplane V2 (eBPF, Cilium) enforces NetworkPolicies, which the chart relies on.
  datapath_provider = "ADVANCED_DATAPATH"
  # Traffic between pods on one node goes through the VPC too, so flow logs and
  # firewall logs see it.
  enable_intranode_visibility = true

  # Pods start only with images the project's Binary Authorization policy allows: the
  # binary-authorization module's, which refuses everything it doesn't name.
  binary_authorization {
    evaluation_mode = "PROJECT_SINGLETON_POLICY_ENFORCE"
  }

  # Private nodes; the API's IP endpoint is private unless networks are listed, and its
  # DNS endpoint, which only IAM-authorized callers can use, is reachable from outside
  # only if asked.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = length(var.public_access_cidrs) == 0
  }
  master_authorized_networks_config {
    gcp_public_cidrs_access_enabled      = false
    private_endpoint_enforcement_enabled = true
    dynamic "cidr_blocks" {
      for_each = concat(var.public_access_cidrs, var.private_access_cidrs)
      content {
        cidr_block   = cidr_blocks.value
        display_name = "Allowed: ${cidr_blocks.value}"
      }
    }
  }
  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = var.dns_endpoint
    }
  }

  # Envelope encryption of Kubernetes Secrets: etcd holds them encrypted with this key.
  database_encryption {
    state    = "ENCRYPTED"
    key_name = var.secrets_kms_key
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  enable_shielded_nodes = true
  enable_legacy_abac    = false
  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }

  # Every control-plane log: the API server (with Kubernetes' audit log, which GKE always
  # keeps in Cloud Audit Logs), the scheduler and the controller manager.
  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS", "APISERVER", "SCHEDULER", "CONTROLLER_MANAGER"]
  }
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS", "APISERVER", "SCHEDULER", "CONTROLLER_MANAGER", "STORAGE", "POD", "DEPLOYMENT", "STATEFULSET", "DAEMONSET", "HPA"]
    managed_prometheus {
      enabled = var.managed_prometheus
    }
  }

  # GKE's view of workloads' configuration and of known vulnerabilities in their images.
  security_posture_config {
    mode               = "BASIC"
    vulnerability_mode = "VULNERABILITY_BASIC"
  }

  addons_config {
    http_load_balancing {
      disabled = false
    }
    horizontal_pod_autoscaling {
      disabled = false
    }
    gce_persistent_disk_csi_driver_config {
      enabled = true
    }
    gcp_filestore_csi_driver_config {
      enabled = var.filestore_csi
    }
    # Off: with NodeLocal DNSCache, pods' lookups go to node-local-dns, which the chart's
    # NetworkPolicies, admitting DNS to kube-dns alone, would refuse.
    dns_cache_config {
      enabled = false
    }
  }

  # The Gateway API, which serves Iki through Google's Application Load Balancer.
  gateway_api_config {
    channel = "CHANNEL_STANDARD"
  }

  # Upgrades and repairs at weekends, in UTC.
  maintenance_policy {
    recurring_window {
      start_time = "2026-01-03T02:00:00Z"
      end_time   = "2026-01-03T08:00:00Z"
      recurrence = "FREQ=WEEKLY;BYDAY=SA,SU"
    }
  }

  deletion_protection = var.deletion_protection
  resource_labels     = local.labels
}

# --- Node pools ----------------------------------------------------------------------------

resource "google_container_node_pool" "this" {
  for_each       = var.node_pools
  project        = var.project_id
  name           = each.key
  cluster        = google_container_cluster.this.id
  location       = var.region
  node_locations = var.zones
  # Per zone; the autoscaler then keeps the pool's total within its limits.
  initial_node_count = ceil(each.value.min_size / length(var.zones))

  autoscaling {
    total_min_node_count = each.value.min_size
    total_max_node_count = each.value.max_size
    location_policy      = each.value.spot ? "ANY" : "BALANCED"
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    strategy        = "SURGE"
    max_surge       = 1
    max_unavailable = 0
  }

  node_config {
    machine_type    = each.value.machine_type
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.nodes.email
    # Access is decided by IAM, not scopes; the node's account can do little.
    oauth_scopes      = ["https://www.googleapis.com/auth/cloud-platform"]
    disk_type         = each.value.disk_type
    disk_size_gb      = each.value.disk_size_gib
    boot_disk_kms_key = var.nodes_kms_key
    spot              = each.value.spot
    tags              = [var.node_tag]
    labels            = { "iki.ikisci.org/pool" = each.key }
    resource_labels   = merge(local.labels, { iki-pool = each.key })

    # Pods reach only GKE's metadata server, which hands them their own Workload
    # Identity, never the node's: IMDSv2 with one hop, here.
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
    metadata = {
      disable-legacy-endpoints = "true"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    dynamic "guest_accelerator" {
      for_each = each.value.gpu == null ? [] : [each.value.gpu]
      content {
        type  = guest_accelerator.value.type
        count = guest_accelerator.value.count
        gpu_driver_installation_config {
          gpu_driver_version = "LATEST"
        }
      }
    }

    # Plugins' code runs on nodes of its own: every pool but system is tainted.
    dynamic "taint" {
      for_each = each.key == "system" ? [] : [each.key]
      content {
        key    = "iki.ikisci.org/pool"
        value  = taint.value
        effect = "NO_SCHEDULE"
      }
    }
  }

  # The autoscaler moves the pool's size; OpenTofu shouldn't move it back.
  lifecycle {
    ignore_changes = [initial_node_count]
  }
}
