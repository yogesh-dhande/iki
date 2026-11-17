output "repositories" {
  description = "Each repository's address to push to and pull from, such as europe-west1-docker.pkg.dev/<project>/iki-production-worker."
  value       = { for k, r in google_artifact_registry_repository.this : k => "${var.location}-docker.pkg.dev/${var.project_id}/${r.repository_id}" }
}

output "readers" {
  description = "Who may pull from each repository."
  value       = { for r in keys(var.repositories) : r => sort([for b in local.readers : b.member if b.repository == r]) }
  depends_on  = [google_artifact_registry_repository_iam_member.readers]
}
