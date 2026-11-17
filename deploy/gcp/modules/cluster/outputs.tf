output "name" {
  description = "The cluster's name."
  value       = google_container_cluster.this.name
}

output "id" {
  description = "The cluster's ID (projects/<project>/locations/<region>/clusters/<name>)."
  value       = google_container_cluster.this.id
}

output "location" {
  description = "The cluster's region: gcloud container clusters get-credentials --location."
  value       = google_container_cluster.this.location
}

output "endpoint" {
  description = "The Kubernetes API's private address."
  value       = google_container_cluster.this.endpoint
}

output "dns_endpoint" {
  description = "The Kubernetes API's DNS endpoint, which IAM guards."
  value       = one(one(google_container_cluster.this.control_plane_endpoints_config).dns_endpoint_config).endpoint
}

output "workload_pool" {
  description = "Workload Identity's pool, which pods' identities are in."
  value       = one(google_container_cluster.this.workload_identity_config).workload_pool
}

output "node_service_account" {
  description = "The nodes' service account: the artifact-registry module lets it pull."
  value       = google_service_account.nodes.email
}

output "node_pools" {
  description = "Each pool's label and, for dedicated pools, the toleration pods need to run there."
  value = {
    for name, _ in var.node_pools : name => {
      node_selector = { "iki.ikisci.org/pool" = name }
      tolerations   = name == "system" ? [] : [{ key = "iki.ikisci.org/pool", operator = "Equal", value = name, effect = "NoSchedule" }]
    }
  }
}
