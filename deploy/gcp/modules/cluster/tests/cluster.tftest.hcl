# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0107 promises.

mock_provider "google" {
  mock_resource "google_service_account" {
    defaults = {
      email  = "iki-test-nodes@iki-test-project.iam.gserviceaccount.com"
      member = "serviceAccount:iki-test-nodes@iki-test-project.iam.gserviceaccount.com"
    }
  }
}

variables {
  name                = "iki-test"
  project_id          = "iki-test-project"
  region              = "europe-west1"
  zones               = ["europe-west1-b", "europe-west1-c", "europe-west1-d"]
  kubernetes_version  = "1.36"
  network_id          = "projects/iki-test-project/global/networks/iki-test"
  subnet_id           = "projects/iki-test-project/regions/europe-west1/subnetworks/iki-test-nodes"
  pods_range_name     = "pods"
  services_range_name = "services"
  node_tag            = "iki-test-nodes"
  secrets_kms_key     = "projects/iki-test-project/locations/europe-west1/keyRings/iki-test/cryptoKeys/kubernetes-secrets"
  nodes_kms_key       = "projects/iki-test-project/locations/europe-west1/keyRings/iki-test/cryptoKeys/node-disks"
}

run "kubernetes_secrets_are_encrypted_with_a_key_of_their_own" {
  assert {
    condition     = one(google_container_cluster.this.database_encryption).state == "ENCRYPTED" && one(google_container_cluster.this.database_encryption).key_name == var.secrets_kms_key
    error_message = "Kubernetes Secrets should be envelope-encrypted with the cluster's key."
  }
}

run "nodes_are_private_and_the_api_too_by_default" {
  assert {
    condition     = one(google_container_cluster.this.private_cluster_config).enable_private_nodes && one(google_container_cluster.this.private_cluster_config).enable_private_endpoint
    error_message = "Nodes should have no public addresses, and the API's IP endpoint should be private."
  }
  assert {
    condition     = !one(one(google_container_cluster.this.control_plane_endpoints_config).dns_endpoint_config).allow_external_traffic
    error_message = "The DNS endpoint should be closed to outside traffic unless asked."
  }
  assert {
    condition     = !one(google_container_cluster.this.master_authorized_networks_config).gcp_public_cidrs_access_enabled
    error_message = "Google Cloud's public addresses shouldn't reach the API just for being Google's."
  }
}

run "the_api_opens_only_to_the_networks_listed" {
  variables {
    public_access_cidrs = ["203.0.113.0/24"]
  }
  assert {
    condition     = !one(google_container_cluster.this.private_cluster_config).enable_private_endpoint && [for c in one(google_container_cluster.this.master_authorized_networks_config).cidr_blocks : c.cidr_block] == ["203.0.113.0/24"]
    error_message = "The public endpoint should admit only the networks listed."
  }
}

run "refuses_to_open_the_api_to_everyone" {
  command = plan
  variables {
    public_access_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.public_access_cidrs]
}

run "only_the_admins_named_get_in" {
  variables {
    admin_members = ["group:platform@example.com"]
  }
  assert {
    condition     = keys(google_project_iam_member.admins) == ["group:platform@example.com"] && google_project_iam_member.admins["group:platform@example.com"].role == "roles/container.admin"
    error_message = "Each named admin should get GKE's admin role, and nobody else."
  }
}

run "pods_cant_take_the_nodes_identity" {
  assert {
    condition     = alltrue([for p in google_container_node_pool.this : one(one(p.node_config).workload_metadata_config).mode == "GKE_METADATA"])
    error_message = "Pods should reach only GKE's metadata server, which gives them their own identity."
  }
  assert {
    condition     = one(google_container_cluster.this.workload_identity_config).workload_pool == "iki-test-project.svc.id.goog"
    error_message = "Workload Identity should be on."
  }
}

run "nodes_are_shielded_encrypted_and_minimal" {
  assert {
    condition = alltrue([for p in google_container_node_pool.this :
      one(one(p.node_config).shielded_instance_config).enable_secure_boot && one(one(p.node_config).shielded_instance_config).enable_integrity_monitoring
    ]) && google_container_cluster.this.enable_shielded_nodes
    error_message = "Nodes should boot securely, with integrity monitoring."
  }
  assert {
    condition     = alltrue([for p in google_container_node_pool.this : one(p.node_config).boot_disk_kms_key == var.nodes_kms_key && one(p.node_config).image_type == "COS_CONTAINERD"])
    error_message = "Node disks should be encrypted with the node-disks key, and nodes run Container-Optimized OS."
  }
  assert {
    condition     = google_project_iam_member.nodes.role == "roles/container.defaultNodeServiceAccount"
    error_message = "The nodes' account should hold only GKE's minimal node role."
  }
}

run "network_policies_are_enforced" {
  assert {
    condition     = google_container_cluster.this.datapath_provider == "ADVANCED_DATAPATH"
    error_message = "Dataplane V2 should enforce the chart's NetworkPolicies."
  }
}

run "every_control_plane_log_is_kept" {
  assert {
    condition     = toset(["APISERVER", "SCHEDULER", "CONTROLLER_MANAGER"]) == setintersection(one(google_container_cluster.this.logging_config).enable_components, toset(["APISERVER", "SCHEDULER", "CONTROLLER_MANAGER"]))
    error_message = "The API server's, scheduler's and controller manager's logs should be on."
  }
}

run "plugins_code_gets_nodes_of_its_own" {
  assert {
    condition     = length(one(google_container_node_pool.this["system"].node_config).taint) == 0
    error_message = "The system pool runs Iki itself: no taint."
  }
  assert {
    condition = alltrue([for name in ["runs", "gpu"] :
      one(one(google_container_node_pool.this[name].node_config).taint).value == name && one(one(google_container_node_pool.this[name].node_config).taint).effect == "NO_SCHEDULE"
    ])
    error_message = "Dedicated pools should be tainted, so only runs that tolerate them land there."
  }
  assert {
    condition     = one(one(google_container_node_pool.this["gpu"].node_config).guest_accelerator).type == "nvidia-l4" && length(one(google_container_node_pool.this["runs"].node_config).guest_accelerator) == 0
    error_message = "Only the GPU pool should have GPUs, with Google's drivers installed."
  }
  assert {
    condition     = output.node_pools["runs"].tolerations[0].value == "runs" && length(output.node_pools["system"].tolerations) == 0
    error_message = "The node_pools output should give the toleration each dedicated pool needs."
  }
}

run "pools_are_sized_in_total_across_zones" {
  assert {
    condition     = one(google_container_node_pool.this["system"].autoscaling).total_min_node_count == 3 && google_container_node_pool.this["system"].initial_node_count == 1
    error_message = "Three system nodes in all: one in each zone."
  }
}

run "refuses_a_cluster_without_a_system_pool" {
  command = plan
  variables {
    node_pools = {
      runs = { machine_type = "n2-standard-4", min_size = 1, max_size = 2 }
    }
  }
  expect_failures = [var.node_pools]
}

run "refuses_a_spot_system_pool" {
  command = plan
  variables {
    node_pools = {
      system = { machine_type = "n2-standard-4", min_size = 3, max_size = 6, spot = true }
    }
  }
  expect_failures = [var.node_pools]
}

run "refuses_a_version_that_needs_a_default_pool" {
  command = plan
  variables {
    kubernetes_version = "1.33"
  }
  expect_failures = [var.kubernetes_version]
}

run "pods_resolve_names_through_kube_dns" {
  command = plan
  assert {
    condition     = !one(one(google_container_cluster.this.addons_config).dns_cache_config).enabled
    error_message = "The chart's NetworkPolicies admit DNS to kube-dns alone: NodeLocal DNSCache would answer from elsewhere, and be refused."
  }
}

run "it_runs_only_images_binary_authorization_allows" {
  command = plan
  assert {
    condition     = google_container_cluster.this.binary_authorization[0].evaluation_mode == "PROJECT_SINGLETON_POLICY_ENFORCE"
    error_message = "The cluster should enforce the project's Binary Authorization policy."
  }
}
