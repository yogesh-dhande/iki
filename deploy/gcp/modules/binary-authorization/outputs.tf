output "allowed" {
  description = "Every pattern of images the policy allows, besides GKE's own."
  value       = [for p in google_binary_authorization_policy.this.admission_whitelist_patterns : p.name_pattern]
}
