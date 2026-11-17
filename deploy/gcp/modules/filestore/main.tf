# An optional Filestore instance for shared reference data (ADR 0077): runs mount one copy
# instead of each making a disk. Regional by default, so it survives losing a zone;
# encrypted with a key of its own; root squashed, so a container's root is nobody on it;
# and exported only to the networks named. Iki keeps its versions under /iki on it.

resource "google_filestore_instance" "this" {
  project      = var.project_id
  name         = var.name
  description  = "${var.name}: shared reference data"
  location     = var.tier == "ZONAL" ? var.zone : var.region
  tier         = var.tier
  protocol     = "NFS_V4_1"
  kms_key_name = var.kms_key
  labels       = merge(var.labels, { iki-deployment = var.name })

  deletion_protection_enabled = true
  deletion_protection_reason  = "Holds every version of shared reference data that runs have used."

  file_shares {
    name        = "iki"
    capacity_gb = var.capacity_gib

    # Root is squashed everywhere, except from the machines named, which make /iki for
    # runs' user (UID 10001) once.
    dynamic "nfs_export_options" {
      for_each = length(var.root_squash_except_cidrs) == 0 ? [] : [1]
      content {
        ip_ranges   = var.root_squash_except_cidrs
        access_mode = "READ_WRITE"
        squash_mode = "NO_ROOT_SQUASH"
      }
    }
    nfs_export_options {
      ip_ranges   = var.client_cidrs
      access_mode = "READ_WRITE"
      squash_mode = "ROOT_SQUASH"
      anon_uid    = 65534
      anon_gid    = 65534
    }
  }

  networks {
    network      = var.network_id
    modes        = ["MODE_IPV4"]
    connect_mode = "PRIVATE_SERVICE_ACCESS"
  }
}
