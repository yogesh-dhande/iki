# Secret Manager entries for Iki's secrets (ADR 0107), each kept in the region and the DR
# region, encrypted with a key of its own in each. OpenTofu makes the entries, never their
# values: scripts/put-secrets.sh generates those and writes them straight to Secret
# Manager, so they never sit in OpenTofu's state. External Secrets copies them into the
# cluster.

locals {
  labels = merge(var.labels, { iki-deployment = var.name })
  # What each holds, and the chart's value that names it.
  required = {
    "secret-key"         = "Django's SECRET_KEY (secretKey)"
    "encryption-keys"    = "Fernet keys that encrypt secrets Iki stores, the first one encrypting (encryptionKeys)"
    "bundle-signing-key" = "The Ed25519 seed this deployment signs bundles with (bundles.signingKey)"
    "database"           = "A postgres:// URI for the app's role, iki_app (database)"
    "database-migrator"  = "A postgres:// URI for the schema's owner, iki (database.migrator)"
    "database-admin"     = "The password of Cloud SQL's postgres user, which creates Iki's two roles once"
    "run-secrets"        = "Plugins' secrets, as a JSON object keyed like dna.NCBI_API_KEY (runSecrets)"
  }
  optional = {
    "email"                = "The mail server's URL, with its credentials (email)"
    "backend-worker-token" = "A backend-tier registration token (backendWorkers.registrationToken)"
  }
  secrets = merge(local.required, { for k, v in local.optional : k => v if contains(var.optional_secrets, k) })
}

resource "google_secret_manager_secret" "this" {
  for_each  = local.secrets
  project   = var.project_id
  secret_id = "${var.name}-${each.key}"
  labels    = merge(local.labels, { secret = each.key })
  annotations = {
    "iki.ikisci.org/holds" = each.value
  }

  # Kept in both regions, each copy under its own region's key: losing one loses no
  # secret, and the recovered deployment reads them where it is.
  replication {
    user_managed {
      replicas {
        location = var.region
        customer_managed_encryption {
          kms_key_name = var.kms_key
        }
      }
      replicas {
        location = var.dr_region
        customer_managed_encryption {
          kms_key_name = var.dr_kms_key
        }
      }
    }
  }

  # A destroyed version can be restored for this long.
  version_destroy_ttl = "${var.recovery_window_days * 86400}s"
  deletion_protection = true
}
