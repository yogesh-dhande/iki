# What the VM tier's machines are given on Compute Engine (ADR 0074, 0107): a service
# account that can only pull their images, an instance template holding every setting
# that keeps a machine locked down (no public address, Shielded VM, OS Login, no
# project-wide SSH keys or serial port, a disk encrypted with its own key), and firewall
# rules that let nothing in and let them reach only Iki, Google's APIs and the file
# systems named. A GCE provisioner starts machines from the template, overriding only
# the machine type, image, GPUs, disks and labels a compute template names.

locals {
  labels = merge(var.labels, { iki-deployment = var.name })
}

resource "google_service_account" "machines" {
  project      = var.project_id
  account_id   = "${var.name}-vm"
  display_name = "${var.name}: VM-tier machines"
  description  = "The VM tier's machines: pull their images, nothing else"
}

resource "google_compute_region_instance_template" "machines" {
  project     = var.project_id
  name_prefix = "${var.name}-vm-"
  region      = var.region
  description = "${var.name}: the VM tier's machines, locked down; the provisioner overrides machine type, image, GPUs and disks"
  # A starting point: the provisioner names each run's machine type.
  machine_type = var.machine_type
  tags         = [var.network_tag]
  labels       = local.labels

  disk {
    boot         = true
    auto_delete  = true
    source_image = var.image
    disk_type    = "pd-balanced"
    disk_size_gb = 50
    labels       = local.labels
    disk_encryption_key {
      kms_key_self_link = var.kms_key
    }
  }

  # No access_config: no public address.
  network_interface {
    subnetwork = var.subnet_id
    stack_type = "IPV4_ONLY"
  }

  service_account {
    email  = google_service_account.machines.email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    # Nobody signs in with a key someone added to the project; OS Login and IAM decide.
    block-project-ssh-keys = "TRUE"
    enable-oslogin         = "TRUE"
    serial-port-enable     = "FALSE"
  }

  scheduling {
    automatic_restart   = false
    on_host_maintenance = "TERMINATE"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# --- Firewalls ------------------------------------------------------------------------------

# Nothing in: the network module denies all ingress, and nothing here allows any. Out,
# only what's listed; the network module denies the rest.
resource "google_compute_firewall" "vpc" {
  project            = var.project_id
  name               = "${var.name}-vm-vpc"
  network            = var.network_id
  description        = "The VM tier's machines: Iki's server and the egress proxy in the VPC, over TLS"
  direction          = "EGRESS"
  priority           = 1000
  target_tags        = [var.network_tag]
  destination_ranges = [var.vpc_cidr]
  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "https" {
  count              = length(var.extra_https_cidrs) == 0 ? 0 : 1
  project            = var.project_id
  name               = "${var.name}-vm-https"
  network            = var.network_id
  description        = "The VM tier's machines: other networks over TLS, as asked"
  direction          = "EGRESS"
  priority           = 1000
  target_tags        = [var.network_tag]
  destination_ranges = var.extra_https_cidrs
  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "nfs" {
  count              = length(var.nfs_cidrs) == 0 ? 0 : 1
  project            = var.project_id
  name               = "${var.name}-vm-nfs"
  network            = var.network_id
  description        = "The VM tier's machines: shared file systems over NFS"
  direction          = "EGRESS"
  priority           = 1000
  target_tags        = [var.network_tag]
  destination_ranges = var.nfs_cidrs
  allow {
    protocol = "tcp"
    ports    = ["111", "2049"]
  }
  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}
