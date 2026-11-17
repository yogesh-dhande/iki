output "network_id" {
  description = "The VPC's ID (projects/<project>/global/networks/<name>)."
  value       = google_compute_network.this.id
}

output "network_name" {
  description = "The VPC's name."
  value       = google_compute_network.this.name
}

output "subnet_id" {
  description = "The nodes' subnet: the cluster's and the VM tier's machines'."
  value       = google_compute_subnetwork.nodes.id
}

output "subnet_name" {
  description = "Its name."
  value       = google_compute_subnetwork.nodes.name
}

output "cidr" {
  description = "The VPC's range for nodes, machines, proxies and managed services."
  value       = var.cidr
}

output "nodes_cidr" {
  description = "The nodes' subnet's range."
  value       = google_compute_subnetwork.nodes.ip_cidr_range
}

output "pods_range_name" {
  description = "The secondary range pods' addresses come from."
  value       = "pods"
}

output "services_range_name" {
  description = "The secondary range Services' addresses come from."
  value       = "services"
}

output "proxy_cidr" {
  description = "The proxy-only subnet's range: an internal load balancer reaches pods from it, which the chart's NetworkPolicy admits."
  value       = google_compute_subnetwork.proxy.ip_cidr_range
}

output "managed_services_cidr" {
  description = "The range Cloud SQL and Filestore get addresses in: what pods may reach the database at (the chart's networkPolicy.egress.database)."
  value       = local.psa_cidr
}

output "managed_services_range_name" {
  description = "Its reserved range's name, for Cloud SQL's allocated_ip_range."
  value       = google_compute_global_address.services.name
}

output "managed_services_connection" {
  description = "The peering with Google's managed services: made before any of them is."
  value       = google_service_networking_connection.services.id
}

output "google_apis_cidr" {
  description = "private.googleapis.com, where Google's APIs are reached."
  value       = local.google_apis
}

output "nat_ips" {
  description = "The addresses traffic from the VPC leaves by, for allowlists elsewhere."
  value       = google_compute_address.nat[*].address
}

output "node_tag" {
  description = "The network tag the cluster's nodes carry, which the firewall rules name."
  value       = local.node_tag
}

output "vm_tag" {
  description = "The network tag the VM tier's machines carry."
  value       = local.vm_tag
}
