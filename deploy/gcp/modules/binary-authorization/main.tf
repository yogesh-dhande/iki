# The project's Binary Authorization policy (ADR 0107), which the cluster enforces
# (PROJECT_SINGLETON_POLICY_ENFORCE): a pod starts only if its image is in this
# deployment's repositories or matches a pattern named here; GKE's own system images
# are allowed by Google's global policy. Anything else is refused, and the refusal is
# written to Cloud Audit Logs. Kept in the project's logs by the logs module.

locals {
  # Every image in a repository of the deployment's own, at any depth, by tag or digest.
  patterns = distinct(concat([for r in var.repositories : "${r}/**"], var.images))
}

resource "google_binary_authorization_policy" "this" {
  project     = var.project_id
  description = "${var.name}: only this deployment's images, Iki's, and those named"

  # Images GKE runs itself (kube-system, its agents) are judged by Google's own policy,
  # which allows them, before this one.
  global_policy_evaluation_mode = "ENABLE"

  dynamic "admission_whitelist_patterns" {
    for_each = local.patterns
    content {
      name_pattern = admission_whitelist_patterns.value
    }
  }

  default_admission_rule {
    evaluation_mode  = "ALWAYS_DENY"
    enforcement_mode = "ENFORCED_BLOCK_AND_AUDIT_LOG"
  }
}
