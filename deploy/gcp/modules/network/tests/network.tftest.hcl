# Runs against a mocked Google provider: no project, no credentials. Each run checks a
# property ADR 0107 promises.

mock_provider "google" {}

variables {
  name       = "iki-test"
  project_id = "iki-test-project"
  region     = "europe-west1"
}

run "everything_is_private_and_reaches_google_privately" {
  assert {
    condition     = !google_compute_network.this.auto_create_subnetworks
    error_message = "The VPC should have only the subnets made here."
  }
  assert {
    condition     = google_compute_subnetwork.nodes.private_ip_google_access
    error_message = "Nodes without public addresses should reach Google's APIs through Private Google Access."
  }
  assert {
    condition     = toset(google_dns_record_set.google_apis["googleapis.com."].rrdatas) == toset(["199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11"])
    error_message = "Google's APIs should resolve to private.googleapis.com inside the VPC."
  }
}

run "ranges_fit_and_never_overlap" {
  assert {
    condition     = google_compute_subnetwork.nodes.ip_cidr_range == "10.40.0.0/20" && google_compute_subnetwork.proxy.ip_cidr_range == "10.40.32.0/23" && output.managed_services_cidr == "10.40.240.0/20"
    error_message = "Nodes, proxies and managed services should each get their own part of the VPC's range."
  }
  assert {
    condition     = google_compute_global_address.services.address == "10.40.240.0" && google_compute_global_address.services.prefix_length == 20
    error_message = "Cloud SQL and Filestore should get addresses from the range reserved for them."
  }
}

run "nat_sends_from_fixed_addresses_and_logs_its_errors" {
  assert {
    condition     = length(google_compute_address.nat) == 2 && google_compute_router_nat.this.nat_ip_allocate_option == "MANUAL_ONLY"
    error_message = "Cloud NAT should send from addresses that stay the same, for allowlists."
  }
  assert {
    condition     = one(google_compute_router_nat.this.log_config).enable
    error_message = "NAT's errors should be logged."
  }
}

run "everything_is_denied_both_ways_unless_allowed" {
  assert {
    condition     = google_compute_firewall.deny_ingress.priority == 65534 && one(google_compute_firewall.deny_ingress.deny).protocol == "all" && google_compute_firewall.deny_ingress.source_ranges == toset(["0.0.0.0/0"])
    error_message = "Ingress should be denied by default, and logged."
  }
  assert {
    condition     = google_compute_firewall.deny_egress.direction == "EGRESS" && one(google_compute_firewall.deny_egress.deny).protocol == "all" && google_compute_firewall.deny_egress.destination_ranges == toset(["0.0.0.0/0"])
    error_message = "Egress should be denied by default too, and logged."
  }
  assert {
    condition     = alltrue([for f in concat([google_compute_firewall.deny_ingress, google_compute_firewall.deny_egress, google_compute_firewall.nodes_internal, google_compute_firewall.google_apis], values(google_compute_firewall.nodes_out)) : one(f.log_config).metadata == "INCLUDE_ALL_METADATA"])
    error_message = "Every firewall rule should log what it matches."
  }
  assert {
    condition     = keys(google_compute_firewall.nodes_out) == ["https"] && google_compute_firewall.nodes_out["https"].target_tags == toset(["iki-test-nodes"])
    error_message = "By default only the cluster's nodes go out, and only on HTTPS."
  }
}

run "egress_narrows_as_asked" {
  variables {
    node_egress = [
      { name = "https", ports = [443], cidrs = ["203.0.113.0/24"] },
      { name = "email", ports = [587], cidrs = ["198.51.100.10/32"] },
    ]
  }
  assert {
    condition     = google_compute_firewall.nodes_out["https"].destination_ranges == toset(["203.0.113.0/24"]) && one(google_compute_firewall.nodes_out["email"].allow).ports == tolist(["587"])
    error_message = "Each egress rule should allow only its ports to its networks."
  }
}

run "flow_logs_record_every_flow" {
  assert {
    condition     = one(google_compute_subnetwork.nodes.log_config).flow_sampling == 1 && one(google_compute_subnetwork.nodes.log_config).metadata == "INCLUDE_ALL_METADATA"
    error_message = "Flow logs should record every flow, with its metadata."
  }
}

run "refuses_a_range_too_small" {
  command = plan
  variables {
    cidr = "10.40.0.0/20"
  }
  expect_failures = [var.cidr]
}
